"""Deterministic answers for rulebook intents the QueryPlan cannot express.

Each operator recognizes one bounded question shape, checks that the shop's prepared
facts carry what its rule needs, and computes the value exactly. Anything else returns
None so the model planner and its guards decide. Operators read question text, the
rulebook-backed revenue contract and prepared facts only; never labels or IDs.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from src.agent.plan import _literal_windows
from src.contracts import validate_answer
from src.data.catalog import BusinessContext
from src.data.normalization.values import PURCHASE_CANCELLED, PURCHASE_RECEIVED
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
# "180.000đ" writes the currency letter against the number.
AMOUNT = r"(\d[\d.,]*)\s*(trieu|tr|k|nghin|ngan)?(?:d\b|\b)"
COMPARATORS = (
    (r"\b(?:tu|dat) " + AMOUNT + r"(?: dong| d| vnd)? tro len\b", ">="),
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
    from src.agent.plan import unparsed_time

    text = question_plain(question.question)
    unrecorded = _unrecorded_attribute(context, text)
    if unrecorded is not None:
        answer = {"id": question.id, "type": "unanswerable", "reason": unrecorded}
        validate_answer(answer)
        return {
            "answer": answer,
            "plan": {"status": "rule_op", "op": "unrecorded_attribute"},
            "execution": {"status": "verified", "operator": "unrecorded_attribute"},
        }
    # An unparsed period would let an operator answer for some other period.
    if not context.prepared or context.prepared.path is None or unparsed_time(question):
        return None
    if repair_ticket_question(context, text):
        from src.agent.repair import answer as repair_answer

        try:
            result = repair_answer(question, context, text)
        except (ContractError, sqlite3.Error, ArithmeticError, ValueError, KeyError):
            result = None
        if result is None:
            return None
        name, value, answer_type = result
        details = {}
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
    for operator in (
        _industry_amounts,
        _pharmacy,
        _debt,
        _staff,
        _purchasing,
        _dead_stock,
        _stockout,
        _stock_value,
        _size_quantity,
        _unit_quantity,
        _threshold_count,
        _best_period,
        _method_revenue,
        _channel_revenue,
        _items_in_orders,
        _cancellation,
        _product_ranking,
        _group_revenue,
        _rulebook_rankings,
        _purchase_total,
        _gross_profit,
        _customer_ops,
        _growth,
        _month_list,
        _price_effect,
        _trend,
        _product_revenue,
        _whole_shop,
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
    return _shift_plan(question, context) or _business_plan(question, context)


def _industry_amounts(question, context, text):
    """Phone-shop trade-in and customer-payable totals (rulebook "Thu máy cũ")."""
    from src.agent.industry import answer as industry_answer

    result = industry_answer(question, context, text)
    return None if result is None else (*result, {})


def _pharmacy(question, context, text):
    from src.agent.pharmacy import answer

    return answer(question, context, text)


def _debt(question, context, text):
    from src.agent.debt import answer

    return answer(question, context, text)


def _day_balances(question, context, expression):
    """(check, product key -> balance at the end of the day) under the stock executor's
    authority, or (decision tuple / None, None)."""
    check = preflight(context, "stock_balance", expression, as_of_date=question.as_of_date)
    if check["decision"] == "unanswerable":
        return ("unanswerable", check["reason"]), None
    if check["decision"] != "eligible_to_execute":
        return None, None
    extension = context.prepared.summary.get("extensions", {})
    if "stock" in extension.get("issues", {}):
        return None, None
    if extension.get("stock_mode") != "snapshot" and (
        "ton o dong cuoi cua san pham tinh den thoi diem do" not in plain(context.catalog["readme"])
    ):
        return None, None  # same authority as the stock executor
    day = check["effective_window"]["end"]
    latest: dict[str, tuple[str, Decimal]] = {}
    with closing(context.prepared.connect()) as conn:
        for product, at, balance in conn.execute(
            "SELECT product_key,occurred_at,balance FROM stock_balances "
            "WHERE substr(occurred_at,1,10)<=? ORDER BY product_key,occurred_at",
            (day,),
        ):
            value = Decimal(str(balance))
            old = latest.get(product)
            if old and old[0] == at and old[1] != value:
                raise ContractError("simultaneous_stock_balances_ambiguous")
            latest[product] = (at, value)
    return check, {k: v for k, (_, v) in latest.items()}


def _catalog_costs(context) -> dict[str, Decimal]:
    """Product code -> cost of one base unit from the product list ("Giá vốn"), else {}."""
    from src.agent.purchasing import _header, _number
    from src.data.normalization.entities import source_fields

    found = []
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        fields = source_fields(context, profile)
        if "product_id" not in fields or "order_id" in fields or "purchase_id" in fields:
            continue
        cost = [
            c["position"]
            for c in profile.get("columns", [])
            if re.fullmatch(r"gia von(?: hang| hien tai)?", _header(c["display_name"]))
        ]
        if len(cost) == 1:
            found.append((profile, fields["product_id"], cost[0]))
    if len(found) != 1:
        return {}
    profile, code_at, cost_at = found[0]
    header = set(profile.get("header_rows", []))
    costs: dict[str, Decimal] = {}
    for row in context.source_table(profile["source_id"]).iter_rows():
        if row.row in header or (header and row.row is not None and row.row < min(header)):
            continue
        code, cost = row.cells.get(code_at), row.cells.get(cost_at)
        if code is None or code.raw_value in {None, ""}:
            continue
        if cost is None or cost.raw_value in {None, ""}:
            raise ContractError("catalog_cost_missing")
        key = str(code.raw_value).strip()
        value = _number(cost.raw_value)
        if costs.setdefault(key, value) != value:
            raise ContractError("catalog_cost_conflict")
    return costs


def _stock_value(question, context, text):
    """§5 "Giá trị hàng tồn kho": each item's balance at the end of the day times the cost
    of one base unit in the product list; only items still in stock."""
    if not re.search(r"\bgia von\b", text) or not re.search(r"\b(?:kho|ton)\b", text):
        return None
    if not re.search(r"\b(?:tien|gia tri|dang bao nhieu)\b", text):
        return None
    rules = context.rules.rules if context.rules is not None else {}
    if "stock_valuation" not in rules or re.search(r"\b(?:nhom|lo|han dung|mat hang nao)\b", text):
        return None
    expressions = _literal_windows(question)
    if len(expressions) != 1 or expressions[0]["kind"] not in {"day", "today"}:
        return None
    check, balances = _day_balances(question, context, expressions[0])
    if isinstance(check, tuple):
        return _decision("stock_value", check, None, "number")
    if check is None or not balances:
        return None
    costs = _catalog_costs(context)
    if not costs:
        return None
    with closing(context.prepared.connect()) as conn:
        codes = dict(conn.execute("SELECT entity_key,source_code FROM entities"))
    total = Decimal(0)
    for product, balance in balances.items():
        if balance <= 0:
            continue
        code = codes.get(product)
        if code is None or str(code).strip() not in costs:
            raise ContractError("stock_item_cost_unknown")
        total += balance * costs[str(code).strip()]
    return _decision("stock_value", check, _money(total), "number")


def _stockout(question, context, text):
    """§5 "Mặt hàng hết hàng cuối ngày": stocked items whose balance at the end of day D
    is 0 (a stock record marks an item bought to sell); counted."""
    if not re.search(r"\b(?:het hang|dut hang|chay hang)\b", text) or not re.search(
        r"\b(?:mat hang|mon|san pham)\b", text
    ):
        return None
    if not re.search(r"\b(?:bao nhieu|bao nhiu|bnhiu|bn|may|so|dem)\b", text):
        return None
    rules = context.rules.rules if context.rules is not None else {}
    if "stockout" not in rules or re.search(r"\b(?:nhom|nao|ten|liet ke)\b|%", text):
        return None
    expressions = _literal_windows(question)
    if len(expressions) != 1 or expressions[0]["kind"] not in {"day", "today"}:
        return None
    check, balances = _day_balances(question, context, expressions[0])
    if isinstance(check, tuple):
        return _decision("stockout", check, None, "number")
    if check is None or not balances:
        return None
    return _decision("stockout", check, sum(v == 0 for v in balances.values()), "number")


def _dead_stock(question, context, text):
    """§5 "Mặt hàng không bán được trong kỳ (ế)": items sold on a §1 order before the
    period that no §1 order of the period bought; counted."""
    if not re.search(r"\b(?:mat hang|mon)\b", text) or not re.search(
        r"\b(?:hang|mon|bi) e\b|\bban cham\b|"
        r"\bkhong (?:ai )?(?:ban|mua) (?:duoc |dc |ra )?(?:cai|mon|lan) nao\b|"
        r"\bkhong ban ra\b",
        text,
    ):
        return None
    if not re.search(r"\b(?:bao nhieu|bao nhiu|bnhiu|bn|may|so|dem|tong cong)\b", text):
        return None
    rules = context.rules.rules if context.rules is not None else {}
    if "dead_stock" not in rules or re.search(r"\b(?:nhom|khach|nhan vien|kenh)\b|%", text):
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("dead_stock", check, None, "number")
    window = check["effective_window"]
    if (window["start"], window["end"]) != (
        check["window"]["requested_start"],
        min(check["window"]["requested_end"], check["window"]["cutoff"] or "9999"),
    ):
        return None  # a clipped period changes which items count as unsold
    statuses = list(check["contract"]["status_policy"])
    sql = (
        "SELECT i.product_key,substr(o.ordered_at,1,10)<? FROM orders o "
        "JOIN order_lines l USING(order_key) LEFT JOIN line_items i USING(line_key) "
        f"WHERE l.row_role='sale_item' AND o.status IN ({','.join('?' for _ in statuses)}) "
        "AND substr(o.ordered_at,1,10)<=?"
    )
    before, during = set(), set()
    with closing(context.prepared.connect()) as conn:
        for product, earlier in conn.execute(sql, [window["start"], *statuses, window["end"]]):
            if product is None:
                raise ContractError("sale_line_product_unknown")
            (before if earlier else during).add(product)
    if not before:
        return None
    return _decision("dead_stock", check, len(before - during), "number")


def _purchasing(question, context, text):
    from src.agent.purchasing import answer

    return answer(question, context, text)


def _staff(question, context, text):
    from src.agent.staff import answer

    return answer(question, context, text)


TICKET_CUES = r"\b(?:phieu|luot|tho|xe|bao hanh|hoa hong|hue hong|bien so)\b"


def repair_ticket_question(context: BusinessContext, text: str) -> bool:
    """A repair shop (rulebook repair rules, README "sửa xe") asked about tickets, visits,
    mechanics or averages: its industry rules define these differently from §1 orders."""
    rules = context.rules.rules if context.rules is not None else {}
    readme = plain((getattr(context, "catalog", None) or {}).get("readme", ""))
    if "repair_visits" not in rules or "sua xe" not in readme:
        return False
    return bool(re.search(TICKET_CUES, text) or re.search(AOV, text))


def _shift_plan(question: Question, context: BusinessContext) -> dict | None:
    """Rulebook shifts (ca sáng, ca chiều by order clock) as a QueryPlan, without the model.

    One shift's revenue or order count, or afternoon against morning (difference or percent
    change, the baseline named by the question). The same validator, preflight and executor
    run; an order without a clock abstains there.
    """
    from src.agent.execution import execute_plan
    from src.agent.plan import _metric_cues, _period_measure, _shift_text, _single_shift

    text = _shift_text(question)
    if not re.search(r"\bca (?:sang|chieu)\b", text) or re.search(
        r"\b(?:nhan vien|khach|nhom|kenh|mat hang|mon|san pham|nhat|top|trung binh|binh quan|"
        r"moi ngay|ngay nao|thang nao|ty le|ti le|huy|tra hang)\b",
        text,
    ):
        return None
    expressions = _literal_windows(question)
    if len(expressions) != 1:
        return None
    measure = _period_measure(question)
    if measure and measure.get("operand_filters"):
        metric = measure["metrics"][0]
        filters = measure["operand_filters"]
        derived = [
            {
                "id": "change",
                "operator": measure["operator"],
                "current_ref": "current",
                "baseline_ref": "baseline",
            }
        ]
        refs = ["change"]
        answer_type = "percent" if measure["operator"] == "percent_change" else "number"
        ids = ["current", "baseline"]
    else:
        single = _single_shift(question)
        metrics = _metric_cues(question)
        if single is None or len(metrics) != 1 or re.search(r"%|phan tram|chenh|so voi", text):
            return None
        metric, filters, derived = next(iter(metrics)), [single["filters"]], []
        refs = ids = ["total"]
        answer_type = "number"
    if metric not in {"revenue", "order_count"}:
        return None
    draft = {
        "entities": [],
        "time_windows": [{"id": "period0", "expression": expressions[0]}],
        "aggregations": [
            {
                "id": i,
                "metric_id": metric,
                "window_ref": "period0",
                "scope": "whole_business",
                "filters": f,
                "group_by": [],
            }
            for i, f in zip(ids, filters, strict=True)
        ],
        "derived_measures": derived,
        "output": {"expected_type": answer_type, "result_refs": refs},
        "uncertainties": [],
    }
    try:
        decision = execute_plan(draft, question, context)
    except (ContractError, sqlite3.Error, ArithmeticError, ValueError, KeyError):
        return None
    if decision["answer"]["type"] == "abstain":
        return None
    decision["plan"]["status"] = "rule_op"
    decision["plan"]["op"] = "shift_" + metric
    return decision


def _business_plan(question: Question, context: BusinessContext) -> dict | None:
    """The bounded business QueryPlan, built from ``business_spec`` without the model.

    The planner's constrained schema leaves the model only the entity choice; here that
    choice is the single grounded candidate. The same validator, preflight and reducers
    run, so any doubt still abstains (returned as ``None``).
    """
    from src.agent.entities import business_spec, entity_candidates
    from src.agent.execution import execute_plan

    spec = business_spec(question)
    # Only the business metrics measured on train without a wrong answer run here; entity
    # quantities and revenues already have their own operators above.
    if (
        spec is None
        or spec.get("trend")
        or spec["metric"]
        not in {
            "receivable",
            "receivable_customer_count",
            "debt_collection",
            "purchase_amount",
            "lot_expiry",
            "stock_balance",
            "gross_profit",
        }
    ):
        return None
    needs_entity = spec["group"] is None and spec["metric"] in {
        "quantity",
        "stock_balance",
        "revenue",
        "lot_expiry",
    }
    entities = []
    if needs_entity:
        candidates = entity_candidates(context, question.question)
        if len(candidates) != 1:
            return None
        entities = [candidates[0]["entity_key"]]
        if spec["metric"] == "stock_balance" and not _base_unit_asked(
            question, candidates[0].get("base_unit")
        ):
            return None
    aggregations = [
        {
            "id": f"total{i}",
            "metric_id": spec["metric"],
            "window_ref": f"period{i}",
            "scope": "whole_business",
            "filters": [],
            "group_by": [spec["group"]] if spec["group"] else [],
            **({"limit": spec["limit"]} if spec["limit"] else {}),
        }
        for i in range(len(spec["expressions"]))
    ]
    draft = {
        "entities": entities,
        "time_windows": [
            {"id": f"period{i}", "expression": e} for i, e in enumerate(spec["expressions"])
        ],
        "aggregations": aggregations,
        "derived_measures": [],
        "output": {"expected_type": spec["type"], "result_refs": [a["id"] for a in aggregations]},
        "uncertainties": [],
    }
    try:
        decision = execute_plan(draft, question, context)
    except (ContractError, sqlite3.Error, ArithmeticError, ValueError, KeyError):
        return None
    if decision["answer"]["type"] == "abstain":
        return None
    decision["plan"]["status"] = "rule_op"
    decision["plan"]["op"] = "business_" + spec["metric"]
    return decision


def _base_unit_asked(question: Question, base_unit: str | None) -> bool:
    """A stock answer is a count of base units: the question must not ask for whole
    packs and loose units ("mấy hộp chẵn, mấy cái lẻ") or name another unit."""
    text = question_plain(question.question)
    if re.search(r"\bchan\b|\ble\b|\bnguyen\b", text):
        return False
    asked = re.findall(r"\b(?:may|bao nhieu|bao nhiu|bn|bnhiu) (\w+)", text)
    base = plain(base_unit) if base_unit else None
    return all(word in NOT_UNITS or word == base for word in asked) and (base or not asked)


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
    from src.agent.ledger import ledger_lines

    ledger = ledger_lines(context)
    if ledger is not None:
        for row in ledger:
            if row[13] in statuses and window["start"] <= row[1] <= window["end"]:
                yield row[0], row[1], row[2], row[3], row[5], row[12], row[7], row[8]
        return
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


PACKAGING = r"\bx ?\d|\b\d+ ?(?:vien|goi|ong|chai|lon|tui|mieng)\b"


def _base_quantity(base, sold, name: str, base_unit: str | None) -> Decimal:
    """Base quantity; a sold quantity stands in only for single-unit items (§3, §10)."""
    if base is not None:
        return Decimal(str(base))
    if sold is None or not base_unit or re.search(PACKAGING, plain(name)):
        raise ContractError("base_quantity_unknown")
    return Decimal(str(sold))


def _money(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# Rulebook §7: a question about something the shop never records, a subjective judgement
# or knowledge outside the data is unanswerable. Each entry: question cue, and the column
# or rulebook wording that would make it answerable after all (then this never fires).
UNRECORDED = {
    "customer_gender": (
        r"\b(?:khach|khach hang|nguoi mua) (?:nu|nam(?! \d| nay| truoc| ngoai| roi| qua| sau))\b",
        r"gioi tinh|gender|\bphai\b|\bnam nu\b",
    ),
    "customer_address": (
        r"\b(?:o|nha o|tai) duong [a-z]|\bdia chi\b",
        r"dia chi|address|duong|phuong|khu vuc",
    ),
    "staff_judgement": (
        r"\b(?:lanh nghe|tay nghe|tan tam|le phep|chu dao|quy men|duoc khen|hay khen|"
        r"khen|nhiet tinh|than thien|kho tinh nhat|de thuong)\b",
        r"danh gia|khen|rating|review|feedback|tay nghe",
    ),
    "subjective_quality": (
        r"\b(?:ngon nhat|chat luong (?:tot|cao) nhat|uy tin nhat|dang tin nhat)\b",
        r"danh gia|rating|review|chat luong|ngon",
    ),
    "outside_knowledge": (
        r"\b(?:nen uong|ba bau|lieu dung|tac dung phu|uong (?:moi|mot) lan|"
        r"(?:dung|uong) duoc khong|chong chi dinh)\b",
        r"lieu dung|huong dan|chi dinh|cach dung",
    ),
}


def _unrecorded_attribute(context: BusinessContext, text: str) -> str | None:
    catalog = getattr(context, "catalog", None) or {}
    columns = " ".join(
        plain(c["display_name"]) for t in catalog.get("tables", []) for c in t.get("columns", [])
    )
    sources = columns + " " + plain(catalog.get("readme", ""))
    rules = context.rules
    rulebook = plain(" ".join(str(c) for c in rules.clauses)) if rules is not None else ""
    for reason, (cue, evidence) in UNRECORDED.items():
        found = re.search(cue, text)
        if found and not re.search(evidence, sources) and found[0] not in rulebook:
            return reason
    return None


def _decision(name, check, value, answer_type, **details):
    if isinstance(check, tuple):
        return name, check[1], "unanswerable", details
    window = check["effective_window"]
    return name, value, answer_type, {"window": [window["start"], window["end"]], **details}


def _products(context: BusinessContext) -> list[tuple[str, str | None]]:
    from src.agent.ledger import ledger_lines

    ledger = ledger_lines(context)
    if ledger is not None:
        return sorted({(plain(row[5]), row[12]) for row in ledger if row[5]})
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
    if days == orders or not re.search(
        r"\b(?:doanh thu|dthu|gia tri|tong tien|tien hang|ban (?:duoc|dc) tu)\b", text
    ):
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
    if context.prepared.summary.get("shape") in LINELESS:
        if not days:
            return None
        per_day = _daily_revenue(question, context, check)
        if per_day is None:
            return None
        count = sum(_compare(v, *bound) for v in per_day.values())
        return _decision(name, check, count, "number", bound=[bound[0], str(bound[1])])
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
    if not (by_day or by_month) or not re.search(
        r"\b(?:cao nhat|nhieu nhat|lon nhat|nhieu tien nhat|dat nhat)\b", text
    ):
        return None
    count_orders = bool(re.search(r"\b(?:nhieu don|dong khach|dat khach|don nhat)\b", text))
    if not count_orders and not re.search(r"\b(?:doanh thu|dthu|tien)\b", text):
        return None
    rest = re.sub(r"\b(?:nhieu|cao|lon|nhieu tien|dat) nhat\b", " ", text)
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
    if context.prepared.summary.get("shape") in LINELESS:
        # A ledger has no orders (rulebook "Bảng không ghi theo đơn") and an order-header
        # export no lines: each day's revenue comes from the verified executor.
        if count_orders:
            return None
        per_day = _daily_revenue(question, context, check) or {}
        best = max(per_day.values(), default=None)
        if not best:
            return None
        return _decision("best_day", check, min(d for d, v in per_day.items() if v == best), "date")
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


def _month_list(question, context, text):
    """Whole-shop revenue or order count of each named month, in the order asked.

    "Doanh thu tháng 2/2026, tháng 3/2026 và tháng 4/2026, ghi lần lượt", "từng tháng từ
    02/2025 tới 04/2025". A list cue is required; any qualifier leaves it to the planner.
    """
    from src.agent.plan import _metric_cues, _time_text

    if not re.search(r"\b(?:lan luot|tung thang|moi thang|theo (?:dung )?thu tu)\b", text):
        return None
    if re.search(QUALIFIERS, text) or re.search(
        r"\b(?:nhat|so voi|cao hon|thap hon|mat hang|san pham|mon|don hang nao)\b", text
    ):
        return None
    metrics = _metric_cues(question)
    if len(metrics) != 1 or not metrics <= {"revenue", "order_count"}:
        return None
    metric = next(iter(metrics))
    time_text = _time_text(question)
    named = re.findall(r"\bthang (\d{1,2})(?:/| nam )(\d{4})\b", time_text)
    months = [(int(y), int(m)) for m, y in named]
    span = re.search(
        r"\btu thang (\d{1,2})/(\d{4}) (?:toi|den)(?: het)? thang (\d{1,2})/(\d{4})\b",
        time_text,
    )
    series = re.search(
        r"\b([2-6]) thang lien(?: nhau| tiep)? ket thuc o thang (\d{1,2})/(\d{4})\b", time_text
    )
    if series and len(months) == 1 and "tu thang cu den thang moi" in text:
        last = int(series[3]) * 12 + int(series[2]) - 1
        months = [(i // 12, i % 12 + 1) for i in range(last - int(series[1]) + 1, last + 1)]
    elif span:
        first = int(span[2]) * 12 + int(span[1]) - 1
        last = int(span[4]) * 12 + int(span[3]) - 1
        if len(months) != 2 or not 1 <= last - first <= 11:
            return None
        months = [(i // 12, i % 12 + 1) for i in range(first, last + 1)]
    elif not 2 <= len(months) <= 12 or len(set(months)) != len(months):
        return None
    expressions = _literal_windows(question)
    if len(expressions) != len(named) or any(e["kind"] != "month" for e in expressions):
        return None
    values = []
    for year, month in months:
        check = preflight(
            context,
            metric,
            {"kind": "month", "year": year, "month": month},
            as_of_date=question.as_of_date,
        )
        if check["decision"] != "eligible_to_execute":
            return None
        values.append(int(_executor_value(context, check)))
    return _decision("month_list", check, values, "list", metric=metric)


def _explicit_base(question, expressions):
    """Two named months, one introduced by "so (với)": (current, base), else None.

    "Doanh thu tháng 3/2026 so với tháng 3/2025", "so với T4/2025 thì T4/2026 doanh thu…".
    """
    from src.agent.plan import _time_text

    if len(expressions) != 2 or any(e["kind"] != "month" or "year" not in e for e in expressions):
        return None
    text = _time_text(question)
    if re.search(r"\b(?:thang|th)(?: lien)? (?:truoc|trc|roi)\b|\bcung ky\b|\bnam ngoai\b", text):
        return None
    months = list(re.finditer(r"\bthang (\d{1,2})(?:/| nam )(\d{4})\b", text))
    if len(months) != 2:
        return None
    based = [bool(re.search(r"\bso(?: voi| vs)?\s*$", text[: m.start()])) for m in months]
    if sum(based) != 1:
        return None
    month = [{"kind": "month", "year": int(m[2]), "month": int(m[1])} for m in months]
    base = month[based.index(True)]
    current = month[based.index(False)]
    if current == base:
        return None
    return current, base


LINELESS = {"sales_ledger", "order_headers"}


def _daily_revenue(question, context, check) -> dict[str, Decimal] | None:
    """Each day's §1 revenue in the check's window from the verified executor; used where
    the facts have no order lines (a ledger or an order-header export)."""
    window = check["effective_window"]
    first, last = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    result = {}
    for n in range((last - first).days + 1):
        day = (first + timedelta(n)).isoformat()
        daily = preflight(
            context, "revenue", {"kind": "day", "date": day}, as_of_date=question.as_of_date
        )
        if daily["decision"] != "eligible_to_execute":
            return None
        result[day] = Decimal(str(_executor_value(context, daily, rounded=False)))
    return result


def _period_value(context, check, count_orders):
    if context.prepared.summary.get("shape") in LINELESS:
        if count_orders:
            raise ContractError("order_count_without_lines")
        return Decimal(str(_executor_value(context, check, rounded=False)))
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
    # "… bằng/chiếm bao nhiêu % tổng doanh thu": one method's share of the same period.
    share_cue = r"%|\b(?:phan tram|chiem|ti trong|ty trong)\b"
    share = len(found) == 1 and bool(re.search(share_cue, rest))
    if share:
        rest = re.sub(share_cue + r"|\btong (?:doanh thu|dthu)\b", " ", rest)
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
    answer_type = "percent" if share else "number" if len(found) == 1 else "list"
    if isinstance(check, tuple):
        return _decision("method_revenue", check, None, answer_type)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    totals = dict.fromkeys(found, Decimal(0))
    whole = Decimal(0)
    for order, _, amount, role, *_ in _rows(context, check):
        if order not in methods:
            raise ContractError("order_payment_method_missing")
        if role not in roles or amount is None:
            continue
        whole += Decimal(str(amount))
        for key in found:
            if methods[order] in METHODS[key][1]:
                totals[key] += Decimal(str(amount))
    if share:
        if not whole:
            return None
        part = totals[found[0]] * 100 / whole
        return _decision("method_share", check, _percent(part), "percent", methods=found)
    values = [_money(totals[key]) for key in found]
    value = values[0] if len(values) == 1 else values
    return _decision("method_revenue", check, value, answer_type, methods=found)


# --- Ranking, groups, customers, growth and price effect (rulebook §1, §3, §5) ---

RANK_COUNT = (
    r"\b(?<!thang )(?<!quy )(?<!tuan )(?<!ngay )"
    r"(?:top\s*)?([2-9]|10|hai|ba|bon|nam)\s+(?:mat hang|hang|mon|san pham|sp|khach(?: hang)?)\b"
)
WORD_NUMBERS = {"hai": 2, "ba": 3, "bon": 4, "nam": 5}
GROUP_QUALIFIERS = (
    r"\b(ca sang|ca chieu|nhan vien|kenh|tien mat|chuyen khoan|cod|zalo|qua dien thoai|"
    r"tai quay|ngoai tru|khong tinh|rieng|so voi|tang|giam|trung binh|binh quan)\b"
)


def _percent(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _lines(context, check, statuses=None):
    """Revenue-policy lines with product identity, order customer and groups."""
    statuses = list(statuses or check["contract"]["status_policy"])
    window = check["effective_window"]
    from src.agent.ledger import ledger_lines

    ledger = ledger_lines(context)
    if ledger is not None:
        for row in ledger:
            if row[13] in statuses and window["start"] <= row[1] <= window["end"]:
                yield row[:13]
        return
    sql = (
        "SELECT o.order_key,substr(o.ordered_at,1,10),l.net_amount,l.row_role,i.product_key,"
        "e.name,e.source_code,i.base_quantity,i.sold_quantity,p.customer_key,g.item_group,"
        "og.customer_group,e.base_unit FROM orders o JOIN order_lines l USING(order_key) "
        "LEFT JOIN line_items i USING(line_key) LEFT JOIN entities e "
        "ON e.entity_key=i.product_key LEFT JOIN order_parties p USING(order_key) "
        "LEFT JOIN line_groups g USING(line_key) LEFT JOIN order_groups og USING(order_key) "
        f"WHERE o.status IN ({','.join('?' for _ in statuses)}) "
        "AND substr(o.ordered_at,1,10) BETWEEN ? AND ?"
    )
    with closing(context.prepared.connect()) as conn:
        yield from conn.execute(sql, [*statuses, window["start"], window["end"]])


def _ranked(totals: dict, keys: dict, limit: int) -> list:
    """Descending measure; ties by code ascending, else by name (§9 'Bằng nhau')."""
    return sorted(totals, key=lambda k: (-totals[k], keys[k]))[:limit]


def _cancellation(question, context, text):
    """§1: cancelled orders, or their share of every order placed in the period."""
    if not re.search(r"\bhuy\b", text) or re.search(
        r"\b(nhom|khach|ca sang|ca chieu|nhan vien|kenh|tien mat|chuyen khoan|zalo|"
        r"qua dien thoai|tai quay|mat hang|san pham|vi|ly do|tra hang|so voi|tang|giam)\b",
        text,
    ):
        return None
    rate = bool(re.search(r"\b(?:ti le|ty le|phan tram)\b|%", text))
    if not rate and not re.search(
        r"\b" + ASK + r" don(?: hang)?\b|\bhuy (?:" + ASK + r") don\b", text
    ):
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("cancellation", check, None, "percent" if rate else "number")
    window = check["effective_window"]
    with closing(context.prepared.connect()) as conn:
        rows = conn.execute(
            "SELECT status FROM orders WHERE substr(ordered_at,1,10) BETWEEN ? AND ?",
            (window["start"], window["end"]),
        ).fetchall()
    placed = len(rows)
    cancelled = sum(1 for (status,) in rows if status == "cancelled")
    if not rate:
        return _decision("cancellation", check, cancelled, "number")
    if not placed:
        return None
    return _decision("cancellation", check, _percent(Decimal(cancelled) * 100 / placed), "percent")


def _rank_limit(text):
    count = re.search(RANK_COUNT, text)
    if not count:
        return None
    return WORD_NUMBERS.get(count[1]) or int(count[1])


def _product_ranking(question, context, text):
    """§3: best sellers by base quantity, or products by revenue; ties by code."""
    by_quantity = re.search(
        r"\b(ban chay|chay nhat|ban nhieu nhat|ban duoc nhieu nhat|hot nhat)\b", text
    )
    by_revenue = re.search(
        r"\b(?:doanh thu|dthu|tien)\b.*\b(?:cao nhat|nhieu nhat|lon nhat)\b", text
    ) and re.search(r"\b(?:mat hang|hang|mon|san pham|sp) nao\b", text)
    if not (by_quantity or by_revenue) or re.search(r"\b(nhom|khach)\b|" + GROUP_QUALIFIERS, text):
        return None
    if re.search(r"\b(?:ngay|thang|tuan) nao\b|\blai\b|\bloi\b", text):
        return None
    limit = _rank_limit(text)
    if limit is None and re.search(r"\btop\b|\bnhung\b|\bliet ke\b", text):
        return None
    check = _window(question, context)
    if check is None:
        return None
    answer_type = "list" if limit else "string"
    if isinstance(check, tuple):
        return _decision("product_ranking", check, None, answer_type)
    totals, keys, names = defaultdict(Decimal), {}, {}
    for row in _lines(context, check):
        _, _, amount, role, product, name, code, base, sold = row[:9]
        if role != "sale_item":
            continue
        if product is None or name is None:
            raise ContractError("sale_line_product_unknown")
        keys[product], names[product] = (code or name), name
        totals[product] += (
            _base_quantity(base, sold, name, row[12]) if by_quantity else Decimal(str(amount or 0))
        )
    ranked = _ranked(totals, keys, limit or 1)
    if not ranked:
        return None
    value = [names[k] for k in ranked] if limit else names[ranked[0]]
    return _decision("product_ranking", check, value, answer_type)


def _group_label(context, text):
    """Longest item or customer group label the question names after 'nhom'."""
    with closing(context.prepared.connect()) as conn:
        labels = [
            (plain(v), kind)
            for kind, table, column in (
                ("item", "line_groups", "item_group"),
                ("customer", "order_groups", "customer_group"),
            )
            for (v,) in conn.execute(f"SELECT DISTINCT {column} FROM {table}")
            if v and plain(v)
        ]
    found = [
        (label, kind)
        for label, kind in labels
        if re.search(r"\bnhom (?:hang |khach (?:hang )?)?" + re.escape(label) + r"\b", text)
    ]
    if not found:
        return None
    label, kind = max(found, key=lambda x: len(x[0]))
    customer_cue = r"\bkhach(?: hang)?(?: thuoc)? nhom\b|\bnhom khach\b"
    if kind == "customer" and not re.search(customer_cue, text):
        return None
    if kind == "item" and re.search(customer_cue, text):
        return None
    return label, kind


def _group_revenue(question, context, text):
    """§1: revenue of an item or customer group, or its share of the whole shop."""
    if not re.search(r"\bnhom\b", text) or re.search(GROUP_QUALIFIERS, text):
        return None
    if re.search(r"\b(nhat|top|nao|so don|bao nhieu don|may don|khach nao)\b", text):
        return None
    share = bool(re.search(r"%|\b(?:phan tram|ti trong|ty trong|chiem|dong gop|phan cua)\b", text))
    if not share and not re.search(r"\b(?:doanh thu|dthu|tien|revenue|thu duoc|ban duoc)\b", text):
        return None
    group = _group_label(context, text)
    if group is None:
        return None
    label, kind = group
    check = _window(question, context)
    if check is None:
        return None
    answer_type = "percent" if share else "number"
    if isinstance(check, tuple):
        return _decision("group_revenue", check, None, answer_type)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    part = whole = Decimal(0)
    for row in _lines(context, check):
        amount, role, item_group, customer_group = row[2], row[3], row[10], row[11]
        if role not in roles or amount is None:
            continue
        value = Decimal(str(amount))
        whole += value
        own = item_group if kind == "item" else customer_group
        if own is not None and plain(own) == label:
            part += value
    if share:
        if not whole:
            return None
        return _decision(
            "group_revenue", check, _percent(part * 100 / whole), "percent", group=label
        )
    return _decision("group_revenue", check, _money(part), "number", group=label)


def _customers(context):
    with closing(context.prepared.connect()) as conn:
        return {
            key: (code, name)
            for key, code, name in conn.execute(
                "SELECT entity_key,source_code,name FROM entities WHERE kind='customer'"
            )
        }


# Rulebook rankings (public §3 rows); each fires only when the rulebook defines it.
RANKINGS = {
    "customer_loyalty": r"\bkhach(?: hang)? (?:trung thanh|ruot|than thiet) nhat\b",
    "customer_aov_ranking": r"\bkhach(?: hang)? sop nhat\b",
    "customer_cancellations": r"\bkhach(?: hang)? (?:hay|de) doi y nhat\b",
    "product_order_ranking": (
        r"\b(?:mat hang|hang|mon|san pham)(?: nao)? (?:duoc )?ua chuong nhat\b"
    ),
    "peak_hour": r"\bgio cao diem\b",
}


def _rulebook_rankings(question, context, text):
    """Customer loyalty/AOV/cancellations, most-wanted product and peak hour (rulebook §3)."""
    found = [r for r, cue in RANKINGS.items() if re.search(cue, text)]
    rules = context.rules.rules if context.rules is not None else {}
    if len(found) != 1 or found[0] not in rules or rules[found[0]].status != "resolved":
        return None
    rule = found[0]
    if re.search(r"(nhom|top|nhung|liet ke|cac)|" + GROUP_QUALIFIERS, text) or _rank_limit(text):
        return None
    check = _window(question, context)
    if check is None:
        return None
    answer_type = "number" if rule == "peak_hour" else "string"
    if isinstance(check, tuple):
        return _decision(rule, check, None, answer_type)
    window = check["effective_window"]
    statuses = list(check["contract"]["status_policy"])
    if rule == "peak_hour":
        with closing(context.prepared.connect()) as conn:
            stamps = [
                t
                for (t,) in conn.execute(
                    f"SELECT ordered_at FROM orders WHERE status IN "
                    f"({','.join('?' for _ in statuses)}) AND substr(ordered_at,1,10) BETWEEN ? "
                    "AND ?",
                    [*statuses, window["start"], window["end"]],
                )
            ]
        hours = [re.match(r"\d{4}-\d{2}-\d{2}[T ](\d{2}):", t) for t in stamps]
        if not hours or not all(hours):
            return None  # an order without a clock cannot be placed in an hour
        counts = Counter(int(h[1]) for h in hours)
        best = [h for h, n in counts.items() if n == max(counts.values())]
        # The rule names no tie-break between hours: a tie abstains.
        return _decision(rule, check, best[0], "number") if len(best) == 1 else None
    if rule == "product_order_ranking":
        orders, keys, names = defaultdict(set), {}, {}
        for row in _lines(context, check):
            order, role, product, name, code = row[0], row[3], row[4], row[5], row[6]
            if role != "sale_item":
                continue
            if product is None or name is None:
                raise ContractError("sale_line_product_unknown")
            keys[product], names[product] = (code or name), name
            orders[product].add(order)
        totals = {k: Decimal(len(v)) for k, v in orders.items()}
        ranked = _ranked(totals, keys, 1)
        return _decision(rule, check, names[ranked[0]], "string") if ranked else None
    customers = _customers(context)
    if not customers:
        return None
    lines = _lines(context, check, ["cancelled"] if rule == "customer_cancellations" else None)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    revenue, orders, days = defaultdict(Decimal), defaultdict(set), defaultdict(set)
    for order, day, amount, role, *rest in lines:
        customer = rest[5]
        if customer is None or customer not in customers:
            continue
        orders[customer].add(order)
        days[customer].add(day)
        if role in roles and amount is not None:
            revenue[customer] += Decimal(str(amount))
    if rule == "customer_loyalty":
        totals = {k: Decimal(len(v)) for k, v in days.items()}
    elif rule == "customer_cancellations":
        totals = {k: Decimal(len(v)) for k, v in orders.items()}
    else:
        minimum = int(rules[rule].parameters.get("minimum_orders", 3))
        totals = {k: revenue[k] / len(v) for k, v in orders.items() if len(v) >= minimum}
    keys = {k: customers[k][0] or customers[k][1] for k in totals}
    ranked = _ranked(totals, keys, 1)
    return _decision(rule, check, customers[ranked[0]][1], "string") if ranked else None


def _purchase_total(question, context, text):
    """§5 'Tiền nhập hàng trong kỳ': purchase receipts dated in the period, not cancelled."""
    if not re.search(r"\bnhap\b", text) or not re.search(
        r"\b(?:tong (?:tien|gia tri)|tien (?:hang )?nhap|het " + ASK + r" tien|gia tri)\b", text
    ):
        return None
    if re.search(r"\b(binh quan|trung binh|nha cung cap|ncc|tru|ban|mat hang|nhom|lo|han)\b", text):
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("purchase_total", check, None, "number")
    window = check["effective_window"]
    with closing(context.prepared.connect()) as conn:
        rows = conn.execute(
            "SELECT status, amount FROM purchases WHERE substr(purchased_at,1,10) BETWEEN ? AND ?",
            [window["start"], window["end"]],
        ).fetchall()
        any_purchase = conn.execute("SELECT 1 FROM purchases LIMIT 1").fetchone()
    if not any_purchase or not rows:
        return None  # no purchase records is not "0 đồng"
    if any(plain(st) not in PURCHASE_RECEIVED | PURCHASE_CANCELLED for st, _ in rows):
        return None  # an unknown receipt status is never guessed
    total = sum((Decimal(a) for st, a in rows if plain(st) in PURCHASE_RECEIVED), Decimal(0))
    return _decision("purchase_total", check, _money(total), "number")


def _gross_profit(question, context, text):
    """§5 'Lãi gộp': revenue (§1) minus quantity × cost written on each sale line."""
    if not re.search(r"\b(?:lai gop|loi nhuan gop)\b", text) or re.search(
        r"\b(nhom|khach|hang nao|mat hang|mon|san pham|nhan vien|kenh|phan tram|ti le|ty le|"
        r"nao|nhat|top)\b|%",
        text,
    ):
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("gross_profit", check, None, "number")
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    window = check["effective_window"]
    statuses = list(check["contract"]["status_policy"])
    sql = (
        "SELECT l.net_amount,l.row_role,i.sold_quantity,i.unit_cost FROM orders o "
        "JOIN order_lines l USING(order_key) LEFT JOIN line_items i USING(line_key) "
        f"WHERE o.status IN ({','.join('?' for _ in statuses)}) "
        "AND substr(o.ordered_at,1,10) BETWEEN ? AND ?"
    )
    profit, lines = Decimal(0), 0
    with closing(context.prepared.connect()) as conn:
        for amount, role, sold, cost in conn.execute(
            sql, [*statuses, window["start"], window["end"]]
        ):
            if role not in roles or amount is None:
                continue
            if role == "sale_item" and (sold is None or cost is None):
                raise ContractError("line_cost_missing")
            profit += Decimal(str(amount))
            if role == "sale_item":
                profit -= Decimal(str(sold)) * Decimal(str(cost))
            lines += 1
    if not lines:
        return None
    return _decision("gross_profit", check, _money(profit), "number")


def _customer_ops(question, context, text):
    """§3 'Xác định khách': customer rankings and counts; walk-in orders never count."""
    if not re.search(r"\bkhach\b", text) or re.search(
        r"\b(nhom|no|nowj|cong no|duong|o duong|mat hang|san pham|vang lai|moi)\b|"
        + GROUP_QUALIFIERS,
        text,
    ):
        return None
    money = re.search(
        r"\b(?:nhieu tien|nhiu tien|chi tieu|tong tien mua|chi nhieu|mua nhieu tien)\b", text
    )
    orders_rank = re.search(
        r"\b(?:nhieu|nhiu) (?:don|don hang|lan)(?: mua)? nhat\b|\bmua (?:nhieu|nhiu) (?:don|lan)\b"
        r"|\bso don(?: mua)? (?:nhieu|nhiu) nhat\b",
        text,
    )
    count = re.search(
        r"\b(?:it nhat|tu|>=|≥)\s*(\d+) (?:don|lan)(?: tro len)?\b|(?:≥|>=)\s*(\d+) don\b", text
    )
    ranking = (money or orders_rank) and re.search(r"\b(nhat|top|nao|ai)\b", text)
    if not (ranking or count):
        return None
    customers = _customers(context)
    if not customers:
        return None
    check = _window(question, context)
    if check is None:
        return None
    limit = _rank_limit(text)
    if ranking:
        answer_type = "list" if limit else "string"
    else:
        answer_type = "percent" if re.search(r"%|\bphan tram\b", text) else "number"
    if isinstance(check, tuple):
        return _decision("customer", check, None, answer_type)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    revenue, orders = defaultdict(Decimal), defaultdict(set)
    for order, _, amount, role, *_rest in _lines(context, check):
        customer = _rest[5]
        if customer is None or customer not in customers:
            continue
        orders[customer].add(order)
        if role in roles and amount is not None:
            revenue[customer] += Decimal(str(amount))
    if not orders:
        return None
    if ranking:
        totals = revenue if money else {k: Decimal(len(v)) for k, v in orders.items()}
        keys = {k: customers[k][0] or customers[k][1] for k in totals}
        ranked = _ranked(totals, keys, limit or 1)
        names = [customers[k][1] for k in ranked]
        return _decision("customer", check, names if limit else names[0], answer_type)
    least = int(count[1] or count[2])
    qualified = sum(1 for v in orders.values() if len(v) >= least)
    if answer_type == "percent":
        return _decision(
            "customer", check, _percent(Decimal(qualified) * 100 / len(orders)), "percent"
        )
    return _decision("customer", check, qualified, "number")


def _growth(question, context, text):
    """§1, §2: revenue change in % against the previous period, or the same month a year ago."""
    if not re.search(r"%|\bphan tram\b", text) or not re.search(
        r"\b(?:doanh thu|dthu|tien ban)\b", text
    ):
        return None
    if not re.search(
        r"\b(?:tang|giam|hon hay kem|thay doi|cao hay thap|len hay xuong|len xuong)\b", text
    ):
        return None
    if re.search(
        r"\b(nhom|khach|ca sang|ca chieu|nhan vien|kenh|tien mat|chuyen khoan|zalo|"
        r"qua dien thoai|tai quay|mat hang|huy|trung binh|binh quan|truoc tet|tet)\b",
        text,
    ):
        return None
    expressions = _literal_windows(question)
    explicit = _explicit_base(question, expressions)
    if explicit is not None:
        checks = [_window(question, context, e) for e in explicit]
        if any(not isinstance(c, dict) for c in checks):
            return None
        values = [_period_value(context, c, False) for c in checks]
        if values[1] <= 0:
            return None
        return _decision(
            "growth", checks[0], _percent((values[0] - values[1]) * 100 / values[1]), "percent"
        )
    # "tháng 4 năm ngoái" names the month; only "cùng kỳ năm trước/ngoái" is the comparison.
    named = re.sub(r"\b(?:thang|quy) \d{1,2} nam (?:ngoai|roi|truoc)\b", " ", text)
    last_year = re.search(r"\b(?:cung ky|cung thang) nam (?:truoc|ngoai)\b|\bnam ngoai\b", named)
    previous = re.search(r"\b(?:thang|th)(?: lien)? (?:truoc|trc|roi)\b|\bcung ky thang\b", text)
    # "tháng này ... cùng kỳ tháng trước": the previous month is the comparison, not a window.
    kinds = sorted(e["kind"] for e in expressions)
    if previous and kinds == ["current_month", "previous_month"]:
        expressions = [{"kind": "current_month"}]
    # "tháng 8/2025 ... tháng trước": the month before the named month is the comparison.
    elif previous and kinds == ["month", "previous_month"]:
        expressions = [e for e in expressions if e["kind"] == "month"]
    if len(expressions) != 1 or bool(last_year) == bool(previous):
        return None
    current = expressions[0]
    if current["kind"] == "month" and "year" not in current:
        # Rulebook "Tháng M không ghi năm": this year unless after today, else last year.
        today = date.fromisoformat(question.as_of_date)
        year = today.year - (date(today.year, current["month"], 1) > today)
        current = {**current, "year": year}
    if current["kind"] == "month":
        if last_year:
            base = {"kind": "month", "year": current["year"] - 1, "month": current["month"]}
        else:
            month = current["year"] * 12 + current["month"] - 2
            base = {"kind": "month", "year": month // 12, "month": month % 12 + 1}
    elif current["kind"] == "current_month" and previous:
        base = {"kind": "previous_mtd"}
    else:
        return None
    checks = [_window(question, context, e) for e in (current, base)]
    if any(not isinstance(c, dict) for c in checks):
        return None
    values = [_period_value(context, c, False) for c in checks]
    if values[1] <= 0:
        return None
    return _decision(
        "growth", checks[0], _percent((values[0] - values[1]) * 100 / values[1]), "percent"
    )


def _price_increase_day(context, product: str, target: Decimal) -> date | None:
    """First day the price list raises ``product`` to ``target`` (rulebook "Hiệu ứng tăng
    giá": a raise is a new price above the one before; the first raise to a price counts)."""
    from src.agent.purchasing import _header, _number

    roles = {
        "date": r"ngay ap dung",
        "price": r"gia moi|gia ban le",
        "old": r"gia cu",
        "code": r"ma (?:san pham|hang hoa|hang|sp)",
        "name": r"ten ?hang|san pham|ten san pham",
    }
    tables = []
    for profile in context.catalog["tables"]:
        found: dict[str, list[int]] = {}
        for column in profile.get("columns", []):
            for role, pattern in roles.items():
                if re.fullmatch(pattern, _header(column["display_name"])):
                    found.setdefault(role, []).append(column["position"])
        if {"date", "price"} <= found.keys() and ({"code", "name"} & found.keys()):
            if all(len(v) == 1 for v in found.values()):
                tables.append((profile, {k: v[0] for k, v in found.items()}))
    if len(tables) != 1:
        return None
    profile, at = tables[0]
    with closing(context.prepared.connect()) as conn:
        codes = {
            str(c).strip()
            for c, n in conn.execute("SELECT source_code,name FROM entities WHERE kind='product'")
            if c and plain(n) == product
        }
    from src.agent.purchasing import _day

    header = set(profile.get("header_rows", []))
    history = []
    source = context.source_table(profile["source_id"])
    for row in source.iter_rows():
        if row.row in header or (header and row.row is not None and row.row < min(header)):
            continue

        def cell(role, row=row):
            c = row.cells.get(at[role]) if role in at else None
            return None if c is None or c.raw_value in {None, ""} else c.raw_value

        if "code" in at:
            if str(cell("code") or "").strip() not in codes:
                continue
        elif plain(str(cell("name") or "")) != product:
            continue
        if cell("date") is None or cell("price") is None:
            raise ContractError("price_history_row_incomplete")
        old = cell("old")
        history.append(
            (
                date.fromisoformat(
                    _day(
                        cell("date"), row.cells.get(at["date"]), source.metadata.get("date_system")
                    )
                ),
                _number(cell("price")),
                _number(old) if old is not None else None,
            )
        )
    previous = None
    for day, price, old in sorted(history, key=lambda h: h[0]):
        before = previous if previous is not None else old
        if price == target and before is not None and price > before:
            return day
        previous = price
    return None


def _price_effect(question, context, text):
    """§5 'Hiệu ứng tăng giá': base quantity N days from D (inclusive) vs N days before D."""
    days = re.search(r"\b(\d+) ngay (?:ke tu|tinh tu|tu|dau)\b", text)
    day = re.search(r"\b(?:ngay|tu ngay|hom|vao ngay) (\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if not days or not re.search(r"%|\bphan tram\b", text):
        return None
    if not re.search(r"\b(?:doi gia|tang gia|len gia|giam gia|gia moi|ngay tang gia)\b", text):
        return None
    products = [name for name, _ in _products(context) if len(name.split()) >= 2 and name in text]
    if not products:
        return None
    product = max(products, key=len)
    n = int(days[1])
    if day:
        start = date(int(day[3]), int(day[2]), int(day[1]))
    else:
        # No date: the price list's first raise of this item to the price asked.
        rules = context.rules.rules if context.rules is not None else {}
        raised = re.search(r"\blen(?: gia)? " + AMOUNT, text.replace(product, " "))
        if "price_effect" not in rules or not raised or re.search(r"\b\d{1,2}/\d{1,2}\b", text):
            return None
        start = _price_increase_day(context, product, _amount(raised))
        if start is None:
            return None
    after = {
        "kind": "range",
        "start": start.isoformat(),
        "end": (start + timedelta(n - 1)).isoformat(),
    }
    before = {
        "kind": "range",
        "start": (start - timedelta(n)).isoformat(),
        "end": (start - timedelta(1)).isoformat(),
    }
    totals = []
    for expression in (after, before):
        check = _window(question, context, expression)
        if not isinstance(check, dict):
            return None
        window = check["effective_window"]
        if (window["start"], window["end"]) != (expression["start"], expression["end"]):
            return None
        total = Decimal(0)
        for row in _lines(context, check):
            role, name, base, sold = row[3], row[5], row[7], row[8]
            if role == "sale_item" and name and plain(name) == product:
                total += _base_quantity(base, sold, name, row[12])
        totals.append((total, check))
    if totals[1][0] <= 0:
        return None
    change = (totals[0][0] - totals[1][0]) * 100 / totals[1][0]
    return _decision("price_effect", totals[0][1], _percent(change), "percent", product=product)


# --- Whole-shop metrics through the verified executor; named-product revenue; trends ---

AOV = (
    r"\b(?:trung binh|binh quan|tb)\b.*\b(?:moi|mot|1) (?:don|bill|hoa don)\b|"
    r"\b(?:moi|mot|1) (?:don|bill|hoa don)(?: hang)? (?:trung binh|binh quan|tb)\b"
)


def _executor_value(context, check, *, rounded=True):
    from src.agent.execution import _compute

    result = _compute(context, check, round_result=rounded)
    return result["rounded_value"] if rounded else result["exact_value"]


def _whole_shop(question, context, text):
    """S1 revenue, order count and average order value with no other condition."""
    from src.agent.entities import mentioned_entity
    from src.agent.plan import _check_question_scope, _metric_cues

    aov = bool(re.search(AOV, text))
    scoped = question
    if aov:
        stripped = re.sub(
            r"\b(?:trung binh|binh quan|tb)\b", " ", question_plain(question.question)
        )
        scoped = Question(question.id, question.business_id, stripped, question.as_of_date)
    metrics = _metric_cues(scoped)
    if aov:
        metrics = {"average_order_value"} if metrics <= {"revenue", "order_count"} else set()
    if len(metrics) != 1 or metrics & {"cancelled_order_count"}:
        return None
    metric = next(iter(metrics))
    _check_question_scope(scoped)
    from src.agent.planner import unsupported_intent

    if unsupported_intent(question):  # e.g. "thuốc kê đơn" narrows the shop total
        return None
    if mentioned_entity(context, question) or re.search(
        r"\b(?:tien ban|ban) (?!(?:hang )?(?:duoc|dc|ra|bao|bn|bnhiu|tong|total|nhieu|cua|"
        r"thang|tuan|hom|ngay|quy|nam|bua|trong|tu|th|t\d|\d))[a-z]",
        text,
    ):
        return None
    if metric == "average_order_value":
        checks = [_window(question, context)]
        if not isinstance(checks[0], dict):
            return None
        order_check = preflight(
            context, "order_count", _literal_windows(question)[0], as_of_date=question.as_of_date
        )
        if order_check["decision"] != "eligible_to_execute":
            return None
        revenue = _executor_value(context, checks[0], rounded=False)
        orders = _executor_value(context, order_check, rounded=False)
        if not orders:
            return None
        return _decision("whole_shop", checks[0], _money(revenue / orders), "number", metric=metric)
    expressions = _literal_windows(question)
    if len(expressions) != 1:
        return None
    check = preflight(context, metric, expressions[0], as_of_date=question.as_of_date)
    if check["decision"] == "unanswerable":
        return _decision("whole_shop", ("unanswerable", check["reason"]), None, "number")
    if check["decision"] != "eligible_to_execute":
        return None
    value = _executor_value(context, check)
    return _decision("whole_shop", check, int(value), "number", metric=metric)


def _product_revenue(question, context, text):
    """§1 revenue of one named product: its sale lines on revenue-policy orders."""
    if not re.search(r"\b(?:tien|doanh thu|dthu|thu ve|thu duoc)\b", text) or re.search(
        r"\b(?:gia ban|don gia|gia mot|nhat|top|nhom|khach|%|phan tram|so voi|tang|giam)\b|"
        + GROUP_QUALIFIERS,
        text,
    ):
        return None
    products = [name for name, _ in _products(context) if len(name.split()) >= 2 and name in text]
    if not products:
        return None
    product = max(products, key=len)
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("product_revenue", check, None, "number")
    total, seen = Decimal(0), False
    for row in _lines(context, check):
        role, name, amount = row[3], row[5], row[2]
        if role == "sale_item" and name is None:
            raise ContractError("sale_line_product_unknown")
        if role == "sale_item" and plain(name) == product and amount is not None:
            total += Decimal(str(amount))
            seen = True
    return _decision("product_revenue", check, _money(total), "number", product=product, seen=seen)


def _trend(question, context, text):
    """Revenue rising in each of three consecutive months (boolean, question order)."""
    if not re.search(r"\btang lien tuc\b|\bthang sau (?:co )?luon cao hon\b", text):
        return None
    if re.search(r"\b(nhom|khach|mat hang|ca sang|ca chieu|kenh)\b", text):
        return None
    months = [e for e in _literal_windows(question) if e["kind"] == "month"]
    if len(months) == 2:
        first = months[0]["year"] * 12 + months[0]["month"] - 1
        last = months[1]["year"] * 12 + months[1]["month"] - 1
        if last - first != 2:
            return None
        months = [
            {"kind": "month", "year": i // 12, "month": i % 12 + 1} for i in range(first, last + 1)
        ]
    if len(months) != 3:
        return None
    checks = [_window(question, context, e) for e in months]
    if any(not isinstance(c, dict) for c in checks):
        return None
    values = [_executor_value(context, c, rounded=False) for c in checks]
    rising = all(b > a for a, b in zip(values, values[1:], strict=False))
    return _decision("trend", checks[0], rising, "boolean")


CHANNELS = {
    "phone": (r"\bqua dien thoai\b", {"phone"}),
    "store": (r"\btai quay\b", {"store", "counter"}),
    "zalo_oa": (r"\bzalo(?: oa)?\b", {"zalo_oa", "zalo"}),
}


def _channel_revenue(question, context, text):
    """§1 revenue by sales channel: one value, two in question order, or a share."""
    positions = sorted(
        (m.start(), key)
        for key, (pattern, _) in CHANNELS.items()
        for m in re.finditer(pattern, text)
    )
    found = list(dict.fromkeys(key for _, key in positions))
    if not 1 <= len(found) <= 2 or not re.search(r"\b(?:doanh thu|dthu|revenue|tien)\b", text):
        return None
    share = bool(re.search(r"%|\b(?:phan tram|ti trong|ty trong|chiem)\b", text))
    rest = text
    for key in found:
        rest = re.sub(CHANNELS[key][0], " ", rest)
    if (share and len(found) != 1) or re.search(
        r"\b(nhom|khach|ca sang|ca chieu|nhan vien|tien mat|chuyen khoan|cod|huy|tra hang|"
        r"so voi|tang|giam|trung binh|binh quan|nhat|top|mat hang|san pham)\b",
        rest,
    ):
        return None
    with closing(context.prepared.connect()) as conn:
        channels = dict(conn.execute("SELECT order_key,channel FROM order_scopes"))
    if not channels:
        return None
    check = _window(question, context)
    if check is None:
        return None
    answer_type = "percent" if share else "number" if len(found) == 1 else "list"
    if isinstance(check, tuple):
        return _decision("channel_revenue", check, None, answer_type)
    roles = set(check["contract"].get("row_roles") or {"sale_item", "service_fee"})
    totals, whole = dict.fromkeys(found, Decimal(0)), Decimal(0)
    for row in _lines(context, check):
        order, amount, role = row[0], row[2], row[3]
        if role not in roles or amount is None:
            continue
        if order not in channels:
            raise ContractError("order_channel_missing")
        value = Decimal(str(amount))
        whole += value
        for key in found:
            if plain(channels[order] or "") in CHANNELS[key][1]:
                totals[key] += value
    if share:
        if not whole:
            return None
        value = _percent(totals[found[0]] * 100 / whole)
    else:
        values = [_money(totals[key]) for key in found]
        value = values[0] if len(values) == 1 else values
    return _decision("channel_revenue", check, value, answer_type, channels=found)


def _items_in_orders(question, context, text):
    """Count products present in at least N distinct valid orders (§1 order policy)."""
    found = re.search(
        r"\b(?:mat hang|san pham|mon)\b.*\b(?:o|trong) (?:it nhat |>= ?|≥ ?)?(\d+) don"
        r"(?: hang)?(?: khac nhau)?(?: tro len)?\b",
        text,
    )
    if not found or not re.search(r"\b(?:may|bao nhieu|bn|bnhiu|dem so|so)\b", text):
        return None
    if not re.search(r"\btro len\b|>=|≥|\bit nhat\b", text) or re.search(GROUP_QUALIFIERS, text):
        return None
    least = int(found[1])
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("items_in_orders", check, None, "number")
    orders = defaultdict(set)
    for row in _lines(context, check):
        if row[3] != "sale_item":
            continue
        if row[4] is None:
            raise ContractError("sale_line_product_unknown")
        orders[row[4]].add(row[0])
    count = sum(1 for v in orders.values() if len(v) >= least)
    return _decision("items_in_orders", check, count, "number", least=least)
