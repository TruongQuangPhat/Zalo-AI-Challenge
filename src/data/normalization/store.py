"""Store versioned prepared facts with exact decimals and revision-checked caching."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from src.data.cache import fingerprint
from src.data.catalog import BusinessContext, digest, discover_sources, require_output
from src.data.normalization.sales import build_sales, sales_mapping
from src.data.normalization.stock import build_stock, stock_mapping
from src.jsonio import ContractError, dumps, loads

VERSION = "prepared-facts-v3"
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE orders (
    order_key TEXT PRIMARY KEY, raw_order_id TEXT NOT NULL,
    ordered_at TEXT NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('completed','cancelled','returned','shipping')),
    mapping_status TEXT NOT NULL CHECK(mapping_status IN ('resolved','ambiguous'))
) STRICT;
CREATE TABLE order_lines (
    line_key TEXT PRIMARY KEY, order_key TEXT NOT NULL REFERENCES orders(order_key),
    net_amount TEXT, row_role TEXT NOT NULL, source_values TEXT NOT NULL, event_at TEXT
) STRICT;
CREATE INDEX order_lines_order ON order_lines(order_key);
CREATE TABLE lineage (
    raw_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    disposition TEXT NOT NULL, line_key TEXT REFERENCES order_lines(line_key),
    source_ref TEXT NOT NULL, transformations TEXT NOT NULL
) STRICT;
CREATE TABLE issues (issue_id TEXT PRIMARY KEY, detail TEXT NOT NULL) STRICT;
CREATE TABLE inventory_snapshot (
    product_code TEXT PRIMARY KEY, product_name TEXT NOT NULL, base_unit TEXT NOT NULL,
    quantity TEXT NOT NULL, snapshot_at TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE order_groups (
    order_key TEXT PRIMARY KEY REFERENCES orders(order_key), customer_group TEXT NOT NULL
) STRICT;
CREATE TABLE line_groups (
    line_key TEXT PRIMARY KEY REFERENCES order_lines(line_key), item_group TEXT NOT NULL
) STRICT;
"""


@dataclass(frozen=True)
class PreparedBusiness:
    path: Path | None
    summary: dict
    cache_hit: bool = False
    elapsed_seconds: float = 0

    def connect(self) -> sqlite3.Connection:
        if self.path is None or self.path.is_symlink():
            raise ContractError("prepared store is unavailable")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    def iter_records(self, table: str) -> Iterator[dict]:
        if table not in {"orders", "order_lines", "lineage", "issues", "inventory_snapshot"}:
            raise ContractError("unknown prepared table")
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"):
                record = dict(row)
                if table == "order_lines" and record["net_amount"] is not None:
                    record["net_amount"] = Decimal(record["net_amount"])
                if table == "inventory_snapshot":
                    record["quantity"] = Decimal(record["quantity"])
                for field in ["source_values", "source_ref", "transformations", "detail"]:
                    if field in record:
                        record[field] = loads(record[field])
                yield record


def _assert_revision(context: BusinessContext) -> None:
    catalog = context.catalog
    sources, excluded = discover_sources(context.root, catalog["business_id"])
    dependencies = catalog["dependencies"]
    if (
        sources != dependencies["sources"]
        or excluded != dependencies["excluded"]
        or any(
            digest(context.root / name) != expected
            for name, expected in dependencies["authority"].items()
        )
    ):
        raise ContractError("source/authority changed since raw catalog preparation")


def _copy_atomic(source: Path, target: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".writing-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, handle)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _cached(entry: Path, dependencies: dict) -> tuple[Path, dict] | None:
    try:
        path, manifest = entry / "facts.sqlite", entry / "manifest.json"
        if any(p.is_symlink() for p in [entry, path, manifest]):
            return None
        if loads(manifest.read_text()) != {
            "complete": True,
            "dependencies": dependencies,
            "facts_sha256": digest(path),
        }:
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return None
            metadata = {key: loads(value) for key, value in conn.execute("SELECT * FROM metadata")}
            if metadata["dependencies"] != dependencies:
                return None
            return path, metadata["summary"]
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return None


def prepare_facts(
    context: BusinessContext, output_dir: Path, *, cache_dir: Path | None = None
) -> PreparedBusiness:
    """Prepare supported facts once/business; unsupported shapes never become empty facts."""
    started = time.perf_counter()
    require_output(output_dir, context.root)
    if cache_dir is not None:
        require_output(cache_dir, context.root)
    _assert_revision(context)
    try:
        mapping = sales_mapping(context)
        builder = build_sales
    except ContractError as sales_error:
        try:
            mapping = stock_mapping(context)
            builder = build_stock
        except ContractError as stock_error:
            mapping = None
            reason = f"{sales_error}; {stock_error}"
    if mapping is None:
        result = PreparedBusiness(
            None,
            {
                "version": VERSION,
                "status": "unsupported",
                "reason": reason,
                "metric_execution": "not_implemented",
            },
            elapsed_seconds=time.perf_counter() - started,
        )
        context.prepared = result
        return result
    if output_dir.exists():
        raise ContractError("prepared output already exists; use a fresh directory")
    dependencies = {
        "version": VERSION,
        "source": context.catalog["dependencies"],
        "normalizer_sha256": fingerprint(
            {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
        ),
    }
    cache_entry = cache_dir / "prepared" / fingerprint(dependencies) if cache_dir else None
    if cache_entry is not None:
        require_output(cache_entry, context.root)
        cached = _cached(cache_entry, dependencies)
        if cached:
            source, summary = cached
            output_dir.mkdir(parents=True)
            destination = output_dir / "facts.sqlite"
            _copy_atomic(source, destination)
            _assert_revision(context)
            result = PreparedBusiness(destination, summary, True, time.perf_counter() - started)
            context.prepared = result
            return result
    output_dir.mkdir(parents=True)
    destination = output_dir / "facts.sqlite"
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".sqlite", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.executescript(SCHEMA)
            summary = {
                "version": VERSION,
                "split": context.catalog["split"],
                "business_id": context.catalog["business_id"],
                "backend": "sqlite",
                "money_storage": "exact_decimal_text",
                **builder(context, connection, mapping),
            }
            for key, value in [("dependencies", dependencies), ("summary", summary)]:
                connection.execute("INSERT INTO metadata VALUES (?,?)", (key, dumps(value)))
            connection.commit()
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ContractError("prepared database integrity failed")
        _assert_revision(context)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    if cache_entry is not None:
        cache_entry.mkdir(parents=True, exist_ok=True)
        _copy_atomic(destination, cache_entry / "facts.sqlite")
        manifest = {
            "complete": True,
            "dependencies": dependencies,
            "facts_sha256": digest(destination),
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=cache_entry, delete=False) as handle:
            manifest_temporary = Path(handle.name)
            try:
                handle.write(dumps(manifest) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(manifest_temporary, cache_entry / "manifest.json")
            finally:
                manifest_temporary.unlink(missing_ok=True)
    result = PreparedBusiness(destination, summary, False, time.perf_counter() - started)
    context.prepared = result
    return result
