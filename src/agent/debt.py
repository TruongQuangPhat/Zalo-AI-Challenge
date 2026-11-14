"""Debt questions beyond the plain balance (rulebook §4 "Công nợ").

- Tuổi nợ: days from the order day to the asked day (an order placed that day is 0 days
  old). "Nợ quá/hơn N ngày" sums the open balances of orders older than N days; "nhóm
  0–7, 8–15, trên 15 ngày" lists the open balance of each age group in the asked order.
- Nợ thêm / trả nợ của một khách trong kỳ: only debt-method orders that are not cancelled.
  New debt is the part of an order placed in the period left unpaid on its order day;
  repayments are that customer's payments dated after the order day and inside the period.

Balances follow the same contract as the business executor's receivable: README payment
authority, non-cancelled orders, COD and marketplace orders excluded, unknown payment
methods or channels abstain. Any doubt returns None (abstain).
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import date
from decimal import Decimal

from src.data.catalog import BusinessContext
from src.jsonio import ContractError
from src.semantics.rulebook import plain

DEBT = r"\b(?:no|nowj|cong no|con thieu|mua chiu|ghi no)\b"
METHODS = {"cash", "transfer", "ewallet", "cod", "debt", "marketplace", "tmdt"}
OFF_DEBT = {"cod", "marketplace", "tmdt"}
# Payment methods as POS exports write them.
METHOD_WORDS = {
    "tien mat": "cash",
    "chuyen khoan": "transfer",
    "vi dien tu": "ewallet",
    "thu ho (cod)": "cod",
    "thu ho": "cod",
    "ghi no": "debt",
}


def method_code(method) -> str | None:
    """Canonical payment method of an order, or None when it is not recorded."""
    if not method:
        return None
    return METHOD_WORDS.get(plain(method), plain(method))


MARKETPLACES = {"marketplace", "tmdt", "san tmdt", "shopee", "lazada", "tiki", "tiktok_shop"}
STORE_CHANNELS = {"store", "phone", "zalo_oa", "counter"}


def _orders(context: BusinessContext, end: str):
    """Debt-relevant orders up to ``end``: (order, day, customer, method, payable, payments)."""
    summary = context.prepared.summary
    issues = summary.get("extensions", {}).get("issues", {})
    if not summary.get("extensions", {}).get("counts", {}).get("payments"):
        raise ContractError("payments_missing")
    if not re.search(
        r"cong no.*moi lan khach tra tien.*so khach phai tra cho ca don",
        plain(context.catalog["readme"]),
    ):
        raise ContractError("payment_history_and_order_payable_authority_missing")
    if any(k in issues for k in ["debt", "payments", "customer"]):
        raise ContractError("debt_payment_mapping_unsupported_or_conflict")
    with closing(context.prepared.connect()) as conn:
        coded = "customer_id" in summary.get("mapping", {}).get("fields", {}) or bool(
            conn.execute(
                "SELECT 1 FROM entities WHERE kind='customer' AND source_code IS NOT NULL"
            ).fetchone()
        )
        codes = dict(conn.execute("SELECT entity_key,source_code FROM entities"))
        payments: dict[str, list[tuple[str, Decimal]]] = {}
        for order, at, raw in conn.execute(
            "SELECT order_key,paid_at,amount FROM payments WHERE substr(paid_at,1,10)<=?", (end,)
        ):
            payments.setdefault(order, []).append((at[:10], Decimal(str(raw))))
        rows = conn.execute(
            "SELECT o.order_key,o.ordered_at,o.status,p.customer_key,p.payable,"
            "p.payment_method,s.channel FROM orders o LEFT JOIN order_parties p "
            "USING(order_key) LEFT JOIN order_scopes s USING(order_key) "
            "WHERE substr(o.ordered_at,1,10)<=? AND o.status!='cancelled'",
            (end,),
        ).fetchall()
    result = []
    for order, created, status, customer, payable, method, channel in rows:
        if status == "cancelled":
            continue
        method = method_code(method)
        if method not in METHODS:
            raise ContractError("order_payment_method_missing_or_unknown")
        if method in OFF_DEBT:
            continue
        if channel is not None:
            label = plain(channel)
            if label in MARKETPLACES:
                continue
            if label not in STORE_CHANNELS:
                raise ContractError("debt_sales_channel_policy_unsupported")
        if payable is None:
            raise ContractError("payable_missing")
        # A name-only customer cannot be a customer in a shop identified by code (§3).
        if customer and coded and not codes.get(customer):
            customer = None
        result.append(
            (order, created[:10], customer, method, Decimal(str(payable)), payments.get(order, []))
        )
    return result


def _age_groups(text: str) -> list[tuple[int, int | None]] | None:
    """'0–7, 8–15, trên 15 ngày' -> [(0, 7), (8, 15), (16, None)]; contiguous from 0."""
    groups = [(int(a), int(b)) for a, b in re.findall(r"\b(\d+) ?[–-] ?(\d+)\b", text)]
    above = re.findall(r"\b(?:tren|hon|qua) (\d+)(?: ngay)?\b", text)
    if not groups or len(above) != 1:
        return None
    bounds = [*groups, (int(above[0]) + 1, None)]
    expected = 0
    for low, high in bounds:
        if low != expected or (high is not None and high < low):
            return None
        expected = (high or 0) + 1
    return bounds


def _customer(context: BusinessContext, text: str) -> str | None:
    with closing(context.prepared.connect()) as conn:
        rows = conn.execute("SELECT entity_key,name FROM entities WHERE kind='customer'").fetchall()
    found: dict[str, set[str]] = {}
    for key, name in rows:
        label = plain(name or "")
        if len(label.split()) >= 2 and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text):
            found.setdefault(label, set()).add(key)
    # The longest named customer; a shorter name inside it is the same mention.
    names = [n for n in found if not any(n != m and n in m for m in found)]
    if len(names) != 1 or len(found[names[0]]) != 1:
        return None
    return next(iter(found[names[0]]))


def answer(question, context: BusinessContext, text: str):
    """(op, value, type, details) for an aged balance, age groups or one customer's
    new debt and repayments; None when the question is something else."""
    from src.agent.plan import _literal_windows
    from src.agent.rule_ops import _decision, _money
    from src.semantics.answerability import preflight

    rules = context.rules.rules if context.rules is not None else {}
    if not re.search(DEBT, text) or not context.prepared:
        return None
    if re.search(r"\b(?:nha cung cap|ncc|nhap)\b|%|phan tram", text):
        return None
    aged = re.search(
        r"\b(?:tuoi(?: no)? |dat |dat cach day |da dat |da )?(?:qua|hon|tren) (\d+) ngay\b", text
    )
    groups = _age_groups(text) if re.search(r"\btuoi\b|\bnhom\b", text) else None
    new_debt = re.search(r"\bno them\b|\bmua chiu them\b|\bghi no them\b", text)
    owed = re.search(r"\bcon no\b|\bchua tra\b|\bcon thieu\b|\btuoi\b", text)
    if groups or (aged and owed):
        if "debt_age" not in rules or "debt_balance" not in rules:
            return None
        if re.search(r"\b(?:khach nao|top|nhieu nhat|so khach|bao nhieu khach|may khach)\b", text):
            return None
        expressions = _literal_windows(question)
        if expressions and (
            len(expressions) != 1 or expressions[0]["kind"] not in {"day", "today"}
        ):
            return None
        expression = expressions[0] if expressions else {"kind": "day", "date": question.as_of_date}
        check = preflight(context, "receivable", expression, as_of_date=question.as_of_date)
        if check["decision"] == "unanswerable":
            return _decision("debt_age", ("unanswerable", check["reason"]), None, "number")
        if check["decision"] != "eligible_to_execute" or not check.get("effective_window"):
            return None
        end = check["effective_window"]["end"]
        cutoff = date.fromisoformat(end)
        balances = []
        for _, day, _, _, payable, paid in _orders(context, end):
            balance = payable - sum((a for d, a in paid if d <= end), Decimal(0))
            if balance > 0:
                balances.append(((cutoff - date.fromisoformat(day)).days, balance))
        if groups:
            values = [
                _money(
                    sum(
                        (b for age, b in balances if low <= age and (high is None or age <= high)),
                        Decimal(0),
                    )
                )
                for low, high in groups
            ]
            return _decision("debt_age_groups", check, values, "list")
        days = int(aged[1])
        return _decision(
            "debt_age",
            check,
            _money(sum((b for age, b in balances if age > days), Decimal(0))),
            "number",
        )
    if new_debt and re.search(r"\btra\b", text):
        if "debt_collection" not in rules:
            return None
        customer = _customer(context, text)
        if customer is None:
            return None
        expressions = _literal_windows(question)
        if len(expressions) != 1:
            return None
        check = preflight(
            context, "debt_collection", expressions[0], as_of_date=question.as_of_date
        )
        if check["decision"] == "unanswerable":
            return _decision("debt_customer", ("unanswerable", check["reason"]), None, "list")
        if check["decision"] != "eligible_to_execute" or not check.get("effective_window"):
            return None
        start, end = check["effective_window"]["start"], check["effective_window"]["end"]
        added = repaid = Decimal(0)
        for _, day, owner, method, payable, paid in _orders(context, end):
            if owner != customer or method != "debt":
                continue
            if start <= day <= end:
                added += max(
                    payable - sum((a for d, a in paid if d == day), Decimal(0)), Decimal(0)
                )
            repaid += sum((a for d, a in paid if d > day and start <= d <= end), Decimal(0))
        order = [added, repaid]
        if text.index(new_debt[0]) > text.index(re.search(r"\btra\b", text)[0]):
            return None
        return _decision("debt_customer", check, [_money(v) for v in order], "list")
    return None
