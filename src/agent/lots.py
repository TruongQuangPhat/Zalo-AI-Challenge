"""Lots still in stock that expire in a window (rulebook "Hàng hết hạn trong N ngày tới").

At today D (the shop's declared last day only, as the lot executor requires), a lot's
remaining quantity is its received quantity minus what orders (not cancelled) took from it,
plus returns back to it by D. The window is D..D+N ("trong N ngày tới") or the two days the
question writes. Answers: the number of such lots, or the items holding one, listed by their
earliest such expiry then code (rulebook list order for items by expiry).
"""

from __future__ import annotations

import re
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

from src.data.catalog import BusinessContext
from src.data.normalization.values import PURCHASE_RECEIVED
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

AUTHORITY = "mot dong ban co the lay tu nhieu lo. hang khach tra ve lai lo da ban"


def remaining_lots(context: BusinessContext, day: str) -> list[tuple[str, str, str, Decimal]]:
    """(lot, product key, expiry, remaining) of lots with stock left at the end of ``day``."""
    balances: dict[str, list] = {}
    with closing(context.prepared.connect()) as conn:
        for lot, product, expiry, quantity, purchased, status in conn.execute(
            "SELECT l.lot_id, l.product_key, l.expiry, l.received_quantity, p.purchased_at, "
            "p.status FROM lots l JOIN purchases p ON l.purchase_id = p.purchase_key"
        ):
            if purchased[:10] <= day and plain(status) in PURCHASE_RECEIVED:
                balances[lot] = [product, expiry, Decimal(quantity)]
        for lot, sold, returned, returned_at, ordered, status in conn.execute(
            "SELECT a.lot_id, a.quantity, a.returned_quantity, a.returned_at, o.ordered_at, "
            "o.status FROM lot_allocations a JOIN orders o USING(order_key)"
        ):
            if lot in balances and ordered[:10] <= day and status != "cancelled":
                balances[lot][2] -= Decimal(sold)
                if returned_at and returned_at[:10] <= day:
                    balances[lot][2] += Decimal(returned)
    if any(b[2] < 0 for b in balances.values()):
        raise ContractError("negative_lot_balance_conflict")
    return [(lot, p, e, q) for lot, (p, e, q) in balances.items() if q > 0]


def _span(question: Question, text: str) -> tuple[str, str] | None:
    from src.agent.plan import _literal_windows

    today = question.as_of_date
    ahead = re.search(r"\btrong (\d+) ngay (?:toi|sap toi|nua)\b|\b(\d+) ngay toi\b", text)
    windows = _literal_windows(question)
    if ahead:
        n = int(ahead[1] or ahead[2])
        end = (date.fromisoformat(today) + timedelta(days=n)).isoformat()
        explicit = [w for w in windows if w["kind"] == "range"]
        if explicit and (explicit[0]["start"], explicit[0]["end"]) != (today, end):
            return None  # the written days must agree with "N ngày tới"
        return today, end
    ranges = [w for w in windows if w["kind"] == "range"]
    if len(ranges) == 1 and ranges[0]["start"] == today:
        return today, ranges[0]["end"]
    until = re.search(
        r"\b(?:tu nay|bay gio) (?:den|toi) het(?: ngay)? (\d{1,2})/(\d{1,2})/(\d{4})\b", text
    )
    if until:
        end = date(int(until[3]), int(until[2]), int(until[1])).isoformat()
        return today, end
    return None


def answer(question: Question, context: BusinessContext, text: str):
    if not re.search(r"\b(?:het han|han dung|hsd|can date)\b", text) or not re.search(
        r"\b(?:lo|con ton|con hang|ton kho|chua ban het)\b", text
    ):
        return None
    if re.search(r"\b(?:tien|von|gia tri|gan het han nhat|som nhat|ngay nao|ngay may)\b", text):
        return None
    if not context.prepared or AUTHORITY not in plain(context.catalog.get("readme") or ""):
        return None
    extension = context.prepared.summary.get("extensions", {})
    if "lots" in (extension.get("issues") or {}):
        return None
    confirmed = context.semantics["coverage"]["confirmed_end"] if context.semantics else None
    if question.as_of_date != confirmed:
        return None  # lot stock is known only at the declared export end
    span = _span(question, text)
    if span is None:
        return None
    start, end = span
    lots = [x for x in remaining_lots(context, start) if start <= x[2] <= end]
    if re.search(r"\b(?:may|bao nhieu|bn|so) lo\b", text):
        return "lots_expiring_count", len(lots), "number"
    if re.search(
        r"\b(?:sp|san pham|hang|mat hang|thuoc)\b.*\b(?:nao|gi)\b|\bke\b|\bliet ke\b", text
    ):
        with closing(context.prepared.connect()) as conn:
            items = {
                k: (code, name)
                for k, code, name in conn.execute(
                    "SELECT entity_key, source_code, name FROM entities WHERE kind='product'"
                )
            }
        earliest = defaultdict(lambda: "9999-12-31")
        for _, product, expiry, _ in lots:
            earliest[product] = min(earliest[product], expiry)
        order = sorted(earliest, key=lambda k: (earliest[k], items[k][0] or items[k][1]))
        return "lots_expiring_items", [items[k][1] for k in order], "list"
    return None
