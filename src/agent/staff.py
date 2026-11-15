"""Staff revenue (rulebook "Nhân viên bán giỏi nhất": by §1 revenue, not order count).

The order table's one seller column ("Người bán", "Nhân viên", "Mã NV"…) is joined to
prepared orders by the shop's order code. A staff member is named the way the shop records
them (a code such as NV003, or a full name). Revenue of a staff member is the §1 revenue
of the orders they sold in the period. A revenue order without a seller, a seller that
changes inside one order, or a tie for the best abstains.
"""

from __future__ import annotations

import re
from contextlib import closing
from decimal import Decimal

from src.data.catalog import BusinessContext
from src.jsonio import ContractError
from src.semantics.rulebook import plain

SELLER = re.compile(
    r"^(?:nguoi ban(?: hang)?|nhan vien(?: ban(?: hang)?)?|ma (?:nv|nhan vien)(?: ban)?|"
    r"nv ban|nv)$"
)


def _header(name: str) -> str:
    return plain(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(name)))


def sellers(context: BusinessContext) -> dict[str, str]:
    """Shop order code -> seller as recorded; {} when no single seller column joins."""
    with closing(context.prepared.connect()) as conn:
        codes = {str(c).strip() for (c,) in conn.execute("SELECT raw_order_id FROM orders")}
    found = []
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        columns = {c["position"]: _header(c["display_name"]) for c in profile.get("columns", [])}
        seller = [p for p, n in columns.items() if SELLER.match(n)]
        if len(seller) != 1:
            continue
        header = set(profile.get("header_rows", []))
        rows = [
            r
            for r in context.source_table(profile["source_id"]).iter_rows()
            if r.row not in header and not (header and r.row is not None and r.row < min(header))
        ]

        def text(row, position):
            cell = row.cells.get(position)
            return "" if cell is None or cell.raw_value is None else str(cell.raw_value).strip()

        joins = [
            p
            for p in columns
            if p != seller[0]
            and rows
            and sum(text(r, p) in codes for r in rows) >= 0.95 * len(rows)
        ]
        if len(joins) == 1:
            found.append((rows, joins[0], seller[0], text, (columns[joins[0]], columns[seller[0]])))
    # One sales book, possibly split into parts (one file or sheet per year) of one layout.
    if not found or len({f[4] for f in found}) != 1:
        return {}
    result: dict[str, str] = {}
    for rows, join, seller, text, _ in found:
        for row in rows:
            order, who = text(row, join), text(row, seller)
            if not order:
                continue
            if not who:
                raise ContractError("order_seller_missing")
            if result.setdefault(order, who) != who:
                raise ContractError("order_seller_conflict")
    return result


def staff_names(context: BusinessContext, codes: set[str]) -> dict[str, str]:
    """Full name -> seller code from a list with one name per code; {} unless it covers
    every recorded code."""
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        rows = list(context.source_table(profile["source_id"]).iter_rows())
        header = set(profile.get("header_rows", []))
        rows = [r for r in rows if r.row not in header]
        positions = [c["position"] for c in profile.get("columns", [])]

        def values(position, rows=rows):
            return [
                str(r.cells[position].raw_value).strip()
                for r in rows
                if position in r.cells and r.cells[position].raw_value not in {None, ""}
            ]

        for code_at in positions:
            column = values(code_at)
            if not column or not codes <= set(column) or len(set(column)) != len(column):
                continue
            for name_at in positions:
                if name_at == code_at:
                    continue
                pairs = {
                    str(r.cells[code_at].raw_value).strip(): str(r.cells[name_at].raw_value).strip()
                    for r in rows
                    if code_at in r.cells and name_at in r.cells
                }
                names = [pairs.get(c, "") for c in codes]
                if all(len(n.split()) >= 2 and not re.search(r"\d", n) for n in names) and len(
                    set(map(plain, names))
                ) == len(names):
                    return {pairs[c]: c for c in codes}
    return {}


def answer(question, context: BusinessContext, text: str):
    """(op, value, type, details) for one staff member's revenue or the best seller."""
    from src.agent.rule_ops import QUALIFIERS, _decision, _money, _window

    if not re.search(r"\bnhan vien\b|\bnv\b|\bnv\d", text) or not context.prepared:
        return None
    text = re.sub(r"\bnv\b(?!\d)", "nhan vien", text)
    text = re.sub(r"\bcong lai\b", "cong", text)  # "cộng lại" is a sum, not "lãi"
    best = re.search(r"\b(?:ban gioi nhat|doanh thu cao nhat|ban duoc nhieu tien nhat)\b", text)
    money = re.search(
        r"\b(?:ban duoc|doanh thu|dthu|tien hang)\b.*\b(?:bao nhieu|tien)\b|"
        r"\bban\b.*\b(?:bao nhieu|bn|nhieu) tien\b|\btong tien hang\b",
        text,
    )
    if not best and not money:
        return None
    if re.search(
        r"\b(?:so don|ngay|cham chi|khach|mat hang|san pham|nhom|%|phan tram|"
        r"ca (?:sang|chieu|toi|lam))\b",
        text,
    ):
        return None
    if re.search(QUALIFIERS, re.sub(r"\bnhan vien\b", " ", text)):
        return None
    staff = sellers(context)
    if not staff:
        return None
    names = {plain(v): v for v in set(staff.values())}
    if len(names) != len(set(staff.values())):
        return None
    # A staff list naming each recorded seller code lets the question use the name.
    for name, code in staff_names(context, set(staff.values())).items():
        names.setdefault(plain(name), code)
    wanted = None
    if not best:
        named = [
            v for k, v in names.items() if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", text)
        ]
        if len(named) != 1:
            return None
        wanted = named[0]
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("staff", check, None, "string" if best else "number")
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    statuses = list(check["contract"]["status_policy"])
    window = check["effective_window"]
    totals: dict[str, Decimal] = {}
    with closing(context.prepared.connect()) as conn:
        rows = conn.execute(
            "SELECT o.raw_order_id,l.net_amount,l.row_role FROM orders o JOIN order_lines l "
            f"USING(order_key) WHERE o.status IN ({','.join('?' for _ in statuses)}) "
            "AND substr(o.ordered_at,1,10) BETWEEN ? AND ?",
            [*statuses, window["start"], window["end"]],
        ).fetchall()
    for order, amount, role in rows:
        if role not in roles or amount is None:
            continue
        who = staff.get(str(order).strip())
        if who is None:
            raise ContractError("revenue_order_without_seller")
        totals[who] = totals.get(who, Decimal(0)) + Decimal(str(amount))
    if best:
        if not totals:
            return None
        top = max(totals.values())
        leaders = [k for k, v in totals.items() if v == top]
        if len(leaders) != 1:
            return None
        return _decision("staff_best", check, leaders[0], "string")
    return _decision("staff_revenue", check, _money(totals.get(wanted, Decimal(0))), "number")
