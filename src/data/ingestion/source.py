"""Small raw contracts shared by readers and the profiler."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.jsonio import ContractError, dumps


@dataclass(frozen=True)
class ReaderOptions:
    encoding: str = "utf-8-sig"
    delimiter: str | None = None
    max_shared_string_bytes: int = 16 * 1024 * 1024
    max_cell_bytes: int = 1024 * 1024
    max_columns: int = 1024

    def __post_init__(self) -> None:
        if self.delimiter is not None and self.delimiter not in {",", ";", "\t", "|"}:
            raise ContractError("unsupported CSV delimiter")
        if min(self.max_shared_string_bytes, self.max_cell_bytes, self.max_columns) <= 0:
            raise ContractError("reader budgets must be positive")


@dataclass(frozen=True, slots=True)
class Cell:
    raw_value: Any
    native_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceRow:
    ordinal: int
    row: int | None
    cells: dict[int, Cell]
    locator: dict[str, Any] = field(default_factory=dict)
    line_end: int | None = None

    def reference(self, table: SourceTable, column: int | None = None) -> dict:
        return {
            **table.source_ref,
            "row": self.row,
            "column": column,
            "record_ordinal": self.ordinal,
            "line_end": self.line_end,
            "locator": self.locator or None,
        }

    def identity(self, table: SourceTable) -> str:
        return hashlib.sha256(dumps(self.reference(table)).encode()).hexdigest()


@dataclass
class SourceTable:
    source_ref: dict[str, Any]
    format: str
    metadata: dict[str, Any]
    rows: Callable[[], Iterator[SourceRow]]

    @property
    def source_id(self) -> str:
        identity = [self.source_ref[k] for k in ("split", "business_id", "file", "table")]
        return hashlib.sha256(dumps(identity + ["whole_table"]).encode()).hexdigest()

    def iter_rows(self) -> Iterator[SourceRow]:
        yield from self.rows()


def source_reference(root: Path, path: Path, business: str, digest: str, table: str) -> dict:
    return {
        "split": root.name,
        "business_id": business,
        "file": path.relative_to(root).as_posix(),
        "sha256": digest,
        "table": table,
        "row": None,
        "column": None,
    }
