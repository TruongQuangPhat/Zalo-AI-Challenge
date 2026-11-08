"""Deterministic answers for rulebook intents the QueryPlan cannot express.

Each operator recognizes one bounded question shape, checks that the shop's prepared
facts carry what its rule needs, and computes the value exactly. Anything else returns
None so the model planner and its guards decide. Operators read question text, the
rulebook-backed revenue contract and prepared facts only; never labels or IDs.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from src.agent.plan import _literal_windows
from src.contracts import validate_answer
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.answerability import preflight
from src.semantics.rulebook import plain, question_plain

ASK = r"(?:bao nhieu|bao nhiu|bnhiu|bnhieu|bn|may|nhieu|tong cong|tong so|so)"
# Conditions none of these operators bind; their presence leaves the question to the planner.
QUALIFIERS = (
    r"\b(nhom|khach|ca sang|ca chieu|nhan vien|kenh|tien mat|chuyen khoan|cod|huy|"
    r"tra hang|don tra|trung binh|binh quan|moi ngay|ti le|ty le|ti trong|ty trong|"
    r"phan tram|top|so voi|tang|giam|chenh|ngoai tru|khong tinh|rieng|qua dien thoai|"
    r"tai quay|zalo|nhap|ton|con lai|gia ban|don gia|lai|loi)\b|%"
)
NOT_UNITS = {"don", "tien", "dot", "ngay", "lan", "bill", "phieu", "khach", "mon", "nguoi"}
# Snapshot questions ("trong kho còn mấy…") belong to stock, not sold quantity.
STOCK = r"\b(?:kho|ton|con (?:lai|bao|may|nhieu)|cuoi ngay|het hom nay)\b"
DAY_COUNT = r"\b(?:so ngay|may ngay|bao nhieu ngay|bao nhiu ngay|bn ngay|bnhiu ngay|nhieu ngay)\b"
AMOUNT = r"(\d[\d.,]*)\s*(trieu|tr|k|nghin|ngan)?\b"
COMPARATORS = (
    (r"\btu " + AMOUNT + r"(?: dong| d| vnd)? tro len\b", ">="),
    (r"\btu " + AMOUNT + r"(?: dong| d| vnd)? tro xuong\b", "<="),
    (r"(?:>=|≥)\s*" + AMOUNT, ">="),
    (r"(?:<=|≤)\s*" + AMOUNT, "<="),
    (r"\b(?:it nhat|toi thieu) " + AMOUNT, ">="),
    (r"\b(?:tren|hon|qua|vuot|lon hon) " + AMOUNT, ">"),
    (r"\b(?:duoi|nho hon|it hon) " + AMOUNT, "<"),
)
METHODS = {
    "cash": (r"\b(?:tien mat|tm)\b", {"cash", "tien mat"}),
    "transfer": (r"\bchuyen khoan\b", {"transfer", "chuyen khoan"}),
    "ewallet": (r"\bvi dien tu\b", {"ewallet", "vi dien tu"}),
    "cod": (r"\b(?:cod|thu ho)\b", {"cod", "thu ho (cod)", "thu ho"}),
}


def answer_by_rule(question: Question, context: BusinessContext) -> dict | None:
    if not context.prepared or context.prepared.path is None:
        return None
    text = question_plain(question.question)
    for operator in (
        _size_quantity,
        _unit_quantity,
        _threshold_count,
        _best_period,
        _method_revenue,
    ):
        try:
            result = operator(question, context, text)
        except (ContractError, sqlite3.Error, ArithmeticError, ValueError, KeyError):
            result = None
        if result is not None:
            name, value, answer_type, details = result
            answer = (
                {"id": question.id, "type": "unanswerable", "reason": value}
                if answer_type == "unanswerable"
                else {"id": question.id, "type": answer_type, "answer": value}
            )
            validate_answer(answer)
            return {
                "answer": answer,
                "plan": {"status": "rule_op", "op": name, **details},
                "execution": {"status": "verified", "operator": name},
            }
    return None


def _window(question: Question, context: BusinessContext, expression: dict | None = None):
    """Revenue contract and effective window, or an unanswerable/None decision."""
    if expression is None:
        expressions = _literal_windows(question)
        if len(expressions) != 1:
            return None
        expression = expressions[0]
    check = preflight(context, "revenue", expression, as_of_date=question.as_of_date)
    if check["decision"] == "unanswerable":
        return "unanswerable", check["reason"]
    if check["decision"] == "abstain" or not check.get("effective_window"):
        return None
    return check


def _rows(context: BusinessContext, check: dict):
    contract = check["contract"]
    statuses = list(contract["status_policy"])
    window = check["effective_window"]
    sql = (
        "SELECT o.order_key,substr(o.ordered_at,1,10),l.net_amount,l.row_role,e.name,"
        "e.base_unit,i.base_quantity,i.sold_quantity FROM orders o "
        "JOIN order_lines l USING(order_key) LEFT JOIN line_items i USING(line_key) "
        "LEFT JOIN entities e ON e.entity_key=i.product_key "
        f"WHERE o.status IN ({','.join('?' for _ in statuses)}) "
        "AND substr(o.ordered_at,1,10) BETWEEN ? AND ?"
    )
    with closing(context.prepared.connect()) as conn:
        yield from conn.execute(sql, [*statuses, window["start"], window["end"]])


def _quantity(base, sold) -> Decimal:
    raw = base if base is not None else sold
    if raw is None:
        raise ContractError("quantity_missing")
    return Decimal(str(raw))


def _money(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _decision(name, check, value, answer_type, **details):
    if isinstance(check, tuple):
        return name, check[1], "unanswerable", details
    window = check["effective_window"]
    return name, value, answer_type, {"window": [window["start"], window["end"]], **details}


def _products(context: BusinessContext) -> list[tuple[str, str | None]]:
    with closing(context.prepared.connect()) as conn:
        return [
            (plain(name), plain(unit) if unit else None)
            for name, unit in conn.execute(
                "SELECT name,base_unit FROM entities WHERE kind='product'"
            )
        ]


def _size_quantity(question, context, text):
    """Cà phê rule: size M/L = items whose name ends with (M)/(L), any case."""
    size = re.search(r"\bsize (m|l)\b", text)
    if not size or not re.search(r"\bly\b", text) or re.search(QUALIFIERS, text):
        return None
    suffix = f"({size[1]})"
    if not any(name.endswith(suffix) for name, _ in _products(context)):
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("size_quantity", check, None, "number")
    total = Decimal(0)
    for _, _, _, role, name, _, base, sold in _rows(context, check):
        if role == "sale_item" and not name:
            raise ContractError("sale_line_product_unknown")
        if role == "sale_item" and plain(name).endswith(suffix):
            total += _quantity(base, sold)
    return _decision("size_quantity", check, int(total), "number", size=size[1])


def _unit_quantity(question, context, text):
    """§3: "bán được bao nhiêu <đơn vị>" sums base quantity of items with that base unit."""
    products = _products(context)
    # Base units that are also ordinary question words never name a quantity here.
    units = {unit for _, unit in products if unit} - NOT_UNITS
    if not units or re.search(r"\bsize\b|" + STOCK, text):
        return None
    pattern = (
        r"\b"
        + ASK
        + r" ("
        + "|".join(sorted(map(re.escape, units), key=len, reverse=True))
        + r")\b(?! nhi)"  # "tong cong bao nhieu": "bao" is the question word, not a unit
    )
    found = re.search(pattern, text)
    if not found:
        return None
    unit = found[1]
    named = [name for name, _ in products if len(name.split()) >= 2 and name in text]
    named = sorted(named, key=len, reverse=True)[:1]
    if named:
        target = named[0]
        if dict(products).get(target) != unit:
            return None
        rest = text.replace(target, " ")
    else:
        target, rest = None, text
    if re.search(QUALIFIERS, rest):
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("unit_quantity", check, None, "number")
    total = Decimal(0)
    for _, _, _, role, name, base_unit, base, sold in _rows(context, check):
        if role != "sale_item":
            continue
        if not name:
            raise ContractError("sale_line_product_unknown")
        if (target and plain(name) == target) or (
            not target and base_unit and plain(base_unit) == unit
        ):
            total += _quantity(base, sold)
    if total != total.to_integral_value():
        return None
    return _decision("unit_quantity", check, int(total), "number", unit=unit, product=target)


def _amount(match: re.Match) -> Decimal:
    digits, scale = match[1], match[2]
    if scale in {"trieu", "tr"}:
        value = Decimal(digits.replace(".", "").replace(",", ".")) * 1_000_000
    elif scale in {"k", "nghin", "ngan"}:
        value = Decimal(digits.replace(".", "").replace(",", ".")) * 1_000
    else:
        value = Decimal(re.sub(r"[.,]", "", digits))
    return value


def _compare(value: Decimal, op: str, bound: Decimal) -> bool:
    return {
        ">=": value >= bound,
        "<=": value <= bound,
        ">": value > bound,
        "<": value < bound,
    }[op]


def _threshold_count(question, context, text):
    """Count orders (or days) whose revenue passes a stated bound; §1, §2 boundaries."""
    days = bool(re.search(DAY_COUNT, text))
    orders = bool(re.search(r"\b" + ASK + r" (?:don|bill|hoa don)\b", text))
    if days == orders or not re.search(r"\b(?:doanh thu|dthu|gia tri|tong tien)\b", text):
        return None
    bound = None
    for pattern, op in COMPARATORS:
        found = re.search(pattern, text)
        if found:
            bound = (op, _amount(found))
            break
    if bound is None:
        return None
    rest = re.sub(r"\b(?:nhieu|trung binh|binh quan)\b", " ", text)
    if re.search(
        r"\b(nhom|khach|ca sang|ca chieu|nhan vien|kenh|tien mat|chuyen khoan|huy|zalo)\b", rest
    ):
        return None
    check = _window(question, context)
    if check is None:
        return None
    name = "threshold_days" if days else "threshold_orders"
    if isinstance(check, tuple):
        return _decision(name, check, None, "number")
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    per_order, order_day = defaultdict(Decimal), {}
    for order, day, amount, role, *_ in _rows(context, check):
        order_day[order] = day
        if role in roles and amount is not None:
            per_order[order] += Decimal(str(amount))
    if days:
        per_day = defaultdict(Decimal)
        for order, day in order_day.items():
            per_day[day] += per_order[order]
        window = check["effective_window"]
        start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        all_days = [(start + timedelta(n)).isoformat() for n in range((end - start).days + 1)]
        count = sum(_compare(per_day[d], *bound) for d in all_days)
    else:
        count = sum(_compare(per_order[o], *bound) for o in order_day)
    return _decision(name, check, count, "number", bound=[bound[0], str(bound[1])])


def _best_period(question, context, text):
    """'Ngày nào' / 'tháng nào' with the highest revenue or most orders."""
    by_day = re.search(r"\bngay nao\b", text)
    by_month = re.search(r"\bthang nao\b", text)
    if not (by_day or by_month) or not re.search(r"\b(?:cao nhat|nhieu nhat|lon nhat)\b", text):
        return None
    count_orders = bool(re.search(r"\b(?:nhieu don|dong khach|dat khach|don nhat)\b", text))
    if not count_orders and not re.search(r"\b(?:doanh thu|dthu|tien)\b", text):
        return None
    rest = re.sub(r"\b(?:nhieu|cao|lon) nhat\b", " ", text)
    if re.search(
        r"\b(nhom|khach|ca sang|ca chieu|nhan vien|kenh|tien mat|chuyen khoan|huy|zalo|"
        r"mat hang|mon)\b",
        rest,
    ):
        return None
    expressions = _literal_windows(question)
    if by_month:
        months = [e for e in expressions if e["kind"] == "month"]
        if len(months) == 2 and re.search(r"\b(?:toi|den)\b", text):
            first = months[0]["year"] * 12 + months[0]["month"] - 1
            last = months[1]["year"] * 12 + months[1]["month"] - 1
            months = [
                {"kind": "month", "year": i // 12, "month": i % 12 + 1}
                for i in range(first, last + 1)
            ]
        if len(months) < 2 or any(e["kind"] != "month" for e in expressions):
            return None
        best = None
        for expression in months:
            check = _window(question, context, expression)
            if not isinstance(check, dict):
                return None
            value = _period_value(context, check, count_orders)
            label = f"{expression['year']:04}-{expression['month']:02}"
            if best is None or value > best[0]:
                best = (value, label)
        return "best_month", best[1], "date", {"months": len(months)}
    if len(expressions) != 1:
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("best_day", check, None, "date")
    per_day = defaultdict(Decimal)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    seen = set()
    for order, day, amount, role, *_ in _rows(context, check):
        if count_orders:
            if order not in seen:
                seen.add(order)
                per_day[day] += 1
        elif role in roles and amount is not None:
            per_day[day] += Decimal(str(amount))
    if not per_day:
        return None
    best = max(per_day.values())
    return _decision("best_day", check, min(d for d, v in per_day.items() if v == best), "date")


def _period_value(context, check, count_orders):
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    total, seen = Decimal(0), set()
    for order, _, amount, role, *_ in _rows(context, check):
        if count_orders:
            seen.add(order)
        elif role in roles and amount is not None:
            total += Decimal(str(amount))
    return Decimal(len(seen)) if count_orders else total


def _method_revenue(question, context, text):
    """§1: revenue by the payment method recorded on the order (debt and COD separate).

    One method gives a number; two methods asked together give a list in question order.
    """
    # "don tra bang X" means paid by X, not the returned-order qualifier "don tra".
    text = re.sub(r"\b(?:cac )?don (?:tra|thanh toan) bang\b", " ", text)
    positions = sorted(
        (m.start(), key)
        for key, (pattern, _) in METHODS.items()
        for m in re.finditer(pattern, text)
    )
    found = list(dict.fromkeys(key for _, key in positions))
    if not 1 <= len(found) <= 2 or not re.search(r"\b(?:doanh thu|dthu|tien ban|ban duoc)\b", text):
        return None
    rest = text
    for key in found:
        rest = re.sub(METHODS[key][0], " ", rest)
    if len(found) == 2 and not re.search(r"\b(?:va|voi|moi loai|lan luot|moi ben)\b", rest):
        return None
    if re.search(QUALIFIERS, rest) or (
        len(found) == 1 and re.search(r"\b(?:va|con)\b.*\b(?:don|doanh thu|dthu)\b", rest)
    ):
        return None
    with closing(context.prepared.connect()) as conn:
        methods = {
            order: plain(method or "")
            for order, method in conn.execute("SELECT order_key,payment_method FROM order_parties")
        }
    if not any(methods.values()):
        return None
    check = _window(question, context)
    if check is None:
        return None
    answer_type = "number" if len(found) == 1 else "list"
    if isinstance(check, tuple):
        return _decision("method_revenue", check, None, answer_type)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    totals = dict.fromkeys(found, Decimal(0))
    for order, _, amount, role, *_ in _rows(context, check):
        if order not in methods:
            raise ContractError("order_payment_method_missing")
        if role not in roles or amount is None:
            continue
        for key in found:
            if methods[order] in METHODS[key][1]:
                totals[key] += Decimal(str(amount))
    values = [_money(totals[key]) for key in found]
    value = values[0] if len(values) == 1 else values
    return _decision("method_revenue", check, value, answer_type, methods=found)
