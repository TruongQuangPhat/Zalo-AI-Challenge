"""Phone-shop rules of the public rulebook ("Cửa hàng điện thoại").

Devices, models and brands come from prepared sale lines whose item resolves through the
shop's own abbreviation sheet (columns "Ghi tắt", "Tên hàng", "Loại": máy mới, máy cũ, phụ
kiện). Trade-in, payable, installment, debt and warranty books are read straight from the
source tables by their column headers. Rulebook meanings used:

- model = the full name before its "…GB" capacity, else the whole name; brand = first word,
  iPhone is Apple;
- trade-in and payable totals of a period count orders that are neither cancelled nor
  returned; payable is after discount and trade-in, including any unpaid part;
- installments are debt: the unpaid part (including periods not yet due) is owed; a period
  whose due day is before the asked day and is not fully paid is overdue;
- warranty rate of a quarter = warranty visits received in the quarter / devices sold in
  the quarter (orders neither cancelled nor returned).

Anything outside these shapes, an unresolved item, a tie or an inconsistent book abstains.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from contextlib import closing
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from src.agent.books import money
from src.agent.books import table as _table
from src.agent.purchasing import _day
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

RULES = ("phone_tradein", "phone_model_brand")
KINDS = {"may moi": "new", "may cu": "used", "phu kien": "accessory"}
DEVICES = {"new", "used"}
WORDS = {"hai": 2, "ba": 3, "bon": 4, "nam": 5}


def model_of(name: str) -> str:
    """Rulebook "dòng máy": the name before its capacity "…GB", else the whole name."""
    match = re.match(r"^(.*?)\s+\d+\s*(?:GB|TB)\b", name.strip(), re.I)
    return (match[1] if match else name).strip()


def brand_of(name: str) -> str:
    first = name.strip().split()[0]
    return "Apple" if plain(first) == "iphone" else first


def phone_shop(context: BusinessContext) -> bool:
    """A shop whose sales book records device IMEIs (README: "Máy có IMEI, phụ kiện không")."""
    return any(
        plain(c["display_name"]) == "imei"
        for t in context.catalog["tables"]
        for c in t.get("columns", [])
    )


def catalog_map(context: BusinessContext) -> dict[str, tuple[str, str]]:
    """Accentless abbreviation or full name -> (full name, kind) from the "Ghi tắt" sheet."""
    result: dict[str, tuple[str, str]] = {}
    for rows, _ in _table(context, {"ghi tat", "ten hang", "loai"}):
        for row in rows:
            if not {"ghi tat", "ten hang", "loai"} <= row.keys():
                continue
            full = str(row["ten hang"][0]).strip()
            kind = KINDS.get(plain(str(row["loai"][0])))
            if kind is None:
                raise ContractError("phone_kind_unknown")
            for key in (plain(str(row["ghi tat"][0])), plain(full)):
                if result.setdefault(key, (full, kind)) != (full, kind):
                    raise ContractError("phone_abbreviation_conflict")
    return result


def _orders(context) -> dict[str, tuple[str, str]]:
    with closing(context.prepared.connect()) as conn:
        return {
            str(raw).strip(): (day[:10], status)
            for raw, day, status in conn.execute(
                "SELECT raw_order_id, ordered_at, status FROM orders"
            )
        }


def _lines(context, check, names):
    """(order, full name, kind, quantity, amount) of sale lines in the revenue window."""
    window, statuses = check["effective_window"], list(check["contract"]["status_policy"])
    with closing(context.prepared.connect()) as conn:
        rows = conn.execute(
            "SELECT o.raw_order_id, e.name, i.sold_quantity, l.net_amount FROM orders o "
            "JOIN order_lines l USING(order_key) LEFT JOIN line_items i USING(line_key) "
            "LEFT JOIN entities e ON e.entity_key = i.product_key "
            f"WHERE l.row_role = 'sale_item' AND o.status IN ({','.join('?' * len(statuses))}) "
            "AND substr(o.ordered_at,1,10) BETWEEN ? AND ?",
            [*statuses, window["start"], window["end"]],
        ).fetchall()
    for order, name, quantity, amount in rows:
        resolved = names.get(plain(str(name or "")))
        if resolved is None or quantity is None or amount is None:
            raise ContractError("phone_line_unresolved")
        yield str(order).strip(), resolved[0], resolved[1], Decimal(quantity), Decimal(amount)


def _percent(part: Decimal, whole: Decimal) -> float:
    return float((part * 100 / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _whole(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def installment_question(question: Question, text: str) -> bool:
    raw = question.question.casefold()
    if "góp" in raw:
        return True
    return "gộp" not in raw and bool(
        re.search(r"\b(?:tra gop|tien gop|ky gop|chua gop|cham gop|han gop|da gop)\b", text)
    )


def answer(question: Question, context: BusinessContext, text: str):
    """(op, value, type) for a phone-shop question, or None."""
    from src.agent.rule_ops import _window

    rules = context.rules.rules if context.rules is not None else {}
    if not all(r in rules for r in RULES) or not context.prepared or not phone_shop(context):
        return None
    if re.search(r"\b(?:nhom khach|khach nhom|kenh|ca sang|ca chieu|nhan vien)\b", text):
        return None
    for handler in (_installments, _debt, _warranty, _tradein, _payable, _devices):
        result = handler(question, context, text, _window)
        if result is not None:
            return result
    return None


# --- devices, models, brands ---------------------------------------------------------

_COUNT = (
    r"\b(?:bao nhieu|bn|may|so|tong cong|tat ca bao nhieu|tat ca) "
    r"(?:chiec|may|cai|dien thoai|dt)\b"
)


def _devices(question, context, text, window):
    if installment_question(question, text) or re.search(
        r"\b(?:bao hanh|thu (?:mua |lai )?may cu|khach phai tra|no)\b", text
    ):
        return None
    names = catalog_map(context)
    if not names:
        return None
    fulls = {full: kind for full, kind in names.values()}
    models = {plain(model_of(f)): model_of(f) for f, k in fulls.items() if k in DEVICES}
    share = re.search(r"%|\bphan tram\b|\bchiem\b", text)
    average = re.search(r"\btrung binh (?:1|mot|moi) may\b", text)
    top = re.search(r"\b(?:top )?(\d|hai|ba|bon|nam) dong may\b", text)
    best_item = re.search(r"\bnhom may (moi|cu)\b.*\bmat hang nao\b.*\bnhieu may nhat\b", text)
    count = re.search(_COUNT, text)
    best_seller = re.search(
        r"\b(?:mat hang|sp|san pham|hang|mon) (?:nao|chi|gi)\b.*\bban chay nhat\b", text
    )
    if not (share or average or top or best_item or count or best_seller):
        return None
    used = re.search(r"\b(?:may|dien thoai|dt) cu\b", text)
    new = re.search(r"\bmay moi\b", text)
    both = re.search(
        r"\b(?:gop|ca|tinh ca|ke ca)\b.*\bmay moi\b.*\b(?:lan|va|voi|ca)\b.*\bmay cu\b", text
    )
    check = window(question, context)
    if not isinstance(check, dict):
        return None
    lines = list(_lines(context, check, names))
    if top:
        n = WORDS.get(top[1]) or int(top[1])
        if not re.search(r"\bnhieu may nhat\b|\bban (?:duoc |chay )?nhieu nhat\b", text):
            return None
        totals = defaultdict(Decimal)
        for _, full, kind, qty, _ in lines:
            if kind in DEVICES:
                totals[model_of(full)] += qty
        ranked = sorted(totals.items(), key=lambda x: -x[1])
        if len(ranked) < n or (len(ranked) > n and ranked[n - 1][1] == ranked[n][1]):
            return None
        values = [v for _, v in ranked[:n]]
        if len(set(values)) != len(values):
            return None  # the rule names no order among equal counts
        return "phone_top_models", [m for m, _ in ranked[:n]], "list"
    if best_seller:
        if re.search(r"\b(?:top|nhung|liet ke)\b|(?<![\d/])\b\d+ (?:mat hang|sp|san pham)\b", text):
            return None
        totals = defaultdict(Decimal)
        for _, full, _, qty, _ in lines:
            totals[full] += qty
        if not totals:
            return None
        best = max(totals.values())
        leaders = [k for k, v in totals.items() if v == best]
        return ("phone_best_seller", leaders[0], "string") if len(leaders) == 1 else None
    if best_item:
        kind_wanted = "new" if best_item[1] == "moi" else "used"
        totals = defaultdict(Decimal)
        for _, full, kind, qty, _ in lines:
            if kind == kind_wanted:
                totals[full] += qty
        if not totals:
            return None
        best = max(totals.values())
        leaders = [k for k, v in totals.items() if v == best]
        return ("phone_best_item", leaders[0], "string") if len(leaders) == 1 else None
    if average:
        if used and not both:
            return None
        quantity = sum(q for _, _, k, q, _ in lines if k in DEVICES)
        amount = sum(a for _, _, k, _, a in lines if k in DEVICES)
        return ("phone_device_average", _whole(amount / quantity), "number") if quantity else None
    if share:
        devices = [(f, k, q, a) for _, f, k, q, a in lines if k in DEVICES]
        if re.search(r"\bmay moi\b.*\b(?:tien|doanh thu|dthu)\b", text) and re.search(
            r"\b(?:tien ban may|doanh thu may|dthu may|tong (?:doanh thu|dthu) may)\b", text
        ):
            part = sum(a for _, k, _, a in devices if k == "new")
            whole = sum(a for *_, a in devices)
            return ("phone_new_revenue_share", _percent(part, whole), "percent") if whole else None
        brand = re.search(r"\bmay (\w+) chiem\b.*\btong so may\b", text)
        if brand:
            brands = {plain(brand_of(f)) for f, *_ in devices} | {"apple"}
            if brand[1] not in brands:
                return None
            part = sum(q for f, _, q, _ in devices if plain(brand_of(f)) == brand[1])
            whole = sum(q for _, _, q, _ in devices)
            return ("phone_brand_share", _percent(part, whole), "percent") if whole else None
        return None
    # Counts: one named model or item, used / new / all devices.
    named = sorted(
        (k for k in models if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", text)),
        key=len,
        reverse=True,
    )
    items = sorted((plain(f) for f in fulls if plain(f) in text), key=len, reverse=True)
    if items and (not named or len(items[0]) > len(named[0])):
        target = items[0]
        return (
            "phone_item_quantity",
            int(sum(q for _, f, _, q, _ in lines if plain(f) == target)),
            "number",
        )
    if named:
        target = named[0]
        if used or new:
            return None
        return (
            "phone_model_quantity",
            int(sum(q for _, f, k, q, _ in lines if k in DEVICES and plain(model_of(f)) == target)),
            "number",
        )
    if re.search(r"\b(?:iphone|samsung|xiaomi|oppo|vivo|realme|nokia|redmi|galaxy)\b", text):
        return None  # a model the shop never sold, or written differently
    if not re.search(r"\b(?:may|chiec|dien thoai|dt)\b", text.replace("may chiec", "chiec")):
        return None
    if both or not (used or new):
        kinds = DEVICES
    else:
        kinds = {"used"} if used else {"new"}
    return "phone_device_count", int(sum(q for _, _, k, q, _ in lines if k in kinds)), "number"


# --- trade-in and payable --------------------------------------------------------------


def tradeins(context) -> dict[str, Decimal]:
    """Order -> trade-in money, from the trade-in book or the sales book column."""
    amounts: dict[str, Decimal] = defaultdict(Decimal)
    book = _table(context, {"gia thu", "tru vao (stt so ban)"})
    if book:
        for rows, _ in book:
            for row in rows:
                if "gia thu" not in row or "tru vao (stt so ban)" not in row:
                    raise ContractError("tradein_row_incomplete")
                amounts[str(row["tru vao (stt so ban)"][0]).strip()] += money(row["gia thu"][0])
        return dict(amounts)
    for rows, _ in _table(context, {"stt", "hang", "thu may cu"}):
        seen: dict[str, Decimal] = {}
        for row in rows:
            order = str(row["stt"][0]).strip() if "stt" in row else None
            if not order:
                continue
            value = money(row["thu may cu"][0]) if "thu may cu" in row else Decimal(0)
            if seen.setdefault(order, value) != value:
                raise ContractError("tradein_order_conflict")
        for order, value in seen.items():
            amounts[order] += value
    return dict(amounts)


def _period_orders(context, check) -> list[str]:
    window, statuses = check["effective_window"], set(check["contract"]["status_policy"])
    return [
        o
        for o, (day, status) in _orders(context).items()
        if window["start"] <= day <= window["end"] and status in statuses
    ]


def _tradein(question, context, text, window):
    if not re.search(r"\bthu (?:mua |lai )?may cu\b", text):
        return None
    share = re.search(r"%|\bphan tram\b|\bchiem\b", text)
    total = re.search(r"\b(?:tong|cong|bao nhieu|het nhieu|duoc|bn)\b", text)
    if not (share or total) or re.search(r"\bkhach phai tra\b|\btru (?:tien )?bot\b", text):
        return None
    check = window(question, context)
    if not isinstance(check, dict):
        return None
    amounts = tradeins(context)
    orders = _period_orders(context, check)
    if share:
        if not orders or not re.search(r"\b(?:don|dh)\b", text):
            return None
        part = sum(1 for o in orders if amounts.get(o, 0) > 0)
        return "phone_tradein_share", _percent(Decimal(part), Decimal(len(orders))), "percent"
    return "phone_tradein_total", int(sum(amounts.get(o, Decimal(0)) for o in orders)), "number"


def payables(context) -> dict[str, Decimal]:
    """Order -> "Khách trả" (written for the whole order), checked against its lines:
    line amounts - discount - trade-in must give it."""
    tradein = tradeins(context)
    result, lines, discounts = {}, defaultdict(Decimal), {}
    for rows, _ in _table(context, {"stt", "hang", "thanh tien", "khach tra"}):
        for row in rows:
            if "stt" not in row:
                continue
            order = str(row["stt"][0]).strip()
            pay = money(row["khach tra"][0]) if "khach tra" in row else None
            cut = money(row["bot"][0]) if "bot" in row else Decimal(0)
            if pay is None or result.setdefault(order, pay) != pay:
                raise ContractError("payable_order_conflict")
            if discounts.setdefault(order, cut) != cut:
                raise ContractError("discount_order_conflict")
            lines[order] += money(row["thanh tien"][0])
    for order, pay in result.items():
        if lines[order] - discounts[order] - tradein.get(order, Decimal(0)) != pay:
            raise ContractError("payable_does_not_add_up")
    return result


def _payable(question, context, text, window):
    if not re.search(r"\bkhach phai tra\b|\btru (?:tien )?bot\b.*\bmay cu\b", text):
        return None
    average = re.search(r"\b(?:trung binh|binh quan)\b", text)
    check = window(question, context)
    if not isinstance(check, dict):
        return None
    amounts = payables(context)
    orders = _period_orders(context, check)
    if any(o not in amounts for o in orders) or not orders:
        return None
    total = sum(amounts[o] for o in orders)
    if average:
        return "phone_payable_average", _whole(total / len(orders)), "number"
    return "phone_payable_total", int(total), "number"


# --- warranty ---------------------------------------------------------------------------


def _visits(context) -> list[tuple[str, str, str]]:
    """(received day, model as written, IMEI) of each warranty visit."""
    visits = []
    for rows, epoch in _table(context, {"ngay nhan", "imei", "may"}):
        for row in rows:
            if "ngay nhan" not in row:
                raise ContractError("warranty_day_missing")
            raw, cell = row["ngay nhan"]
            imei = str(row["imei"][0]).strip() if "imei" in row else ""
            visits.append((_day(raw, cell, epoch), str(row.get("may", ("",))[0]).strip(), imei))
    return visits


def _sold_imeis(context, check) -> set[str]:
    orders = set(_period_orders(context, check))
    found = set()
    for rows, _ in _table(context, {"stt", "hang", "imei"}):
        for row in rows:
            if "stt" in row and str(row["stt"][0]).strip() in orders and "imei" in row:
                found.update(p.strip() for p in str(row["imei"][0]).split("/") if p.strip())
    return found


def _warranty(question, context, text, window):
    if not re.search(r"\bbao hanh\b", text):
        return None
    visits = _visits(context)
    if not visits:
        return None
    if re.search(r"\bmay ban\b.*\b(?:da|duoc)\b.*\b(?:mang|dem)\b.*\bbao hanh\b", text):
        from src.agent.plan import _literal_windows

        months = [w for w in _literal_windows(question) if w["kind"] == "month"]
        rest = [w for w in _literal_windows(question) if w["kind"] not in {"month", "today"}]
        if len(months) != 1 or rest:
            return None
        check = window(question, context, months[0])
        if not isinstance(check, dict):
            return None
        sold = _sold_imeis(context, check)
        back = {v[2] for v in visits if v[0] <= question.as_of_date}
        return "phone_warranty_returned_devices", len(sold & back), "number"
    check = window(question, context)
    if not isinstance(check, dict):
        return None
    span = check["effective_window"]
    inside = [v for v in visits if span["start"] <= v[0] <= span["end"]]
    if re.search(r"%|\bphan tram\b|\bchiem\b|\bti le\b", text):
        if "phone_warranty" not in context.rules.rules or not re.search(r"\bquy\b", text):
            return None
        names = catalog_map(context)
        sold = sum(q for _, _, k, q, _ in _lines(context, check, names) if k in DEVICES)
        return (
            ("phone_warranty_rate", _percent(Decimal(len(inside)), sold), "percent")
            if sold
            else None
        )
    if re.search(r"\bdong may nao\b", text) and re.search(r"\bnhieu luot\b.*\bnhat\b", text):
        totals = defaultdict(int)
        shown = {}
        for _, model, _ in inside:
            totals[plain(model)] += 1
            shown.setdefault(plain(model), model)
        if not totals:
            return None
        best = max(totals.values())
        leaders = [k for k, v in totals.items() if v == best]
        return ("phone_warranty_model", shown[leaders[0]], "string") if len(leaders) == 1 else None
    if re.search(r"\b(?:bao nhieu|bn|may) luot\b|\bluot\b.*\bbao hanh\b", text):
        return "phone_warranty_visits", len(inside), "number"
    return None


# --- installments and debt ----------------------------------------------------------------


def _customer(row) -> tuple[str, str]:
    """Rulebook §3 without customer codes: names differing only in writing are one customer."""
    name = str(row["khach"][0]).strip() if "khach" in row else ""
    if not name:
        raise ContractError("customer_name_missing")
    return plain(name), name


def contracts(context) -> list[dict]:
    """Installment contracts with their payments (rows sharing the contract's STT)."""
    found = []
    for rows, epoch in _table(context, {"stt", "tra truoc", "so ky", "gop/thang", "ngay gop"}):
        by_order: dict[str, dict] = {}
        for row in rows:
            order = str(row["stt"][0]).strip() if "stt" in row else None
            if not order:
                raise ContractError("installment_row_without_order")
            if "ngay" in row:
                if order in by_order:
                    raise ContractError("installment_contract_twice")
                raw, cell = row["ngay"]
                key, name = _customer(row)
                start = _day(raw, cell, epoch)
                deadline = int(Decimal(str(row["han gop (ngay)"][0])))
                price, upfront = money(row["gia"][0]), money(row["tra truoc"][0])
                periods, each = int(Decimal(str(row["so ky"][0]))), money(row["gop/thang"][0])
                if each * periods != price - upfront:
                    raise ContractError("installment_schedule_inexact")
                by_order[order] = {
                    "order": order,
                    "customer": key,
                    "name": name,
                    "start": start,
                    "owed": price - upfront,
                    "each": each,
                    "periods": periods,
                    "due": _schedule(start, periods, deadline),
                    "payments": [],
                }
            contract = by_order.get(order)
            if contract is None:
                raise ContractError("installment_payment_before_contract")
            if "ngay gop" in row or "da gop" in row:
                if "ngay gop" not in row or "da gop" not in row:
                    raise ContractError("installment_payment_incomplete")
                raw, cell = row["ngay gop"]
                contract["payments"].append((_day(raw, cell, epoch), money(row["da gop"][0])))
        found.extend(by_order.values())
    return found


def _schedule(start: str, periods: int, day: int) -> list[str]:
    first = date.fromisoformat(start)
    due = []
    for k in range(1, periods + 1):
        month = first.month - 1 + k
        year, month = first.year + month // 12, month % 12 + 1
        if day > calendar.monthrange(year, month)[1]:
            raise ContractError("installment_due_day_missing_in_month")
        due.append(date(year, month, day).isoformat())
    return due


def _paid(contract, cutoff: str) -> Decimal:
    return sum((a for d, a in contract["payments"] if d <= cutoff), Decimal(0))


def _overdue(contract, cutoff: str) -> Decimal:
    due = sum(1 for d in contract["due"] if d < cutoff) * contract["each"]
    return max(Decimal(0), due - _paid(contract, cutoff))


def _cutoff(question) -> str | None:
    from src.agent.plan import _literal_windows

    points = {
        w.get("date", question.as_of_date) if w["kind"] in {"day", "today"} else None
        for w in _literal_windows(question)
    }
    if len(points) != 1 or None in points:
        return None
    return points.pop()


def _live(context, contract_list, cutoff):
    orders = _orders(context)
    live = []
    for c in contract_list:
        if c["start"] > cutoff:
            continue
        if orders.get(c["order"], (None, None))[1] not in {"completed", "shipping"}:
            raise ContractError("installment_order_not_a_sale")
        live.append(c)
    return live


def _named(contract_list, text):
    names = {plain(c["name"]) for c in contract_list if c["name"]}
    named = [n for n in names if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text)]
    if len(named) != 1:
        return None
    keys = {c["customer"] for c in contract_list if plain(c["name"]) == named[0]}
    return keys if len(keys) == 1 else None


def _installments(question, context, text, window):
    if not installment_question(question, text) or re.search(r"\bkhach phai tra\b", text):
        return None
    cutoff = _cutoff(question)
    if cutoff is None or cutoff > question.as_of_date:
        return None
    all_contracts = contracts(context)
    if not all_contracts:
        return None
    live = _live(context, all_contracts, cutoff)
    customer = None
    if re.search(r"\bkhach (?!tra gop\b|qua han\b|chua\b|con\b)\w", text):
        customer = _named(live, text)
        if customer is None:
            return None
    chosen = [c for c in live if customer is None or c["customer"] in customer]
    if re.search(
        r"\btoi han (?:bao nhieu|may|bn) ky\b|\b(?:bao nhieu|may|bn) ky\b.*\btoi han\b", text
    ):
        if customer is None or len(chosen) != 1:
            return None
        return "installment_due_periods", sum(d <= cutoff for d in chosen[0]["due"]), "number"
    if re.search(r"\bqua han\b|\bcham gop\b", text):
        if re.search(r"\b(?:may|bao nhieu|bn|so) (?:khach|nguoi)\b|\bmay nguoi\b", text):
            late = {c["customer"] for c in chosen if _overdue(c, cutoff) > 0}
            return "installment_overdue_customers", len(late), "number"
        if re.search(r"\btien\b", text):
            return (
                "installment_overdue_amount",
                int(sum(_overdue(c, cutoff) for c in chosen)),
                "number",
            )
        return None
    if re.search(r"\b(?:chua gop|con thieu|con lai|chua tra)\b", text) and re.search(
        r"\b(?:ke ca|tinh ca)\b.*\bchua toi han\b", text
    ):
        owed = sum(c["owed"] - _paid(c, cutoff) for c in chosen)
        return "installment_remaining", int(owed), "number"
    return None


def debts(context, cutoff: str) -> list[tuple[str, str, Decimal]]:
    """(customer, order day, unpaid at cutoff) of the debt book, orders placed by cutoff."""
    result = []
    for rows, epoch in _table(context, {"no", "da tra", "ngay tra", "ghi chu"}):
        current, by_note = None, {}
        for row in rows:
            note = str(row["ghi chu"][0]).strip() if "ghi chu" in row else ""
            if "ngay" in row:
                raw, cell = row["ngay"]
                key, _ = _customer(row)
                current = {
                    "customer": key,
                    "day": _day(raw, cell, epoch),
                    "owed": money(row["no"][0]),
                    "paid": [],
                    "note": note,
                }
                if note in by_note:
                    raise ContractError("debt_order_twice")
                by_note[note] = current
            elif note and by_note.get(note) is not current:
                raise ContractError("debt_payment_order_mismatch")
            if current is None:
                raise ContractError("debt_payment_before_order")
            if "da tra" in row:
                if "ngay tra" not in row:
                    raise ContractError("debt_payment_without_day")
                raw, cell = row["ngay tra"]
                current["paid"].append((_day(raw, cell, epoch), money(row["da tra"][0])))
        orders = _orders(context)
        for note, d in by_note.items():
            order = re.fullmatch(r"(?i)stt\s*(\d+)", note)
            status = orders.get(order[1], (None, None))[1] if order else None
            if status is None:
                raise ContractError("debt_order_unknown")
            if status == "cancelled":
                continue
            if d["day"] <= cutoff:
                paid = sum((a for day, a in d["paid"] if day <= cutoff), Decimal(0))
                result.append((d["customer"], d["day"], d["owed"] - paid))
    return result


def _debt(question, context, text, window):
    if "installment" not in context.rules.rules:
        return None
    if not re.search(r"\b(?:con no|cong no|con thieu|no chua)\b", text) or (
        installment_question(question, text)
        or re.search(r"\bkhach (?!con|hang|co|dang|chua)\w+\b", text)
    ):
        return None
    cutoff = _cutoff(question)
    if cutoff is None or cutoff > question.as_of_date:
        return None
    book = debts(context, cutoff)
    if not book:
        return None
    plan = [
        (c["customer"], c["start"], c["owed"] - _paid(c, cutoff))
        for c in _live(context, contracts(context), cutoff)
    ]
    owed = [(k, day, v) for k, day, v in book + plan if v != 0]
    if any(v < 0 for _, _, v in owed):
        raise ContractError("debt_overpaid")
    if re.search(r"\btuoi no\b", text):
        if not re.search(r"0\s*[-–]\s*7\b.*8\s*[-–]\s*15\b.*\btren 15\b", text):
            return None
        groups = [Decimal(0)] * 3
        for _, day, v in owed:
            age = (date.fromisoformat(cutoff) - date.fromisoformat(day)).days
            groups[0 if age <= 7 else 1 if age <= 15 else 2] += v
        return "phone_debt_age_groups", [int(g) for g in groups], "list"
    if re.search(r"\b(?:may|bao nhieu|bn|so) (?:khach|nguoi)\b", text):
        return "phone_debt_customers", len({k for k, _, _ in owed}), "number"
    if re.search(r"\btien\b|\btong\b", text):
        return "phone_debt_total", int(sum(v for _, _, v in owed)), "number"
    return None
