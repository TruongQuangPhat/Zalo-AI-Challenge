"""Delivery batches and payment due dates read from the shop's own books (rulebook §6).

The delivery book and the due-date book are found by their README column meanings
("đợt giao", "hạn thanh toán"); orders come from the prepared facts. Rulebook: the number
of batches in month X counts each batch of each order with a delivery day in month X, up to
the end of today, including batches of orders returned later.
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.agent.plan import _literal_windows
from src.agent.purchasing import _day
from src.agent.repair import _sections
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

ROLES = {
    "order": r"ma don(?: hang)?",
    "batch": r"dot giao.*|lan giao.*",
    "day": r"ngay giao.*",
    "item": r"ten hang.*",
    "quantity": r"so luong da doi ra don vi co ban.*",
    "unit": r"don vi co ban.*",
    "due": r"han thanh toan.*",
}
ASK = r"(?:bao nhieu|bao nhiu|bnhiu|bn|may|so|tong cong|total)"


def _books(context: BusinessContext) -> dict[str, list[dict]]:
    """Rows of the delivery book ("batch") and the due-date book ("due")."""
    sections = _sections(context.catalog["readme"])
    books: dict[str, list[dict]] = {}
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        meanings = sections.get(Path(profile["source_ref"]["file"]).name, {})
        roles: dict[str, list[int]] = {}
        for column in profile.get("columns", []):
            meaning = meanings.get(plain(column["display_name"])) or ""
            for role, pattern in ROLES.items():
                if re.fullmatch(pattern, meaning):
                    roles.setdefault(role, []).append(column["position"])
        kind = "batch" if "batch" in roles else "due" if "due" in roles else None
        if kind is None or "order" not in roles:
            continue
        if any(len(p) != 1 for p in roles.values()):
            raise ContractError("delivery_book_columns_ambiguous")
        header = set(profile.get("header_rows", []))
        source = context.source_table(profile["source_id"])
        epoch = source.metadata.get("date_system")
        for row in source.iter_rows():
            if row.row in header or (header and row.row is not None and row.row < min(header)):
                continue
            values = {}
            for role, (position,) in roles.items():
                cell = row.cells.get(position)
                raw = None if cell is None else cell.raw_value
                raw = None if raw is None or str(raw).strip() == "" else raw
                if raw is not None and role in {"day", "due"}:
                    raw = _day(raw, cell, epoch)
                values[role] = raw
            if values.get("order") is not None:
                books.setdefault(kind, []).append(values)
    return books


def _orders(context: BusinessContext) -> dict[str, tuple[str, str]]:
    with closing(context.prepared.connect()) as conn:
        return {
            str(raw).strip(): (day[:10], status)
            for raw, day, status in conn.execute(
                "SELECT raw_order_id, ordered_at, status FROM orders"
            )
        }


def _month(question: Question) -> tuple[int, int] | None:
    windows = [e for e in _literal_windows(question) if e["kind"] in {"month", "current_month"}]
    if len(windows) != 1:
        return None
    if windows[0]["kind"] == "current_month":
        today = date.fromisoformat(question.as_of_date)
        return today.year, today.month
    return windows[0]["year"], windows[0]["month"]


def _in_month(day: str, month: tuple[int, int]) -> bool:
    return day[:7] == f"{month[0]:04}-{month[1]:02}"


def answer(question: Question, context: BusinessContext, text: str):
    """(op, value, type) for a delivery-batch or due-date count, or None."""
    batches = re.search(r"\bdot\b", text) and re.search(r"\b(?:giao|cho|xe)\b", text)
    due = re.search(r"\b(?:ngay phai tra|han (?:phai )?tra|han thanh toan)\b", text)
    # "giao cho khách" is who receives the delivery, not a customer condition.
    scoped = re.sub(r"\bcho khach(?: hang)?\b", " ", text)
    if not (batches or due) or re.search(r"\b(?:nhom|khach|nhan vien|%|phan tram)\b", scoped):
        return None
    if not context.prepared or context.prepared.path is None:
        return None
    books = _books(context)
    today = question.as_of_date
    orders = _orders(context)
    if batches and "batch" in books:
        return _batches(question, text, books["batch"], orders, today)
    if due and "due" in books and not batches:
        return _due(question, text, books["due"], orders, today)
    return None


def _batch_days(rows, today) -> dict[tuple[str, str], str]:
    """(order, batch) -> delivery day, for batches delivered by the end of today."""
    days: dict[tuple[str, str], str] = {}
    for row in rows:
        if row.get("batch") is None or row.get("day") is None:
            raise ContractError("delivery_batch_incomplete")
        key = (str(row["order"]).strip(), str(row["batch"]).strip())
        if days.setdefault(key, row["day"]) != row["day"]:
            raise ContractError("delivery_batch_two_days")
    return {k: d for k, d in days.items() if d <= today}


def _batches(question, text, rows, orders, today):
    month = _month(question)
    if month is None:
        return None
    days = _batch_days(rows, today)
    ordered = re.search(r"\bdon (?:dat|dat hang)\b|\bcho (?:cac )?don\b|\bkhach dat\b", text)
    if not ordered:
        # "Số đợt giao trong tháng X": batches whose delivery day falls in month X.
        if not re.search(r"\b" + ASK + r" dot\b|\bso dot\b|\bdot (?:giao )?hang\b", text):
            return None
        # A unit ("bao" xi măng) counts goods, not batches; "bao nhiêu" asks the count.
        if re.search(r"\b(?:tro len|it nhat|mat hang|thung|bao(?! nhieu)|cay|vien|kg)\b", text):
            return None
        return "delivery_batches", sum(_in_month(d, month) for d in days.values()), "number"
    # Orders placed in month X (by the end of today) and what has been delivered for them.
    placed = {o for o, (day, _) in orders.items() if _in_month(day, month) and day <= today}
    if (least := re.search(r"\b(?:tu|it nhat|thanh) (\d|hai|ba) dot(?: tro len)?\b", text)) and (
        re.search(r"\b(?:bao nhieu|may|so) don\b", text)
    ):
        n = {"hai": 2, "ba": 3}.get(least[1]) or int(least[1])
        count: dict[str, int] = {}
        for order, _ in days:
            if order in placed:
                count[order] = count.get(order, 0) + 1
        return "delivery_orders", sum(c >= n for c in count.values()), "number"
    unit = re.search(r"\b" + ASK + r" ([a-z0-9]+)\b", text)
    if unit is None or unit[1] in {"don", "dot", "tien", "lan"}:
        return None
    named = {plain(str(r["item"])) for r in rows if r.get("item") and plain(str(r["item"])) in text}
    if len(named) != 1:
        return None
    item = next(iter(named))
    total, units = Decimal(0), set()
    for row in rows:
        order = str(row["order"]).strip()
        key = (order, str(row["batch"]).strip())
        if order in placed and key in days and plain(str(row.get("item") or "")) == item:
            if row.get("quantity") is None:
                raise ContractError("delivery_quantity_missing")
            total += Decimal(str(row["quantity"]))
            units.add(plain(str(row.get("unit") or "")))
    # The book records base units; the question must ask in that unit.
    if units != {unit[1]} or total != total.to_integral_value():
        return None
    return "delivery_quantity", int(total), "number"


def _due(question, text, rows, orders, today):
    if not re.search(r"\b(?:bao nhieu|may|so) don\b", text):
        return None
    later = re.search(r"\b(?:sau|muon hon|con sau) hom nay\b", text)
    month = None if later else _month(question)
    if not later and month is None:
        return None
    counted = set()
    for row in rows:
        order = str(row["order"]).strip()
        if row.get("due") is None or order not in orders:
            raise ContractError("due_date_orphan")
        placed, status = orders[order]
        if placed > today:
            continue  # rulebook: records after today have not happened yet
        if (later and row["due"] > today) or (month and _in_month(row["due"], month)):
            # A credit sale later cancelled or returned has no rule here: abstain.
            if status not in {"completed", "shipping"}:
                return None
            counted.add(order)
    return "due_dates", len(counted), "number"
