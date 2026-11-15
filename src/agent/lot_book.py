"""Lots read from the shop's lot books when the prepared facts hold no lot records.

README "Lô, hạn dùng: mỗi dòng nhập là một lô … Một dòng bán có thể lấy từ nhiều lô. Hàng
khách trả về lại lô đã bán": a receipt line is one lot (lot number, item code, expiry and the
quantity received in base units); a lot sale row takes a base quantity from one lot for an
order and records the base quantity the customer brought back to it. Columns are found by
their header or README meaning; quantities must be stated in base units. At the shop's last
data day a lot holds what it received, minus what orders that are not cancelled took from
it, plus what came back. A key that does not join, a repeated lot number, a negative balance,
or item totals that disagree with the shop's own end-of-data stock list abstain.
"""

from __future__ import annotations

import re
from contextlib import closing
from decimal import Decimal

from src.data.catalog import BusinessContext
from src.jsonio import ContractError

BASE = r"\((?:dvt|don vi|don vi tinh) co ban\)"
# Role -> (header pattern, README meaning pattern), both over plain text.
RECEIPT_LOTS = {
    "lot": (r"lo|so lo|ma lo", r"so lo\b.*"),
    "code": (r"ma (?:hang|mon|sku|hh|san pham|hang hoa)", r"ma (?:hang|san pham)\b.*"),
    "expiry": (r"hsd|han su dung|han dung", r"han (?:su )?dung\b.*"),
    "received": (r"(?:sl|so luong)(?: nhap)? " + BASE, r"so luong nhap " + BASE + ".*"),
}
LOT_SALES = {
    "order": (r"ma (?:hoa don|don|don hang)", r"ma don\b.*"),
    "lot": (r"lo|so lo|ma lo", r"so lo\b.*"),
    "sold": (r"(?:sl|so luong) " + BASE + r"|sl quy ra le", r"so luong ban lay tu lo " + BASE),
    "returned": (r"(?:sl|so luong) tra " + BASE, r"so luong khach tra lai " + BASE + ".*"),
}


def _books(context: BusinessContext, roles: dict) -> list[tuple[dict, list, str | None]]:
    """(role -> header, rows, epoch) of every table with exactly one column per role."""
    from src.agent.books import _read, column_meanings
    from src.semantics.rulebook import plain

    found = []
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        meanings = column_meanings(context, profile)
        headers = [plain(str(c["display_name"])) for c in profile.get("columns", [])]
        columns = {}
        for role, (header, meaning) in roles.items():
            hits = [
                h
                for h in headers
                if re.fullmatch(header, h) or re.fullmatch(meaning, meanings.get(h, ""))
            ]
            if len(hits) != 1:
                break
            columns[role] = hits[0]
        else:
            if len(set(columns.values())) == len(columns):
                result = _read(context, profile)
                if result is not None:
                    found.append((columns, result[1], result[2]))
    return found


def lot_balances(context: BusinessContext) -> list[tuple[str, str, str, Decimal]] | None:
    """(lot, product key, expiry, balance) of every lot at the shop's last data day, or None
    when the books are absent, do not join or disagree with the shop's stock list."""
    from src.agent.purchasing import _number
    from src.data.normalization.values import parse_date

    receipts, sales = _books(context, RECEIPT_LOTS), _books(context, LOT_SALES)
    if len(receipts) != 1 or not sales:
        return None
    with closing(context.prepared.connect()) as conn:
        products = dict(
            conn.execute(
                "SELECT source_code, entity_key FROM entities "
                "WHERE kind='product' AND source_code IS NOT NULL"
            )
        )
        orders = dict(conn.execute("SELECT raw_order_id, status FROM orders"))
        snapshot = context.prepared.summary.get("extensions", {}).get("stock_mode") == "snapshot"
        stocked = conn.execute("SELECT product_key, balance FROM stock_balances").fetchall()
    stocked = stocked if snapshot else []
    lots: dict[str, list] = {}
    try:
        columns, rows, epoch = receipts[0]
        for row in rows:
            lot = str(row[columns["lot"]][0]).strip()
            product = products.get(str(row[columns["code"]][0]).strip())
            received = _number(row[columns["received"]][0])
            expiry = parse_date(row[columns["expiry"]][0], date_system=epoch).isoformat()
            if not lot or product is None or lot in lots or received < 0:
                raise ContractError("lot_receipt_key_or_quantity_invalid")
            lots[lot] = [product, expiry, received]
        for columns, rows, _ in sales:
            for row in rows:
                lot = str(row[columns["lot"]][0]).strip()
                status = orders.get(str(row[columns["order"]][0]).strip())
                sold = _number(row[columns["sold"]][0])
                back = row.get(columns["returned"])
                returned = _number(back[0]) if back else Decimal(0)
                if lot not in lots or status is None or not 0 <= returned <= sold:
                    raise ContractError("lot_sale_key_or_quantity_invalid")
                if status != "cancelled":
                    lots[lot][2] += returned - sold
    except (ContractError, KeyError, TypeError, ValueError, ArithmeticError):
        return None
    if any(balance < 0 for _, _, balance in lots.values()):
        return None
    # A shop that also lists its end-of-data stock (a snapshot) must hold the same totals.
    totals: dict[str, Decimal] = {}
    for product, _, balance in lots.values():
        totals[product] = totals.get(product, Decimal(0)) + balance
    for product, balance in stocked:
        if product in totals and totals[product] != Decimal(str(balance)):
            return None
    return [(lot, product, expiry, balance) for lot, (product, expiry, balance) in lots.items()]
