"""Mother-and-baby shop rules of the rulebook ("Mẹ và bé").

The product list (columns "Tên hàng", "Tên gọi khác", "Nhóm", "Hãng", "Số / Size", "ĐVT",
"Quy đổi") gives each item its group, brand, size and base unit. Sale lines and the invoice
rows "Voucher", "Dùng <n> điểm", "Phí ship" are read from the sales book; invoice dates and
statuses come from the prepared facts. Rulebook meanings used:

- size/number of an item is the list's "Số / Size" column only; a share of a size or a brand
  divides by all bags of the diaper group in the same period;
- voucher, points and shipping amounts are positive, summed by invoice date, over invoices
  neither cancelled nor returned;
- new members of a month are customers whose registration date falls in it.

Group revenue is checked against the prepared facts' revenue of the whole shop before use.
"""

from __future__ import annotations

import re
from collections import defaultdict
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal

from src.agent.books import money, table
from src.agent.purchasing import _day
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain

PRODUCT_COLUMNS = {"ten hang", "nhom", "hang", "so / size", "dvt"}
DIAPERS = "bim ta"
ADJUSTMENTS = {
    "voucher": (r"\b(?:voucher|vau cho)\b", r"^voucher\b"),
    "points": (r"\b(?:dung|tru|doi) (?:\d+ )?diem\b|\bdiem\b", r"^dung \d+ diem\b"),
    "shipping": (r"\bphi (?:ship|giao(?: hang)?)\b", r"^phi (?:ship|giao)\b"),
}


def _percent(part: Decimal, whole: Decimal) -> float:
    return float((part * 100 / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def products(context: BusinessContext) -> dict[str, dict]:
    """Accentless name or alias -> item record from the product list."""
    found: dict[str, dict] = {}
    for rows, _ in table(context, PRODUCT_COLUMNS):
        for row in rows:
            if "ten hang" not in row:
                continue
            name = str(row["ten hang"][0]).strip()
            conversion = {}
            for part in str(row.get("quy doi", ("",))[0]).split(";"):
                if m := re.fullmatch(r"\s*1\s+(.+?)\s*=\s*(\d+)\s+(.+?)\s*", part):
                    conversion[plain(m[1])] = (int(m[2]), plain(m[3]))
            record = {
                "name": name,
                "group": plain(str(row.get("nhom", ("",))[0])),
                "brand": str(row.get("hang", ("",))[0]).strip(),
                "size": plain(str(row.get("so / size", ("",))[0])),
                "unit": plain(str(row.get("dvt", ("",))[0])),
                "conversion": conversion,
            }
            if any(base != record["unit"] for _, base in conversion.values()):
                raise ContractError("baby_conversion_base_mismatch")
            aliases = str(row.get("ten goi khac", ("",))[0]).split(",")
            for key in {plain(name), *(plain(a) for a in aliases if a.strip())}:
                if found.setdefault(key, record) is not record:
                    raise ContractError("baby_alias_conflict")
    return found


def _sales(context: BusinessContext):
    """(invoice, item name as written, unit, quantity, amount, channel) of every book row."""
    for rows, _ in table(context, {"hd", "ten hang", "tt"}):
        for row in rows:
            if "hd" not in row:
                if "tt" in row or "sl" in row:
                    raise ContractError("baby_row_without_invoice")
                continue  # an empty or label-only row
            yield (
                str(row["hd"][0]).strip(),
                str(row["ten hang"][0]).strip() if "ten hang" in row else "",
                plain(str(row["dvt"][0])) if "dvt" in row else None,
                Decimal(str(row["sl"][0])) if "sl" in row else None,
                money(row["tt"][0]) if "tt" in row else None,
                plain(str(row["kenh"][0])) if "kenh" in row else None,
            )


def _invoices(context, check) -> set[str]:
    window, statuses = check["effective_window"], list(check["contract"]["status_policy"])
    with closing(context.prepared.connect()) as conn:
        return {
            str(raw).strip()
            for (raw,) in conn.execute(
                f"SELECT raw_order_id FROM orders WHERE status IN ({','.join('?' * len(statuses))})"
                " AND substr(ordered_at,1,10) BETWEEN ? AND ?",
                [*statuses, window["start"], window["end"]],
            )
        }


def _item_lines(context, check, items):
    """(record, base quantity, amount, channel) of the item lines of the period's invoices."""
    invoices = _invoices(context, check)
    for invoice, name, unit, quantity, amount, channel in _sales(context):
        if invoice not in invoices or any(
            re.search(p, plain(name)) for _, p in ADJUSTMENTS.values()
        ):
            continue
        record = items.get(plain(name))
        if record is None or quantity is None or amount is None:
            raise ContractError("baby_line_unresolved")
        if unit == record["unit"]:
            base = quantity
        elif unit in record["conversion"]:
            base = quantity * record["conversion"][unit][0]
        else:
            raise ContractError("baby_line_unit_unknown")
        yield record, base, amount, channel


def answer(question: Question, context: BusinessContext, text: str):
    """(op, value, type) for a mother-and-baby question, or None."""
    from src.agent.rule_ops import _executor_value, _window

    rules = context.rules.rules if context.rules is not None else {}
    if not context.prepared or "baby_size" not in rules:
        return None
    if not any(
        PRODUCT_COLUMNS <= {plain(str(c["display_name"])) for c in t.get("columns", [])}
        for t in context.catalog["tables"]
    ):
        return None
    adjustment = any(re.search(cue, text) for cue, _ in ADJUSTMENTS.values())
    rest = re.sub(r"\b(?:giam )?cho khach\b|\bkhach (?:dung|tru|doi)\b", " ", text)
    if re.search(r"\b(?:khach|nhan vien|ca sang|ca chieu|trung binh|binh quan)\b", rest):
        if not re.search(r"\b(?:thanh vien moi|dang ky)\b", text):
            return None
    if re.search(r"\bkhach\b", rest) and not adjustment and not re.search(r"\bdang ky\b", text):
        return None
    if re.search(r"\b(?:thanh vien moi|dang ky moi|moi dang ky|dang ky thanh vien)\b", text):
        return _members(question, context, text, rules)
    check = None

    def window():
        nonlocal check
        check = check or _window(question, context)
        return check if isinstance(check, dict) else None

    for kind, (cue, _) in ADJUSTMENTS.items():
        if re.search(cue, text) and "invoice_adjustments" in rules:
            return _adjustment(context, window(), kind, text)
    items = products(context)
    if re.search(r"\bbim\b", text):
        return _diapers(context, window(), items, text)
    if re.search(r"\bsua bot so \d\b", text):
        return _milk(context, window(), items, text)
    group = _group(items, text)
    channel = re.search(r"\bsan (?:thuong mai dien tu|tmdt)\b", text)
    if channel and re.search(r"\bzalo\b|\btai (?:quay|tiem|cua hang)\b|\bqua dien thoai\b", text):
        return None  # several channels: the shop-wide channel operator answers per channel
    if group or channel:
        c = window()
        if c is None:
            return None
        lines = list(_item_lines(context, c, items))
        whole = sum(a for _, _, a, _ in lines)
        # The book's item lines must give the facts' own revenue of the period.
        if whole != _executor_value(context, c, rounded=False):
            return None
        if group:
            part = sum(a for r, _, a, _ in lines if r["group"] == group)
        else:
            part = sum(
                a
                for _, _, a, ch in lines
                if ch and re.fullmatch(r"san (?:tmdt|thuong mai dien tu)", ch)
            )
        if re.search(r"%|\bphan tram\b|\bchiem\b|\bdong gop\b", text):
            return ("baby_share", _percent(part, whole), "percent") if whole else None
        if re.search(r"\b(?:doanh thu|dthu|tien)\b", text):
            return "baby_group_revenue", int(part), "number"
    return None


def _group(items, text):
    groups = {r["group"] for r in items.values() if r["group"]}
    named = [g for g in groups if re.search(r"\bnhom (?:hang )?" + re.escape(g) + r"\b", text)]
    return max(named, key=len) if named else None


def _adjustment(context, check, kind, text):
    if check is None or re.search(r"%|\bphan tram\b|\btrung binh\b", text):
        return None
    invoices = _invoices(context, check)
    pattern = ADJUSTMENTS[kind][1]
    total = Decimal(0)
    for invoice, name, _, _, amount, _ in _sales(context):
        if invoice in invoices and re.search(pattern, plain(name)):
            if amount is None:
                raise ContractError("baby_adjustment_amount_missing")
            total += abs(amount)
    return f"baby_{kind}", int(total), "number"


def _diapers(context, check, items, text):
    if check is None:
        return None
    lines = [(r, b) for r, b, _, _ in _item_lines(context, check, items) if r["group"] == DIAPERS]
    if not lines:
        return None
    if {r["unit"] for r, _ in lines} != {"bich"}:
        return None
    size = re.search(r"\bsize (nb|s|m|l|xl|xxl|xxxl)\b", text)
    brands = {plain(r["brand"]) for r, _ in lines if r["brand"]}
    brand = [b for b in brands if re.search(r"\bbim (?:ta )?" + re.escape(b) + r"\b", text)]
    if size and brand:
        return None
    if size:
        part = sum(b for r, b in lines if r["size"] == f"size {size[1]}")
    elif len(brand) == 1:
        part = sum(b for r, b in lines if plain(r["brand"]) == brand[0])
    else:
        return None
    if re.search(r"%|\bphan tram\b|\bchiem\b", text):
        whole = sum(b for _, b in lines)
        return "baby_diaper_share", _percent(part, whole), "percent"
    if re.search(r"\b(?:bao nhieu|may|bn|tong cong) bich\b", text):
        return "baby_diaper_bags", int(part), "number"
    return None


def _milk(context, check, items, text):
    """Powdered milk "số N" (the list's "Số / Size"): cans of all brands, or the brand
    selling the most cans."""
    number = re.search(r"\bsua bot so (\d)\b", text)
    if check is None or not number:
        return None
    lines = [
        (r, b)
        for r, b, _, _ in _item_lines(context, check, items)
        if r["group"] == "sua bot" and r["size"] == f"so {number[1]}"
    ]
    if not lines or {r["unit"] for r, _ in lines} != {"lon"}:
        return None
    if re.search(r"\b(?:hang nao|hang gi)\b", text) and re.search(r"\bnhieu lon nhat\b", text):
        totals = defaultdict(Decimal)
        for r, b in lines:
            totals[r["brand"]] += b
        best = max(totals.values())
        leaders = [k for k, v in totals.items() if v == best]
        return ("baby_milk_brand", leaders[0], "string") if len(leaders) == 1 else None
    if re.search(r"\b(?:bao nhieu|may|bn|tong cong) lon\b", text) and re.search(
        r"\b(?:gop|tat ca|moi|het)\b.*\bhang\b", text
    ):
        return "baby_milk_cans", int(sum(b for _, b in lines)), "number"
    return None


def _members(question, context, text, rules):
    from src.agent.plan import _literal_windows

    if "membership_new" not in rules or not re.search(r"\b(?:so|bao nhieu|may|bn)\b", text):
        return None
    windows = _literal_windows(question)
    if len(windows) != 1 or windows[0]["kind"] != "month":
        return None
    month = f"{windows[0]['year']:04}-{windows[0]['month']:02}"
    found = table(context, {"ngay dang ky"})
    if len(found) != 1:
        return None
    rows, epoch = found[0]
    days = []
    for row in rows:
        if "ngay dang ky" not in row:
            raise ContractError("member_registration_missing")
        raw, cell = row["ngay dang ky"]
        days.append(_day(raw, cell, epoch))
    if month > question.as_of_date[:7]:
        return None
    count = defaultdict(int)
    for d in days:
        if d <= question.as_of_date:  # registrations after today have not happened yet
            count[d[:7]] += 1
    return "baby_new_members", count[month], "number"
