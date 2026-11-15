"""Debt written in the order note ("nợ 130k", "trả 259k, nợ 449.500đ", "nợ cả đơn").

Only for a shop whose README says the note states each order's debt as of the last day of
the data; questions are answered only at that day. Rulebook §4: orders not cancelled
(returned orders still count); walk-in orders count in the total but are no customer;
debt age counts days from the order day.
"""

from __future__ import annotations

import re
from collections import defaultdict
from contextlib import closing
from datetime import date
from decimal import Decimal

from src.agent.books import table
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

PAID = r"\b(?:da tra du|tra het|tra du|ck du|chuyen khoan du|da thanh toan|tra het roi)\b"
OWED = (
    r"\b(?:no|thieu lai|con thieu|ghi so|ghi no|chua tra, ghi no)\s+"
    r"(\d[\d.,]*\s*(?:tr\d*|trieu|k|nghin|ngan|d)?)"
)


def _amount(token: str) -> Decimal:
    token = token.replace(" ", "")
    if m := re.fullmatch(r"(\d+)tr(\d{1,3})", token):
        return Decimal(m[1]) * 1_000_000 + Decimal("0." + m[2]) * 1_000_000
    if m := re.fullmatch(r"([\d.,]+)(tr|trieu|k|nghin|ngan|d)?", token):
        digits, unit = m[1], m[2]
        scale = {"tr": 1_000_000, "trieu": 1_000_000, "k": 1000, "nghin": 1000, "ngan": 1000}
        if unit in scale:
            return Decimal(digits.replace(",", ".")) * scale[unit]
        return Decimal(re.sub(r"[.,]", "", digits))
    raise ContractError("note_debt_amount")


def note_debts(context: BusinessContext) -> dict[str, Decimal]:
    """Order -> unpaid money at the end of the data, from the notes of debt orders."""
    readme = plain(context.catalog.get("readme") or "")
    if not re.search(
        r"\bno\b.*\bnote\b.*\bngay cuoi du lieu\b|\bghi chu no\b.*\bngay cuoi\b", readme
    ):
        return {}
    owed: dict[str, Decimal] = {}
    for rows, _ in table(context, {"bill", "note", "thanh toan bang"}, keep={"tong tien"}):
        for row in rows:
            order = str(row["bill"][0]).strip() if "bill" in row else ""
            method = plain(str(row.get("thanh toan bang", ("",))[0]))
            if not order or method != "debt":
                continue
            note = plain(str(row.get("note", ("",))[0]))
            if re.search(PAID, note):
                value = Decimal(0)
            elif re.search(r"\bno ca don\b|\bchua tra\b(?!.*\d)", note):
                if "tong tien" not in row:
                    raise ContractError("note_debt_total_missing")
                value = _money(row["tong tien"][0])
            elif upfront := re.fullmatch(
                r"(?:khach )?dua truoc (\d[\d.,]*\s*(?:tr\d*|trieu|k|nghin|ngan|d)?), "
                r"con lai ghi (?:so|no)",
                note,
            ):
                if "tong tien" not in row:
                    raise ContractError("note_debt_total_missing")
                value = _money(row["tong tien"][0]) - _amount(upfront[1])
            elif found := re.findall(OWED, note):
                if len(found) != 1:
                    raise ContractError("note_debt_ambiguous")
                value = _amount(found[0])
            else:
                raise ContractError(f"note_debt_unread:{note}")
            if owed.setdefault(order, value) != value:
                raise ContractError("note_debt_conflict")
    return owed


def _money(raw) -> Decimal:
    from src.agent.books import money

    return money(raw)


def answer(question: Question, context: BusinessContext, text: str):
    from src.agent.phone import _cutoff

    if not re.search(
        r"\b(?:con no|cong no|dang no|no nhieu nhat|tuoi no|con thieu|khoan no|no con lai)\b", text
    ):
        return None
    if re.search(r"\b(?:nhom khach|tra no|thu no|thu hoi|no them)\b", text):
        return None
    cutoff = _cutoff(question)
    end = context.catalog["coverage"].get("available_end_hint")
    if cutoff is None or not end:
        return None
    from src.data.normalization.values import parse_date

    if cutoff != parse_date(end).isoformat():
        return None  # the notes state the debt only at the end of the data
    owed = note_debts(context)
    if not owed:
        return None
    with closing(context.prepared.connect()) as conn:
        orders = {
            str(raw).strip(): (day[:10], status, customer)
            for raw, day, status, customer in conn.execute(
                "SELECT o.raw_order_id, o.ordered_at, o.status, p.customer_key FROM orders o "
                "LEFT JOIN order_parties p USING(order_key)"
            )
        }
        names = dict(conn.execute("SELECT entity_key, name FROM entities WHERE kind='customer'"))
        codes = dict(
            conn.execute("SELECT entity_key, source_code FROM entities WHERE kind='customer'")
        )
    debts = []
    for order, value in owed.items():
        if order not in orders:
            raise ContractError("note_debt_orphan")
        day, status, customer = orders[order]
        if status == "cancelled" or value <= 0 or day > cutoff:
            continue
        debts.append((day, value, customer))
    total = sum(v for _, v, _ in debts)
    per_customer = defaultdict(Decimal)
    for _, v, c in debts:
        if c is not None:
            per_customer[c] += v
    if re.search(r"\btuoi no\b|\btuoi (?:qua|hon|tren) \d+ ngay\b", text):
        age = lambda d: (date.fromisoformat(cutoff) - date.fromisoformat(d)).days  # noqa: E731
        if m := re.search(r"\btuoi (?:qua|hon|tren) (\d+) ngay\b", text):
            return (
                "note_debt_aged",
                int(sum(v for d, v, _ in debts if age(d) > int(m[1]))),
                "number",
            )
        if re.search(r"0\s*[-–]\s*7\b.*8\s*[-–]\s*15\b.*\btren 15\b", text):
            groups = [Decimal(0)] * 3
            for d, v, _ in debts:
                groups[0 if age(d) <= 7 else 1 if age(d) <= 15 else 2] += v
            return "note_debt_age_groups", [int(g) for g in groups], "list"
        return None
    top = re.search(r"\b(?:top )?(\d+) khach\b", text)
    if top and re.search(r"\bnhieu nhat\b", text):
        n = int(top[1])
        ranked = sorted(
            per_customer.items(), key=lambda kv: (-kv[1], codes.get(kv[0]) or names[kv[0]])
        )
        rows = [[names[k], int(v)] for k, v in ranked[:n]]
        return "note_debt_top", {"columns": ["khach_hang", "so_tien_no"], "rows": rows}, "table"
    if re.search(r"\b(?:so|bao nhieu|may|bn) khach\b", text):
        return "note_debt_customers", len(per_customer), "number"
    if re.search(r"\b(?:tong|bao nhieu|bn) (?:cong )?(?:tien)?\b", text):
        return "note_debt_total", int(total), "number"
    return None
