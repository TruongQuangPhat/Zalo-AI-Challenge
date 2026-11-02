"""Bounded structural profiles and evidence for candidate relationships."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from itertools import chain, islice

from src.data.ingestion.source import Cell, SourceRow, SourceTable
from src.jsonio import ContractError


@dataclass(frozen=True)
class ProfileOptions:
    max_distinct: int = 100000
    max_counter_bytes: int = 32 * 1024 * 1024
    max_key_candidates: int = 12
    max_issues: int = 1000
    max_columns: int = 1024
    header_rows_examined: int = 32

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ContractError("profile budgets must be positive integers")


@lru_cache(maxsize=4096)
def _normalized_cached(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(c for c in text if not unicodedata.combining(c))


def normalized(text: str) -> str:
    if len(text) <= 1024:
        return _normalized_cached(text)
    text = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(c for c in text if not unicodedata.combining(c))


HEADER_WORDS = re.compile(
    r"\b(ma|ngay|ten|gia|tien|luong|hang|khach|don|thu|tong|thanh|trang|noi|"
    r"stt|sku|id|date|amount|order|product|customer|quantity|name|value|status)\b"
)
KEY_WORDS = re.compile(r"\b(ma|sku|id|barcode|imei|order_id|customer_id|product_id)\b")


def header_score(row: SourceRow) -> int:
    texts = [str(c.raw_value).strip() for c in row.cells.values() if c.native_type == "text"]
    words = sum(bool(HEADER_WORDS.search(normalized(t).replace("_", " "))) for t in texts)
    return words * 4 + sum(bool(re.search(r"[^\W\d_]", t)) for t in texts) if len(texts) >= 2 else 0


def _key(cell: Cell | None) -> tuple | None:
    if cell is None or cell.raw_value is None or cell.raw_value == "":
        return None
    # Type tags prevent silent ID coercion across text/numeric sources.
    return cell.native_type, cell.raw_value


def profile_table(table: SourceTable, options: ProfileOptions) -> tuple[dict, dict[int, Counter]]:
    """All row counts are exact; bounded distinct sets explicitly expose truncation."""
    issues, issue_counts = [], Counter()
    accounting = Counter()
    counters: dict[int, Counter] = {}
    exact: dict[int, bool] = {}
    columns: dict[int, dict] = {}
    used_bytes = 0
    raw_rows, physical_end, data_rows = 0, 0, 0
    structure = "resolved"

    def issue(code: str, row: SourceRow | None = None, column: int | None = None, **detail):
        issue_counts[code] += 1
        if len(issues) < options.max_issues:
            reference = row.reference(table, column) if row else table.source_ref
            issues.append({"code": code, "source_ref": reference, **detail})

    iterator = table.iter_rows()
    early, early_bytes = [], 0
    for row in islice(iterator, options.header_rows_examined):
        early_bytes += sum(len(str(c.raw_value).encode()) + 128 for c in row.cells.values())
        if early_bytes > options.max_counter_bytes:
            iterator.close()
            raise ContractError("header lookahead memory budget exceeded")
        early.append(row)
    declared = table.metadata.get("declared_columns", [])
    header = None
    if not declared:
        candidates = sorted(early, key=lambda row: (-header_score(row), row.ordinal))
        if candidates and header_score(candidates[0]) >= 8:
            header = candidates[0]
        elif any(r.cells for r in early):
            structure = "ambiguous"
            issue("header_unresolved")
    width = len(declared) if declared else max(header.cells, default=0) if header else 0
    header_values = {p: str(c.raw_value or "") for p, c in header.cells.items()} if header else {}
    header_signature = tuple((p, _key(c)) for p, c in header.cells.items()) if header else None
    header_first = next(iter(header.cells.values())).raw_value if header and header.cells else None
    header_threshold = max(8, header_score(header) - 2) if header else 8
    if header:
        for candidate in early:
            candidate_values = list(candidate.cells.values())
            if (
                candidate.ordinal < header.ordinal
                and candidate_values
                and candidate_values[0].native_type == "text"
                and HEADER_WORDS.search(
                    normalized(str(candidate_values[0].raw_value)).replace("_", " ")
                )
                and header_score(candidate) >= header_threshold
                and tuple((p, _key(c)) for p, c in candidate.cells.items()) != header_signature
            ):
                structure = "ambiguous"
                issue("conflicting_header_candidates", candidate)

    def add_column(position: int) -> None:
        if position > options.max_columns:
            raise ContractError("column budget exceeded")
        display = (
            declared[position - 1]["name"]
            if position <= len(declared)
            else header_values.get(position, "")
        )
        identity = f"{table.source_id}:{position}"
        columns[position] = {
            "column_id": identity,
            "name": f"column_{position}",
            "display_name": display,
            "position": position,
            "raw_header": header.reference(table, position) if header else None,
            "row_count": data_rows,
            "non_null_count": 0,
            "null_count": data_rows,
            "absent_count": data_rows,
            "blank_string_count": 0,
            "error_count": 0,
            "native_types": Counter({"absent": data_rows}) if data_rows else Counter(),
            "samples": [],
            "semantic_role": "identifier_candidate"
            if KEY_WORDS.search(normalized(display).replace("_", " "))
            else "date_candidate"
            if re.search(r"ngay|date|thoi.gian|taoluc|han.dung", normalized(display))
            else None,
            "inferred_type": None,
            "parse_status": "not_checked",
            "issue_refs": [],
        }
        counters[position], exact[position] = Counter(), True

    for position in range(1, width + 1):
        add_column(position)
    labels = [c["display_name"] for c in columns.values()]
    if len(labels) != len(set(labels)):
        issue("duplicate_header")
    if labels and any(not name.strip() for name in labels):
        issue("blank_header")
    for row in chain(early, iterator):
        raw_rows += 1
        physical_end = max(physical_end, row.line_end or row.row or 0)
        values = [c.raw_value for c in row.cells.values()]
        if not any(v is not None and v != "" for v in values):
            role = "blank"
        elif header and row.ordinal < header.ordinal:
            role = "title"
        elif header and row.ordinal == header.ordinal:
            role = "header"
        elif (
            header_signature
            and values
            and values[0] == header_first
            and tuple((p, _key(c)) for p, c in row.cells.items()) == header_signature
        ):
            role = "repeated_header"
        elif any(
            v.casefold().lstrip().startswith(("tổng ", "cộng ", "tong ", "cong "))
            and re.match(r"^(tong|cong)\s+(ngay|cong|thang|ky)\b", normalized(v).strip())
            for v in values
            if isinstance(v, str)
        ):
            role = "summary_candidate"
        else:
            role = "data_candidate"
        accounting[role] += 1
        if role != "data_candidate":
            # Preserve row/cell failures even in titles or summaries.
            for position, cell in row.cells.items():
                if cell.native_type == "error":
                    issue("native_error", row, position)
                if cell.native_type == "formula" and cell.metadata["cache_status"] != "present":
                    issue("formula_cache_" + cell.metadata["cache_status"], row, position)
            continue
        if (
            not declared
            and header
            and values
            and isinstance(values[0], str)
            and HEADER_WORDS.search(normalized(values[0]).replace("_", " "))
            and header_score(row) >= header_threshold
        ):
            structure = "ambiguous"
            issue("additional_header_candidate", row)
        extent = max(row.cells, default=0)
        if table.format == "csv" and header and extent != width:
            structure = "ambiguous"
            issue("record_width_mismatch", row, expected=width, actual=extent)
        if extent > len(columns):
            for position in range(len(columns) + 1, extent + 1):
                add_column(position)
            if header:
                structure = "ambiguous"
                issue("cells_beyond_header", row)
        for position, column in columns.items():
            cell = row.cells.get(position)
            value = cell.raw_value if cell else None
            column["row_count"] += 1
            column["native_types"][cell.native_type if cell else "absent"] += 1
            column["non_null_count"] += value is not None
            column["null_count"] += value is None
            column["absent_count"] += cell is None
            column["blank_string_count"] += isinstance(value, str) and value == ""
            is_error = cell is not None and (
                cell.native_type == "error"
                or (cell.native_type == "formula" and cell.metadata["cache_status"] == "error")
            )
            column["error_count"] += is_error
            if is_error:
                issue("native_error", row, position)
            if (
                cell
                and cell.native_type == "formula"
                and cell.metadata["cache_status"] == "missing"
            ):
                issue("formula_cache_missing", row, position)
            key = _key(cell)
            if key is None:
                continue
            counts = counters[position]
            if key in counts:
                counts[key] += 1
            elif exact[position]:
                cost = len(str(key[1]).encode()) + 200
                if (
                    len(counts) >= options.max_distinct
                    or used_bytes + cost > options.max_counter_bytes
                ):
                    exact[position] = False
                    issue("distinct_budget_exhausted", row, position)
                else:
                    counts[key] = 1
                    used_bytes += cost
            if len(column["samples"]) < 5 and not any(
                s["raw_value"] == value and s["native_type"] == cell.native_type
                for s in column["samples"]
            ):
                column["samples"].append({"raw_value": value, "native_type": cell.native_type})
        data_rows += 1
    for merged in table.metadata.get("merged_ranges", []):
        bounds = re.findall(r"[A-Z]+(\d+)", merged or "")
        if header and len(bounds) == 2 and int(bounds[0]) <= header.row <= int(bounds[1]):
            if int(bounds[0]) != int(bounds[1]):
                structure = "ambiguous"
                issue("multirow_header_candidate", header, merged_range=merged)
    keys, retained = [], {}
    for position, column in columns.items():
        counts = counters[position]
        column["native_types"] = dict(sorted(column["native_types"].items()))
        column["inferred_type"] = (
            next(iter(column["native_types"])) if len(column["native_types"]) == 1 else "mixed"
        )
        column["distinct"] = {
            "value": len(counts),
            "status": "exact" if exact[position] else "not_checked",
            "lower_bound": not exact[position],
            "rows_scanned": data_rows,
        }
        if column["semantic_role"] == "identifier_candidate" or (
            position <= len(declared) and declared[position - 1]["pk_position"]
        ):
            if len(keys) >= options.max_key_candidates:
                issue("key_candidate_budget_exhausted", column=position)
                continue
            complete = exact[position] and structure == "resolved"
            keys.append(
                {
                    "columns": [position],
                    "scope": table.source_id,
                    "status": "verified" if complete else "not_checked",
                    "null_or_blank_count": column["null_count"] + column["blank_string_count"],
                    "duplicate_count": sum(n - 1 for n in counts.values())
                    if exact[position]
                    else None,
                    "distinct_count": len(counts) if exact[position] else None,
                    "rows_scanned": data_rows,
                }
            )
            if complete:
                retained[position] = counts
    if table.format == "sqlite":
        pk = [i for i, c in enumerate(declared, 1) if c["pk_position"]]
        if len(pk) > 1:
            keys.append(
                {
                    "columns": pk,
                    "scope": table.source_id,
                    "status": "candidate",
                    "basis": "declared composite PK; not profiled as a composite",
                    "null_or_blank_count": None,
                    "duplicate_count": None,
                }
            )
    result = {
        "source_id": table.source_id,
        "source_ref": table.source_ref,
        "format": table.format,
        "read_status": "read" if raw_rows else "empty",
        "structure_status": structure,
        "header_rows": [header.row] if header else [],
        "header_candidates": [
            {"row": r.row, "score": header_score(r)} for r in early if header_score(r) >= 8
        ]
        if not declared
        else [],
        "raw_row_count": raw_rows,
        "table_boundaries": {
            "region_id": "whole_table",
            "first_record": 1 if raw_rows else None,
            "last_record": raw_rows or None,
            "internal_boundaries": "not_established"
            if structure != "resolved"
            else "single_candidate",
        },
        "physical_line_end": physical_end if table.format == "csv" else None,
        "row_accounting": dict(sorted(accounting.items())),
        "profile_scope": "data_candidate rows; structural roles are proposals, not business facts",
        "statistics_status": "exact" if all(exact.values()) else "partial",
        "columns": list(columns.values()),
        "key_candidates": keys,
        "grain": "unknown",
        "key_status": "unknown",
        "roles": ["unknown"],
        "coverage": {
            "observed_span": None,
            "available_span": None,
            "snapshot_at": None,
            "history_available": None,
            "parse_status": "not_checked",
            "uncertainties": [
                "Business date parsing and availability are not established by raw ingestion."
            ],
        },
        "metadata": table.metadata,
        "issues": issues,
        "issue_counts": dict(sorted(issue_counts.items())),
        "issues_truncated": sum(issue_counts.values()) > len(issues),
        "counter_bytes_accounted": used_bytes,
    }
    return result, retained


def candidate_relations(profiles: list[dict], values: dict, limit: int = 64) -> list[dict]:
    """Bounded exact-value checks; semantic/metric approval remains separate."""
    relations = []
    for parent in profiles:
        for key in parent.get("key_candidates", []):
            if key["status"] != "verified" or len(key["columns"]) != 1:
                continue
            position = key["columns"][0]
            parent_values = values.get(parent["source_id"], {}).get(position)
            if not parent_values:
                continue
            for child in profiles:
                if child["source_id"] == parent["source_id"]:
                    continue
                for child_key in child.get("key_candidates", []):
                    if child_key["status"] != "verified" or len(child_key["columns"]) != 1:
                        continue
                    child_position = child_key["columns"][0]
                    parent_name = normalized(parent["columns"][position - 1]["display_name"])
                    child_name = normalized(child["columns"][child_position - 1]["display_name"])
                    # Compare typed values even when identifier column names differ.
                    child_values = values.get(child["source_id"], {}).get(child_position, {})
                    overlap = parent_values.keys() & child_values.keys()
                    if not overlap and parent_name != child_name:
                        continue
                    if len(relations) >= limit:
                        return relations
                    matched = sum(child_values[v] for v in overlap)
                    count = sum(child_values.values())
                    relations.append(
                        {
                            "parent": {"source_id": parent["source_id"], "columns": [position]},
                            "child": {"source_id": child["source_id"], "columns": [child_position]},
                            "status": "verified_data"
                            if key["duplicate_count"] == 0
                            and key["null_or_blank_count"] == 0
                            and matched == count
                            and count > 0
                            else "candidate",
                            "basis": "full typed values; metric/identity checks still required",
                            "parent_unique": key["duplicate_count"] == 0,
                            "parent_null_refs": key["null_or_blank_count"],
                            "child_null_refs": child_key["null_or_blank_count"],
                            "matched_rows": matched,
                            "unmatched_rows": count - matched,
                            "match_rate": matched / count if count else None,
                            "max_parent_fanout": max(parent_values.values(), default=0),
                            "max_child_fanout": max(child_values.values(), default=0),
                        }
                    )
    return relations
