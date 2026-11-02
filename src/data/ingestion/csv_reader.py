"""Strict text CSV with record and physical-line provenance."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from src.data.ingestion.source import (
    Cell,
    ReaderOptions,
    SourceRow,
    SourceTable,
    source_reference,
)
from src.jsonio import ContractError


def csv_tables(
    root: Path, path: Path, business: str, digest: str, options: ReaderOptions
) -> list[SourceTable]:
    with path.open(encoding=options.encoding, newline="") as handle:
        sample = handle.read(min(options.max_cell_bytes, 65536))
    delimiter = options.delimiter
    if delimiter is None and sample.strip():
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error as exc:
            raise ContractError("CSV dialect unresolved; supply an explicit delimiter") from exc
    metadata = {"encoding": options.encoding, "delimiter": delimiter, "declared_columns": []}

    def rows() -> Iterator[SourceRow]:
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(options.max_cell_bytes)
        try:
            with path.open(encoding=options.encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter or ",", strict=True)
                end = 0
                try:
                    for ordinal, values in enumerate(reader, 1):
                        if len(values) > options.max_columns:
                            raise ContractError("CSV column budget exceeded")
                        if any(len(value.encode()) > options.max_cell_bytes for value in values):
                            raise ContractError("CSV cell byte budget exceeded")
                        start = end + 1
                        end = reader.line_num
                        yield SourceRow(
                            ordinal,
                            start,
                            {i: Cell(value, "text") for i, value in enumerate(values, 1)},
                            line_end=end,
                        )
                except csv.Error as exc:
                    raise ContractError(f"CSV physical line {reader.line_num}: {exc}") from exc
        finally:
            csv.field_size_limit(previous_limit)

    return [
        SourceTable(source_reference(root, path, business, digest, "csv"), "csv", metadata, rows)
    ]
