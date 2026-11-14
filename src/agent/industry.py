"""Order-level amounts from industry rules (phone shops: trade-in money, customer payable).

Rulebook "Thu máy cũ": the customer payable is the order money after the discount and the
trade-in; totals of payable and trade-in for a period count orders that are neither
cancelled nor returned. Columns are found by their README description; an order whose
amount is written twice with different values abstains.
"""

from __future__ import annotations

import re
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from src.data.catalog import BusinessContext
from src.data.normalization.values import ParseError, cell_value, parse_money
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

COLUMNS = {
    "trade_in": r"^(?:so )?(?:tien )?thu (?:mua )?may cu\b",
    "payable": r"\bkhach tra\b.*\bca don\b|\bkhach phai tra\b",
}


def _meanings(readme: str) -> dict[str, dict[str, str]]:
    found, section = {}, None
    for line in readme.splitlines():
        if line.startswith("#"):
            match = re.match(r"^## Cột trong `([^`]+)`", line)
            section = Path(match[1]).name if match else None
        elif section and (bullet := re.match(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$", line)):
            found.setdefault(section, {})[plain(bullet[1])] = plain(bullet[2])
    return found


def order_amounts(context: BusinessContext, kind: str) -> dict[str, Decimal] | None:
    """raw order ID -> order-level amount of ``kind`` (missing cell: 0), or None."""
    from src.data.normalization.relational import MappedSource, data_rows, relational_mapping, value
    from src.data.normalization.sales import sales_mapping

    meanings = _meanings(context.catalog["readme"])
    try:
        mapping = relational_mapping(context)
        sources = [s for s in mapping.sources if s.role == "combined"]
    except ContractError:
        sources = []
    if not sources:
        try:
            sales = sales_mapping(context)
        except ContractError:
            return None
        profiles = {t["source_id"]: t for t in context.catalog["tables"]}
        sources = [
            MappedSource(profiles[m.source_id], m.fields, "combined")
            for m in [sales, *sales.continuations]
        ]
    if not sources:
        return None
    amounts: dict[str, Decimal] = {}
    for source in sources:
        described = meanings.get(Path(source.profile["source_ref"]["file"]).name, {})
        hits = [
            c["position"]
            for c in source.profile["columns"]
            if re.search(COLUMNS[kind], described.get(plain(c["display_name"]), ""))
        ]
        if len(hits) != 1:
            return None
        position = hits[0]
        for _, row in data_rows(context, source):
            order = value(row, source, "order_id")
            if order is None:
                return None
            cell = row.cells.get(position)
            if cell is None or cell.raw_value in {None, ""}:
                amounts.setdefault(str(order), Decimal(0))
                continue
            try:
                amount = parse_money(
                    cell_value(cell),
                    "decimal_dot" if cell.native_type in {"numeric", "integer"} else None,
                )
            except ParseError:
                return None
            previous = amounts.get(str(order))
            if previous not in {None, Decimal(0)} and previous != amount:
                return None  # two different order totals: never pick one
            amounts[str(order)] = amount
    return amounts


ADJUSTMENTS = {
    "voucher": (r"\b(?:voucher|vau cho)\b", r"\bvoucher\b"),
    "points": (r"\b(?:dung|tru|doi) (?:\d+ )?diem\b|\bdiem thuong\b", r"\bdiem\b"),
    "shipping": (r"\bphi (?:ship|giao(?: hang)?)\b", r"\bphi (?:ship|giao)\b"),
}


def adjustment_totals(context: BusinessContext) -> dict[str, dict[str, Decimal]] | None:
    """raw order ID -> {kind: positive amount} of its voucher/points/shipping lines."""
    from src.data.normalization.relational import MappedSource, data_rows, relational_mapping
    from src.data.normalization.relational import value as cell_of
    from src.data.normalization.sales import sales_mapping

    try:
        sources = [s for s in relational_mapping(context).sources if s.role == "combined"]
    except ContractError:
        sources = []
    if not sources:
        try:
            sales = sales_mapping(context)
        except ContractError:
            return None
        profiles = {t["source_id"]: t for t in context.catalog["tables"]}
        sources = [
            MappedSource(profiles[m.source_id], m.fields, "combined")
            for m in [sales, *sales.continuations]
        ]
    totals: dict[str, dict[str, Decimal]] = {}
    for source in sources:
        if not {"order_id", "item", "amount"} <= source.fields.keys():
            return None
        for _, row in data_rows(context, source):
            item = plain(str(cell_of(row, source, "item", "")))
            kinds = [k for k, (_, cue) in ADJUSTMENTS.items() if re.search(cue, item)]
            if len(kinds) != 1:
                continue
            order = cell_of(row, source, "order_id")
            cell = row.cells.get(source.fields["amount"])
            if order is None or cell is None:
                return None
            try:
                amount = parse_money(
                    cell_value(cell),
                    "decimal_dot" if cell.native_type in {"numeric", "integer"} else None,
                )
            except ParseError:
                return None
            entry = totals.setdefault(str(order), {})
            entry[kinds[0]] = entry.get(kinds[0], Decimal(0)) + abs(amount)
    return totals


def registrations(context: BusinessContext) -> list[str] | None:
    """Registration dates (ISO) of every member in the table README describes with a
    "ngày đăng ký" column; None unless exactly one such column exists and all parse."""
    from src.data.normalization.values import parse_date

    meanings = _meanings(context.catalog["readme"])
    found = []
    for profile in context.catalog["tables"]:
        file = Path(profile["source_ref"]["file"]).name
        described = {
            **meanings.get(file, {}),
            **meanings.get(f"{file}, sheet `{profile['source_ref']['table']}`", {}),
        }
        for column in profile.get("columns", []):
            label = plain(column["display_name"])
            if re.search(r"\bngay dang ky\b", label + " " + described.get(label, "")):
                found.append((profile, column["position"]))
    if len(found) != 1:
        return None
    profile, position = found[0]
    table = context.source_table(profile["source_id"])
    system = table.metadata.get("date_system")
    header = set(profile.get("header_rows", []))
    days = []
    for row in table.iter_rows():
        if row.row in header or (header and row.row is not None and row.row < min(header)):
            continue
        cell = row.cells.get(position)
        if not row.cells or cell is None or cell.raw_value in {None, ""}:
            continue
        raw = str(cell.raw_value).strip()
        try:
            day = parse_date(
                raw, date_system=system if re.fullmatch(r"\d+(?:\.\d+)?", raw) else None
            )
        except ParseError:
            return None
        days.append(day.isoformat())
    return days


def answer(question: Question, context: BusinessContext, text: str):
    """Trade-in or payable total of a period, or None."""
    from src.agent.entities import mentioned_entity
    from src.agent.plan import _literal_windows
    from src.semantics.answerability import preflight

    if re.search(r"%|\b(?:ti le|chiem|trung binh|binh quan|nhat)\b", text):
        return None
    if mentioned_entity(context, question):
        return None  # one customer's or product's amount is a different question
    if re.search(r"\bthu (?:mua |lai )?may cu\b", text) and re.search(
        r"\b(?:tien|tong|cong lai|het)\b", text
    ):
        kind = "trade_in"
    elif re.search(r"\bkhach phai tra\b", text):
        kind = "payable"
    elif re.search(r"\b(?:thanh vien moi|khach moi dang ky|dang ky thanh vien)\b", text):
        kind = "new_members"
    else:
        found = [k for k, (cue, _) in ADJUSTMENTS.items() if re.search(cue, text)]
        if len(found) != 1 or re.search(r"\b(?:don|hoa don) (?:co|nao)\b|\bso don\b", text):
            return None
        kind = found[0]
    rules = context.rules.rules if context.rules is not None else {}
    rule = {"new_members": "membership_new"}.get(
        kind, "invoice_adjustments" if kind in ADJUSTMENTS else "phone_tradein"
    )
    if rule not in rules or context.prepared is None:
        return None
    expressions = _literal_windows(question)
    if len(expressions) != 1:
        return None
    check = preflight(context, "revenue", expressions[0], as_of_date=question.as_of_date)
    if check["decision"] == "unanswerable":
        return "industry_" + kind, check["reason"], "unanswerable"
    window = check.get("effective_window")
    if check["decision"] != "eligible_to_execute" or not window:
        return None
    if kind == "new_members":
        days = registrations(context)
        if days is None:
            return None
        return (
            "industry_new_members",
            sum(window["start"] <= d <= window["end"] for d in days),
            "number",
        )
    if kind in ADJUSTMENTS:
        lines = adjustment_totals(context)
        if lines is None:
            return None
        amounts = {o: v.get(kind, Decimal(0)) for o, v in lines.items()}
    else:
        amounts = order_amounts(context, kind)
    if amounts is None:
        return None
    with closing(context.prepared.connect()) as conn:
        orders = conn.execute(
            "SELECT raw_order_id,status FROM orders WHERE substr(ordered_at,1,10) BETWEEN ? AND ?",
            [window["start"], window["end"]],
        ).fetchall()
    if not orders or (kind not in ADJUSTMENTS and any(str(o) not in amounts for o, _ in orders)):
        return None
    total = sum(
        (
            amounts.get(str(o), Decimal(0))
            for o, status in orders
            if status in {"completed", "shipping"}
        ),
        Decimal(0),
    )
    if total != total.to_integral_value():
        return None
    return "industry_" + kind, int(total), "number"
