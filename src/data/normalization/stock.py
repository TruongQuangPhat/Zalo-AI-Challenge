"""Exact single-product inventory snapshots with explicit README date/unit authority.

Only one resolved XLSX table with four POS columns is supported. This does not
establish transaction-history completeness or reconstruct historical inventory.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass

from src.data.cache import fingerprint
from src.data.catalog import BusinessContext
from src.data.normalization.values import cell_value, parse_date, parse_number
from src.jsonio import ContractError, dumps
from src.semantics.rulebook import plain

METRIC = "inventory_on_hand"
FIELDS = {
    "product_code": "ma hang",
    "product_name": "ten hang",
    "base_unit": "dvt",
    "quantity": "ton cuoi ky",
}


def identity_text(text: str) -> str:
    """Ignore case/punctuation/spacing, preserve Vietnamese accents and all digits."""
    return "".join(c for c in unicodedata.normalize("NFC", text).casefold() if c.isalnum())


def _alias_pattern(text: str) -> str:
    return r"(?<!\w)" + r"[\W_]*".join(re.escape(c) for c in identity_text(text)) + r"(?!\w)"


@dataclass(frozen=True)
class StockMapping:
    source_id: str
    fields: dict[str, int]
    snapshot_at: str
    source_refs: list[dict]


def stock_mapping(context: BusinessContext) -> StockMapping:
    if not context.catalog["read_complete"]:
        raise ContractError("snapshot source read is incomplete")
    lines = context.catalog["readme"].splitlines()
    declarations = [(i, line) for i, line in enumerate(lines, 1) if "ton cuoi ky" in plain(line)]
    expected = "ton kho: ton cuoi ky la ton cuoi ngay cuoi co du lieu, theo don vi co ban"
    if len(declarations) != 1 or plain(declarations[0][1]).lstrip("- ") != expected:
        raise ContractError("README does not unambiguously declare base-unit end-date snapshot")
    ends = [
        (i, line, re.fullmatch(r"-?\s*du lieu co den het (\d{1,2}/\d{1,2}/\d{4})", plain(line)))
        for i, line in enumerate(lines, 1)
    ]
    ends = [(i, line, match[1]) for i, line, match in ends if match]
    if len(ends) != 1:
        raise ContractError("snapshot requires one explicit README end date")
    snapshot_at = parse_date(ends[0][2]).isoformat()
    candidates = []
    for table in context.catalog["tables"]:
        if table["format"] != "xlsx" or table["structure_status"] != "resolved":
            continue
        columns = {plain(c["display_name"]): c["position"] for c in table["columns"]}
        if len(table["columns"]) == 4 and set(columns) == set(FIELDS.values()):
            candidates.append((table["source_id"], {k: columns[v] for k, v in FIELDS.items()}))
    if len(candidates) != 1:
        raise ContractError("requires exactly one resolved four-column XLSX inventory snapshot")
    ref = {
        "split": context.catalog["split"],
        "file": f"data/{context.catalog['business_id']}/README.md",
        "sha256": context.catalog["readme_hash"],
    }
    refs = [
        {**ref, "line": i, "quote": line} for i, line in [declarations[0], (ends[0][0], ends[0][1])]
    ]
    return StockMapping(*candidates[0], snapshot_at, refs)


def build_stock(
    context: BusinessContext, connection: sqlite3.Connection, mapping: StockMapping
) -> dict:
    table = context.source_table(mapping.source_id)
    profile = next(t for t in context.catalog["tables"] if t["source_id"] == mapping.source_id)
    accounting: Counter = Counter()
    issue_counts: Counter = Counter()
    header_seen = False
    seen_names: set[str] = set()
    seen_codes: set[str] = set()
    for row in table.iter_rows():
        ref = row.reference(table)
        values = {k: row.cells.get(pos) for k, pos in mapping.fields.items()}
        if not any(c.raw_value not in {None, ""} for c in row.cells.values()):
            accounting["blank"] += 1
            continue
        if all(c is not None and plain(str(c.raw_value)) == FIELDS[k] for k, c in values.items()):
            header_seen = True
            accounting["header"] += 1
            continue
        try:
            if not header_seen:
                raise ContractError("data precedes snapshot header")
            text = {k: cell_value(c) for k, c in values.items() if k != "quantity"}
            if any(not isinstance(v, str) or not identity_text(v) for v in text.values()):
                raise ContractError("snapshot identity/unit must be nonempty source text")
            if any(len(v) > 256 for v in text.values()):
                raise ContractError("snapshot identity/unit exceeds supported text budget")
            code, name = identity_text(text["product_code"]), identity_text(text["product_name"])
            if code in seen_codes or name in seen_names:
                raise ContractError("duplicate or ambiguous snapshot identity")
            amount = values["quantity"]
            if amount is None or amount.native_type != "numeric":
                raise ContractError("snapshot quantity requires an exact native XLSX number")
            quantity = parse_number(cell_value(amount), "decimal_dot")
            connection.execute(
                "INSERT INTO inventory_snapshot VALUES (?,?,?,?,?,?)",
                (
                    text["product_code"],
                    text["product_name"],
                    text["base_unit"],
                    str(quantity),
                    mapping.snapshot_at,
                    dumps(ref),
                ),
            )
            seen_codes.add(code)
            seen_names.add(name)
            accounting["prepared"] += 1
        except (ContractError, ValueError, sqlite3.IntegrityError) as exc:
            issue_counts[str(exc)] += 1
            connection.execute(
                "INSERT INTO issues VALUES (?,?)",
                (
                    fingerprint(ref),
                    dumps(
                        {
                            "code": "snapshot_row_invalid",
                            "severity": "blocking",
                            "source_ref": ref,
                            "reason": str(exc),
                            "affected_metrics": [METRIC],
                        }
                    ),
                ),
            )
            accounting["quarantined"] += 1
    if sum(accounting.values()) != profile["raw_row_count"] or not header_seen:
        raise ContractError("snapshot row accounting/header reconciliation failed")
    return {
        "status": "blocked" if issue_counts else "ready",
        "shape": "inventory_snapshot",
        "source_id": mapping.source_id,
        "mapping": {
            "fields": mapping.fields,
            "authority": {
                "readme_hash": context.catalog["readme_hash"],
                "rulebook_hash": context.catalog["rulebook_hash"],
            },
        },
        "snapshot": {
            "fact_type": "inventory_snapshot",
            "time_axis": "snapshot_at",
            "snapshot_at": mapping.snapshot_at,
            "unit_basis": "source_base_unit",
            "source_refs": mapping.source_refs + [table.source_ref],
        },
        "counts": {
            "inventory_snapshot": accounting["prepared"],
            "issues": sum(issue_counts.values()),
        },
        "row_accounting": dict(accounting),
        "issue_counts": dict(issue_counts),
        "blocking_metrics": [METRIC] if issue_counts else [],
        "coverage": {
            "available_span": None,
            "observed_span": None,
            "parse_status": "not_applicable",
            "available_end_hint": None,
        },
        "metric_execution": "separate_query_time_executor",
    }


def _mentioned_records(context: BusinessContext, question: str) -> list[dict]:
    q = unicodedata.normalize("NFC", question).casefold()
    return [
        r
        for r in context.prepared.iter_records("inventory_snapshot")
        if re.search(_alias_pattern(r["product_name"]), q)
        or re.search(_alias_pattern(r["product_code"]), q)
    ]


def stock_candidates(context: BusinessContext, question: str) -> list[dict]:
    """Bounded source identities for planning; never expose inventory quantities."""
    if not context.prepared or context.prepared.summary.get("shape") != "inventory_snapshot":
        return []
    return [
        {k: row[k] for k in ["product_code", "product_name", "base_unit"]}
        for row in _mentioned_records(context, question)[:8]
    ]


def resolve_stock_entity(context: BusinessContext, question: str, entity: str, unit: str) -> dict:
    """Require a unique accent-preserving source identity and an explicit base unit.

    Do not use values, fuzzy similarity, deaccenting, or an LLM choice to resolve
    collisions. Requested unit must occur outside the product's own name.
    """
    if context.prepared is None or context.prepared.path is None:
        raise ContractError("snapshot_preparation_unsupported")
    q = unicodedata.normalize("NFC", question).casefold()
    mentioned = _mentioned_records(context, question)
    if len(mentioned) != 1:
        raise ContractError("snapshot_entity_missing_or_ambiguous")
    row = mentioned[0]
    if identity_text(entity) not in {
        identity_text(row["product_code"]),
        identity_text(row["product_name"]),
    }:
        raise ContractError("snapshot_entity_not_grounded_in_question")
    remaining = re.sub(_alias_pattern(row["product_name"]), " ", q)
    if identity_text(unit) != identity_text(row["base_unit"]) or not re.search(
        _alias_pattern(unit), remaining
    ):
        raise ContractError("snapshot_unit_not_explicit_base_unit")
    return row
