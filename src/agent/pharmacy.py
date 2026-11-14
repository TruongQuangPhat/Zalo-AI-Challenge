"""Pharmacy rules over the shop's drug catalog (rulebook "Nhà thuốc").

- Thuốc kê đơn: products the drug catalog flags as prescription; every other product is
  "hàng không kê đơn". The share is measured on revenue (§1).
- Doanh thu theo hoạt chất: revenue of drugs whose catalog ingredient is written exactly so;
  a combination ("A + B") is its own kind, never counted under A or B.

The catalog table is found by its columns (an ingredient column and a prescription-flag
column) and joined to sold products by code. An unreadable flag, an unjoinable code or a sale
line without a product abstains.
"""

from __future__ import annotations

import re
from decimal import Decimal

from src.data.catalog import BusinessContext
from src.jsonio import ContractError
from src.semantics.rulebook import plain

YES = {"true", "1", "x", "co", "yes"}
NO = {"false", "0", "", "khong", "no"}
NOT_PRESCRIPTION = r"\b(?:khong|k|ko|hok|hong) (?:phai )?ke don\b|\btru (?:cac )?thuoc ke don\b"
MONEY = r"\b(?:tien|doanh thu|dthu|revenue|ban duoc|thu duoc)\b"
SHARE = r"%|\b(?:phan tram|ti le|ty le|chiem|phan cua)\b"


def drug_catalog(context: BusinessContext) -> tuple[str, dict[str, tuple[str | None, bool]]]:
    """("code" or "name", product key -> (ingredient or None, prescription flag))."""
    from contextlib import closing

    with_flags = []
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        # "BanTheoDon" (database column) reads as "ban theo don".
        columns = {
            c["position"]: plain(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", c["display_name"]))
            for c in profile.get("columns", [])
        }
        ingredient = [
            p for p, n in columns.items() if re.search(r"\bhoat chat\b|\bthanh phan chinh\b", n)
        ]
        flag = [p for p, n in columns.items() if re.search(r"\bke don\b|\bban theo don\b", n)]
        if len(ingredient) == 1 and len(flag) == 1:
            with_flags.append((profile, columns, ingredient[0], flag[0]))
    if len(with_flags) != 1:
        return "code", {}
    profile, columns, ingredient_at, flag_at = with_flags[0]
    with closing(context.prepared.connect()) as conn:
        products = conn.execute(
            "SELECT source_code,name FROM entities WHERE kind='product'"
        ).fetchall()
    keys = {
        "code": {str(c).strip() for c, _ in products if c is not None},
        "name": {plain(n) for _, n in products if n},
    }
    header = set(profile.get("header_rows", []))
    rows = []
    for row in context.source_table(profile["source_id"]).iter_rows():
        if row.row in header or (header and row.row is not None and row.row < min(header)):
            continue
        if any(c.raw_value not in {None, ""} for c in row.cells.values()):
            rows.append(row)

    def text(row, position) -> str:
        cell = row.cells.get(position)
        return "" if cell is None or cell.raw_value is None else str(cell.raw_value).strip()

    def key(kind, value):
        return value if kind == "code" else plain(value)

    # The join column holds the shop's product codes (else its exact product names).
    for kind in ["code", "name"]:
        join = [
            p
            for p in columns
            if p not in {ingredient_at, flag_at}
            and rows
            and sum(key(kind, text(r, p)) in keys[kind] for r in rows) >= 0.95 * len(rows)
        ]
        if join:
            break
    if len(join) != 1:
        return "code", {}
    catalog: dict[str, tuple[str | None, bool]] = {}
    for row in rows:
        product, raw_flag = key(kind, text(row, join[0])), plain(text(row, flag_at))
        if raw_flag not in YES | NO or product in catalog:
            raise ContractError("drug_catalog_unreadable")
        catalog[product] = (plain(text(row, ingredient_at)) or None, raw_flag in YES)
    return kind, catalog


def answer(question, context: BusinessContext, text: str):
    """(op, value, type, details) for a prescription or active-ingredient question, or None."""
    from src.agent.rule_ops import _decision, _lines, _money, _percent, _window

    rules = context.rules.rules if context.rules is not None else {}
    ingredient_cue = re.search(r"\bhoat chat\b", text)
    if ingredient_cue:
        if "active_ingredient" not in rules or re.search(SHARE, text):
            return None
    elif not re.search(r"\bke don\b", text) or "prescription_share" not in rules:
        return None
    if not re.search(MONEY, text) and not re.search(SHARE, text):
        return None
    if re.search(r"\b(?:nhat|top|nao|so don|nhap|ton|lai gop|loi nhuan|khach)\b", text):
        return None
    kind, catalog = drug_catalog(context)
    if not catalog:
        return None
    if ingredient_cue:
        named = [
            i
            for i in {v for v, _ in catalog.values() if v}
            if re.search(r"\bhoat chat " + re.escape(i) + r"(?![\w+]| \+)", text)
        ]
        if len(named) != 1:
            return None
        wanted = named[0]
        name = "active_ingredient"

        def selected(code):
            return catalog.get(code, (None, False))[0] == wanted
    else:
        negated = bool(re.search(NOT_PRESCRIPTION, text))
        name = "prescription_share" if re.search(SHARE, text) else "prescription_revenue"
        if negated and name == "prescription_share":
            return None

        def selected(code):
            return catalog.get(code, (None, False))[1] != negated

    answer_type = "percent" if name == "prescription_share" else "number"
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision(name, check, None, answer_type)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    part = whole = Decimal(0)
    for row in _lines(context, check):
        amount, role = row[2], row[3]
        product = row[6] if kind == "code" else row[5]
        if role not in roles or amount is None:
            continue
        if role == "sale_item" and product is None:
            raise ContractError("sale_line_product_unknown")
        value = Decimal(str(amount))
        whole += value
        if role == "sale_item" and selected(
            str(product).strip() if kind == "code" else plain(product)
        ):
            part += value
    if name == "prescription_share":
        if not whole:
            return None
        return _decision(name, check, _percent(part * 100 / whole), "percent")
    return _decision(name, check, _money(part), "number")
