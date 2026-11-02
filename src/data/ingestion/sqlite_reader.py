"""Read-only SQLite tables with revision-scoped, explicitly ordered locators."""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from src.data.ingestion.source import (
    Cell,
    ReaderOptions,
    SourceRow,
    SourceTable,
    source_reference,
)
from src.jsonio import ContractError


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _cell(value, declared: str, limit: int) -> Cell:
    attrs = {"declared_type": declared}
    kind = {type(None): "null", int: "integer", float: "real", str: "text", bytes: "blob"}[
        type(value)
    ]
    if isinstance(value, bytes):
        value = base64.b64encode(value).decode("ascii")
        attrs["encoding"] = "base64"
    if value is not None and len(str(value).encode()) > limit:
        raise ContractError("cell byte budget exceeded")
    return Cell(value, kind, attrs)


def _rows(
    path: Path, name: str, columns: list[dict], options: ReaderOptions
) -> Iterator[SourceRow]:
    with closing(connect(path)) as connection:
        # Prefer rowid for raw lineage: it distinguishes identical export copies.
        aliases = {c["name"].casefold() for c in columns}
        locator_column = next((n for n in ["rowid", "_rowid_", "oid"] if n not in aliases), None)
        kind, locator_names = "rowid", [locator_column] if locator_column else []
        try:
            if not locator_column:
                raise sqlite3.OperationalError("rowid aliases shadowed")
            connection.execute(f"SELECT {locator_column} FROM {quote(name)} LIMIT 0")
        except sqlite3.OperationalError:
            locator_names = [
                c["name"]
                for c in sorted(columns, key=lambda c: c["pk_position"])
                if c["pk_position"]
            ]
            kind = "primary_key"
        if not locator_names:
            raise ContractError("no stable SQLite locator: rowid unavailable and no primary key")
        selection = ",".join(quote(n) for n in locator_names)
        fields = ",".join(quote(c["name"]) for c in columns)
        query = f"SELECT {selection},{fields} FROM {quote(name)} ORDER BY {selection}"
        previous = None
        for ordinal, values in enumerate(connection.execute(query), 1):
            locator_values = values[: len(locator_names)]
            if kind == "primary_key" and (None in locator_values or locator_values == previous):
                raise ContractError("primary-key locator is null or duplicated")
            previous = locator_values
            locator = {
                "kind": kind,
                "columns": locator_names,
                "values": [_cell(v, "", options.max_cell_bytes).raw_value for v in locator_values],
            }
            cells = {
                i: _cell(value, column["declared_type"], options.max_cell_bytes)
                for i, (column, value) in enumerate(
                    zip(columns, values[len(locator_names) :], strict=True), 1
                )
            }
            yield SourceRow(ordinal, None, cells, locator=locator)


def sqlite_tables(
    root: Path, path: Path, business: str, digest: str, options: ReaderOptions
) -> list[SourceTable]:
    tables = []
    with closing(connect(path)) as connection:
        objects = connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        ).fetchall()
        for name, kind, ddl in objects:
            columns = [
                {"name": r[1], "declared_type": r[2], "not_null": bool(r[3]), "pk_position": r[5]}
                for r in connection.execute(f"PRAGMA table_info({quote(name)})")
            ]
            if len(columns) > options.max_columns:
                raise ContractError("SQLite column budget exceeded")
            metadata = {
                "object_type": kind,
                "ddl": ddl,
                "declared_columns": columns,
                "foreign_keys": [
                    list(r) for r in connection.execute(f"PRAGMA foreign_key_list({quote(name)})")
                ],
                "indexes": [
                    list(r) for r in connection.execute(f"PRAGMA index_list({quote(name)})")
                ],
                "exclusion_reason": "system_table"
                if name.startswith("sqlite_")
                else "unsupported_view"
                if kind == "view"
                else None,
                "event_order": "unknown; raw locator order is not business event order",
            }

            def rows(name=name, columns=columns):
                yield from _rows(path, name, columns, options)

            tables.append(
                SourceTable(
                    source_reference(root, path, business, digest, name), "sqlite", metadata, rows
                )
            )
    return tables
