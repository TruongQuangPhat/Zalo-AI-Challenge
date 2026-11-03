"""README-driven preparation of one CSV or a narrowly mapped XLSX order-line sheet."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from src.data.cache import fingerprint
from src.data.ingestion import Cell, SourceRow
from src.data.normalization.values import (
    ParseError,
    cell_value,
    exact_add,
    normalize_status,
    number_evidence,
    parse_date,
    parse_datetime,
    parse_money,
    parse_number,
)
from src.data.profiling import normalized
from src.jsonio import ContractError, dumps

if TYPE_CHECKING:
    from src.data.catalog import BusinessContext


@dataclass(frozen=True)
class SalesMapping:
    source_id: str
    fields: dict[str, int]
    inherit_headers: bool
    convention: str | None
    negative_returns: bool
    shape: str = "csv_order_lines"
    unused_source_ids: tuple[str, ...] = ()


def sales_mapping(context: BusinessContext) -> SalesMapping:
    catalog = context.catalog
    if not catalog["read_complete"]:
        raise ContractError("requires completely read sources")
    readme = catalog["readme"]
    text = normalized(readme)
    header_only = bool(re.search(r"moi dong mot don hang.*khong co chi tiet mat hang", text))
    if not header_only and not re.search(
        r"(?:mot dong hang|moi dong mot san pham|one order line per row)", text
    ):
        raise ContractError("README does not establish order-line grain")
    if context.rules is None or context.rules.require("dedup").operator != "exact_within_order":
        raise ContractError("runtime rulebook does not establish exact within-order deduplication")
    meanings = {
        "order_id": {"ma don", "ma hoa don", "order id"},
        "ordered_at": {"ngay ban", "ngay dat hang", "order date"},
        "status": {"trang thai don", "trang thai don hang", "order status"},
        "amount": {"thanh tien sau giam", "line net amount"},
        "item": {"ten hang", "ten san pham", "product name"},
        "quantity": {"so luong", "quantity"},
        "returned_at": {"ngay tra hang", "return date"},
        "customer": {"ten khach", "ten khach hang", "customer name"},
        "note": {"ghi chu don", "order note"},
    }

    def descriptions(section: str) -> dict[str, str]:
        result = {}
        for label, meaning in re.findall(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$", section, re.M):
            key = normalized(label).strip()
            if key in result:
                raise ContractError(
                    "duplicate README field definitions require explicit resolution"
                )
            result[key] = normalized(meaning).strip().rstrip(".")
        return result

    def mapped_fields(table: dict, definitions: dict) -> dict:
        fields = {}
        for field, definitions_for_field in meanings.items():
            candidates = [
                column["position"]
                for column in table["columns"]
                if definitions.get(normalized(column["display_name"]).strip())
                in definitions_for_field
            ]
            if len(candidates) > 1:
                raise ContractError(f"ambiguous README mapping for {field}")
            if candidates:
                fields[field] = candidates[0]
        return fields

    tables = catalog["tables"]
    unused = ()
    xlsx = False
    if len(tables) == 1 and tables[0]["format"] == "csv":
        table = tables[0]
        fields = mapped_fields(table, descriptions(readme))
    else:
        # Bind column meanings to the exact filename declared by README. Never mix
        # similarly named columns from the conversion lookup into transaction facts.
        sections = {}
        for filename, section in re.findall(
            r"^## Cột trong `([^`]+)`\s*\n(.*?)(?=^## |\Z)", readme, re.M | re.S
        ):
            if filename in sections:
                raise ContractError("duplicate README source sections")
            sections[filename] = descriptions(section)
        candidates, lookups = [], []
        for candidate in tables:
            definitions = sections.get(Path(candidate["source_ref"]["file"]).name, {})
            fields_for_table = mapped_fields(candidate, definitions)
            if {"order_id", "ordered_at", "status"} & fields_for_table.keys():
                candidates.append((candidate, fields_for_table))
            elif (
                candidate["format"] == "xlsx"
                and candidate["structure_status"] == "resolved"
                and definitions
                and all(
                    normalized(c["display_name"]).strip() in definitions
                    for c in candidate["columns"]
                )
                and any(v.startswith("he so: so don vi co ban") for v in definitions.values())
                and "don vi co ban" in definitions.values()
            ):
                lookups.append(candidate["source_id"])
            else:
                raise ContractError("unclassified source cannot be omitted from order preparation")
        if len(candidates) != 1 or header_only:
            raise ContractError("only one explicitly mapped XLSX order-line sheet is enabled")
        table, fields = candidates[0]
        if table["format"] != "xlsx" or "thong tin don lap lai o moi dong" not in text:
            raise ContractError("XLSX requires README repeated order headers on each product row")
        if sum(t["source_ref"]["file"] == table["source_ref"]["file"] for t in tables) != 1:
            raise ContractError("multi-sheet order normalization is not enabled")
        unused = tuple(lookups)
        xlsx = True
    if table["structure_status"] != "resolved":
        raise ContractError("requires a resolved order source")
    required = {"order_id", "ordered_at", "status"}
    if not header_only:
        required.update({"amount", "item"})
    if not required <= fields.keys():
        raise ContractError("README lacks unambiguous order/date/status/line amount/item mappings")
    conventions = {
        "comma_grouped": "dau phay nhom nghin",
        "dot_grouped": "dau cham nhom nghin",
        "decimal_dot": "dau cham thap phan, khong nhom nghin",
        "decimal_comma": "dau phay thap phan, khong nhom nghin",
    }
    declared = [style for style, phrase in conventions.items() if phrase in text]
    if len(declared) > 1:
        raise ContractError("conflicting numeric conventions in README")
    inheritance = "thong tin don chi ghi o dong dau nhom" in text
    if inheritance:
        context.rules.require("header_inheritance")
    negative_returns = "so luong am la tra hang" in text
    if negative_returns and "quantity" not in fields:
        raise ContractError("negative-return convention has no verified quantity field")
    if negative_returns and context.rules.require("return_policy").operator != "whole_order_return":
        raise ContractError("runtime return policy is not supported")
    return SalesMapping(
        table["source_id"],
        fields,
        inheritance,
        next(iter(declared), None),
        negative_returns,
        "xlsx_order_lines" if xlsx else "csv_order_headers" if header_only else "csv_order_lines",
        unused,
    )


def _signature(cells: dict[int, Cell]) -> str:
    # Coordinates/styles locate an export copy but do not make it a different business row.
    return dumps(
        [
            [position, cell.native_type, cell.raw_value, cell.metadata.get("formula")]
            for position, cell in sorted(cells.items())
        ]
    )


def _summary(row: SourceRow, mapping: SalesMapping) -> bool:
    candidates = (
        [row.cells.get(mapping.fields["item"])]
        if "item" in mapping.fields and mapping.shape != "xlsx_order_lines"
        else row.cells.values()
    )
    return any(
        cell
        and re.match(
            r"^(tong|cong)\s+(ngay|cong|thang|ky)\b", normalized(str(cell.raw_value)).strip()
        )
        for cell in candidates
    )


def _row_role(row: SourceRow, header: SourceRow, mapping: SalesMapping) -> str:
    if not any(c.raw_value is not None and c.raw_value != "" for c in row.cells.values()):
        return "blank"
    if row.ordinal < header.ordinal:
        return "title"
    first = min(header.cells)
    if row.cells.get(first) == header.cells[first] and _signature(row.cells) == _signature(
        header.cells
    ):
        return "header" if row.ordinal == header.ordinal else "repeated_header"
    if _summary(row, mapping):
        # A summary sharing an order ID could be a named item; do not silently drop it.
        return (
            "unknown"
            if any(
                row.cells.get(mapping.fields[f]) and row.cells[mapping.fields[f]].raw_value
                for f in ["order_id", "ordered_at", "status"]
            )
            else "summary"
        )
    return "data"


def _time_value(value) -> str:
    parsed = parse_datetime(value)
    token = str(value).strip()
    has_time = re.search(r"\d{1,2}:\d{2}", token) or re.match(r"\d{4}-\d{2}-\d{2}[T ]", token)
    return parsed.isoformat() if has_time else parsed.date().isoformat()


def build_sales(
    context: BusinessContext, connection: sqlite3.Connection, mapping: SalesMapping
) -> dict:
    profile = next(t for t in context.catalog["tables"] if t["source_id"] == mapping.source_id)
    table = context.source_table(mapping.source_id)
    header = next((r for r in table.iter_rows() if r.row in profile["header_rows"]), None)
    if header is None:
        raise ContractError("resolved source has no accessible header")
    evidence, evidence_refs = set(), []
    for row in table.iter_rows():
        role = _row_role(row, header, mapping)
        if (
            role == "data" or (mapping.shape == "xlsx_order_lines" and role == "summary")
        ) and "amount" in mapping.fields:
            cell = row.cells.get(mapping.fields["amount"])
            style = (
                number_evidence(str(cell.raw_value))
                if cell and cell.native_type != "numeric"
                else None
            )
            if style:
                evidence.add(style)
                if len(evidence_refs) < 5:
                    evidence_refs.append(
                        {
                            "style": style,
                            "row_role": role,
                            "raw_value": cell.raw_value,
                            "source_ref": row.reference(table, mapping.fields["amount"]),
                        }
                    )
    convention = mapping.convention
    convention_conflict = len(evidence) > 1 or bool(convention and evidence - {convention})
    if convention is None and len(evidence) == 1:
        convention = next(iter(evidence))
    issue_counts, accounting = Counter(), Counter()
    blocked = set()

    def issue(code: str, ref: dict, raw, fields: list[str], severity: str = "blocking"):
        issue_counts[code] += 1
        if severity == "blocking":
            blocked.update(fields)
        record = {
            "code": code,
            "source_ref": ref,
            "raw_value": raw,
            "affected_metrics": fields,
            "severity": severity,
        }
        connection.execute("INSERT INTO issues VALUES (?, ?)", (fingerprint(record), dumps(record)))

    if convention_conflict:
        issue("number_convention_conflict", table.source_ref, sorted(evidence), ["revenue"])
    group: dict[str, Cell] = {}
    header_fields = [
        f for f in ["order_id", "ordered_at", "status", "customer", "note"] if f in mapping.fields
    ]
    for row in table.iter_rows():
        reference = row.reference(table)
        raw_id = row.identity(table)
        role = _row_role(row, header, mapping)
        methods = ["read_raw"]
        fact_key, order_key = None, None
        if role != "data":
            group = {}
            disposition = role
            if role == "unknown":
                issue("row_role_unknown", reference, None, ["revenue", "order_count"])
        else:
            cells = dict(row.cells)
            new_id = cells.get(mapping.fields["order_id"])
            if new_id and new_id.raw_value not in {None, ""}:
                if not group or new_id.raw_value != group["order_id"].raw_value:
                    group = {}
            if mapping.inherit_headers:
                for field in header_fields:
                    position = mapping.fields[field]
                    if (
                        position not in cells or cells[position].raw_value in {None, ""}
                    ) and field in group:
                        cells[position] = group[field]
                        methods.append("inherit:" + field)
            line_role, event_at = "sale_item", None
            if "quantity" in mapping.fields:
                qty = cells.get(mapping.fields["quantity"])
                if mapping.negative_returns or (
                    qty and str(qty.raw_value).lstrip().startswith("-")
                ):
                    try:
                        quantity = parse_number(cell_value(qty))
                        if not mapping.negative_returns:
                            raise ParseError(
                                "return_parser_unsupported", "negative quantity role unknown"
                            )
                        if quantity < 0:
                            line_role = "return_item"
                            methods.append("return_from_source_quantity")
                            if "returned_at" in mapping.fields:
                                event_at = _time_value(
                                    cell_value(cells.get(mapping.fields["returned_at"]))
                                )
                    except ParseError as exc:
                        line_role = "unknown"
                        issue(
                            exc.code,
                            reference,
                            qty.raw_value if qty else None,
                            ["revenue", "order_count"],
                        )
            parsed, valid_headers = {}, True
            for field, parser in [
                ("order_id", str),
                ("ordered_at", _time_value),
                ("status", normalize_status),
            ]:
                try:
                    value = cell_value(cells.get(mapping.fields[field]))
                    parsed[field] = parser(value)
                    if field == "order_id" and (not isinstance(value, str) or not value.strip()):
                        raise ParseError("identifier_type", "order IDs must be nonempty text")
                except ParseError as exc:
                    parsed[field] = None
                    valid_headers = False
                    issue(
                        exc.code,
                        row.reference(table, mapping.fields[field]),
                        cells.get(mapping.fields[field]).raw_value
                        if cells.get(mapping.fields[field])
                        else None,
                        ["revenue", "order_count"],
                    )
            if valid_headers:
                group = {
                    f: cells[mapping.fields[f]] for f in header_fields if mapping.fields[f] in cells
                }
                ordered_at = parsed["ordered_at"]
                order_key = fingerprint(
                    {
                        "split": table.source_ref["split"],
                        "business": table.source_ref["business_id"],
                        "kind": "order",
                        "id": parsed["order_id"],
                    }
                )
                existing = connection.execute(
                    "SELECT ordered_at,status FROM orders WHERE order_key=?", (order_key,)
                ).fetchone()
                has_return = (
                    line_role == "return_item"
                    or connection.execute(
                        "SELECT 1 FROM order_lines WHERE order_key=? "
                        "AND row_role='return_item' LIMIT 1",
                        (order_key,),
                    ).fetchone()
                    is not None
                )
                conflict = existing and (
                    existing[0] != ordered_at
                    or (
                        existing[1] != parsed["status"]
                        and not (has_return and "cancelled" not in {existing[1], parsed["status"]})
                    )
                )
                if conflict or (has_return and parsed["status"] == "cancelled"):
                    connection.execute(
                        "UPDATE orders SET mapping_status='ambiguous' WHERE order_key=?",
                        (order_key,),
                    )
                    issue(
                        "order_conflict", reference, parsed["order_id"], ["revenue", "order_count"]
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?)",
                    (order_key, parsed["order_id"], ordered_at, parsed["status"], "resolved"),
                )
                if has_return:
                    connection.execute(
                        "UPDATE orders SET status='returned' WHERE order_key=?", (order_key,)
                    )
            else:
                group = {}
            if mapping.shape == "csv_order_headers":
                disposition = "kept" if valid_headers else "quarantined"
                methods.extend(["parse_date", "normalize_status", "create_order"])
            else:
                amount = None
                try:
                    if convention_conflict:
                        raise ParseError(
                            "number_convention_conflict", "source conventions conflict"
                        )
                    amount = parse_money(
                        cell_value(cells.get(mapping.fields["amount"])),
                        "decimal_dot"
                        if cells.get(mapping.fields["amount"])
                        and cells[mapping.fields["amount"]].native_type == "numeric"
                        else convention,
                    )
                    if amount < 0 and line_role != "return_item":
                        line_role = "unknown"
                        raise ParseError(
                            "row_role_unknown", "negative amounts require a role parser"
                        )
                except ParseError as exc:
                    issue(
                        exc.code,
                        row.reference(table, mapping.fields["amount"]),
                        cells.get(mapping.fields["amount"]).raw_value
                        if cells.get(mapping.fields["amount"])
                        else None,
                        ["revenue", "order_count"]
                        if exc.code == "row_role_unknown"
                        else ["revenue"],
                    )
                item = cells.get(mapping.fields["item"])
                item_text = normalized(str(item.raw_value)).strip() if item else ""
                if not item_text or re.search(
                    r"voucher|diem|phi (?:ship|phuc vu)"
                    + ("|topping" if mapping.shape != "xlsx_order_lines" else ""),
                    item_text,
                ):
                    line_role = "unknown"
                    issue(
                        "row_role_unknown",
                        reference,
                        item.raw_value if item else None,
                        ["revenue", "order_count"],
                    )
                for position, cell in row.cells.items():
                    if position not in mapping.fields.values():
                        try:
                            cell_value(cell)
                        except ParseError as exc:
                            if exc.code != "source_missing":
                                issue(
                                    exc.code,
                                    row.reference(table, position),
                                    cell.raw_value,
                                    [],
                                    "warning",
                                )
                if valid_headers:
                    source_values = _signature(cells)
                    fact_key = fingerprint({"order_key": order_key, "source_values": source_values})
                    duplicate = connection.execute(
                        "SELECT source_values FROM order_lines WHERE line_key=?", (fact_key,)
                    ).fetchone()
                    if duplicate and duplicate[0] != source_values:
                        raise ContractError("line identity collision")
                    disposition = "duplicate" if duplicate else "kept"
                    if not duplicate:
                        connection.execute(
                            "INSERT INTO order_lines VALUES (?,?,?,?,?,?)",
                            (
                                fact_key,
                                order_key,
                                str(amount) if amount is not None else None,
                                line_role,
                                source_values,
                                event_at,
                            ),
                        )
                    methods.extend(
                        [
                            "parse_date",
                            "normalize_status",
                            "parse_money" if amount is not None else "parse_money_failed",
                            "exact_duplicate" if duplicate else "create_fact",
                        ]
                    )
                else:
                    disposition = "quarantined"
        accounting[disposition] += 1
        connection.execute(
            "INSERT INTO lineage VALUES (?,?,?,?,?,?,?)",
            (
                raw_id,
                mapping.source_id,
                row.ordinal,
                disposition,
                fact_key,
                dumps(reference),
                dumps(methods),
            ),
        )
    if sum(accounting.values()) != profile["raw_row_count"]:
        raise ContractError("source-to-canonical row accounting failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ContractError("canonical foreign-key reconciliation failed")
    totals = {}
    for status, amount in connection.execute(
        "SELECT o.status,l.net_amount FROM order_lines l JOIN orders o USING(order_key) "
        "WHERE l.row_role='sale_item' ORDER BY l.line_key"
    ):
        if amount is not None:
            totals[status] = exact_add(totals.get(status, Decimal(0)), Decimal(amount))
    observed = connection.execute("SELECT MIN(ordered_at),MAX(ordered_at) FROM orders").fetchone()
    readme = context.catalog["readme"]
    complete_span = re.search(
        r"Dữ liệu đầy đủ từ (\d{1,2}/\d{1,2}/\d{4}) đến hết (\d{1,2}/\d{1,2}/\d{4})", readme
    )
    available = None
    observed_span = {"start": observed[0][:10], "end": observed[1][:10]} if observed[0] else None
    if complete_span:
        start, end = (parse_date(complete_span[i]).isoformat() for i in [1, 2])
        if start > end or (observed[0] and (observed[0][:10] < start or observed[1][:10] > end)):
            issue(
                "coverage_conflict", table.source_ref, complete_span[0], ["revenue", "order_count"]
            )
        else:
            available = {"start": start, "end": end}
    elif "order_count" not in blocked:
        available = observed_span
    counts = {
        name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ["orders", "order_lines", "lineage", "issues"]
    }
    return {
        "status": "blocked" if blocked else "ready",
        "shape": mapping.shape,
        "source_id": mapping.source_id,
        "unused_source_ids": list(mapping.unused_source_ids),
        "mapping": {
            "fields": mapping.fields,
            "inherit_headers": mapping.inherit_headers,
            "negative_returns": mapping.negative_returns,
            "money_convention": convention,
            "numeric_evidence": evidence_refs,
            "authority": {
                "readme_hash": context.catalog["readme_hash"],
                "rulebook_hash": context.catalog["rulebook_hash"],
            },
        },
        "counts": counts,
        "row_accounting": dict(sorted(accounting.items())),
        "issue_counts": dict(sorted(issue_counts.items())),
        "blocking_metrics": sorted(blocked),
        "diagnostic_line_totals_by_status": totals,
        "diagnostic_totals_complete": mapping.shape in {"csv_order_lines", "xlsx_order_lines"}
        and "revenue" not in blocked,
        "coverage": {
            "fact_type": "orders",
            "time_axis": "ordered_at",
            "observed_span": observed_span,
            "available_span": available,
            "available_end_hint": context.catalog["coverage"]["available_end_hint"],
            "availability_basis": [complete_span[0]]
            if complete_span
            else (["observed_order_dates"] if available else []),
            "parse_status": "partial" if "order_count" in blocked else "complete",
            "uncertainties": []
            if complete_span and available
            else [
                "Export extent accepted as coverage; missing interior records cannot be detected."
            ]
            if available
            else ["available start/completeness unknown"],
        },
        "metric_execution": "separate_query_time_executor",
    }
