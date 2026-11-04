"""Bounded QueryPlan grammar and authoritative enrichment (roadmap appendix E)."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta

from src.data.catalog import BusinessContext
from src.data.normalization.stock import resolve_stock_entity
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.metric_registry import metric_contract
from src.semantics.rulebook import plain
from src.temporal.resolver import TimeWindow, resolve_window

VERSION = "query-plan-v9"


def _object(properties: dict, *, optional: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key not in optional],
        "additionalProperties": False,
    }


def _array(items: dict, *, minimum: int = 0, maximum: int = 4) -> dict:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


TEXT = {"type": "string", "maxLength": 256}
INTEGER = {"type": "integer"}
# One grammar shared by generation and validation, with at most one derived scalar.
TIME_FIELDS = {
    "day": {"date": TEXT},
    "range": {"start": TEXT, "end": TEXT},
    **{
        k: {}
        for k in (
            "today",
            "yesterday",
            "current_week",
            "previous_week",
            "current_month",
            "previous_month",
            "previous_mtd",
            "year_to_date",
        )
    },
    "month": {"year": INTEGER, "month": INTEGER},
    "quarter": {"year": INTEGER, "quarter": INTEGER},
    "year": {"year": INTEGER},
    "last_n_days": {"days": INTEGER},
    "tet": {"year": INTEGER},
    "before_tet": {"year": INTEGER},
}
EXPRESSION = {
    "anyOf": [_object({"kind": {"const": kind}, **fields}) for kind, fields in TIME_FIELDS.items()]
}
PLAN_SCHEMA = _object(
    {
        "entities": _array(TEXT),
        "time_windows": _array(
            _object({"id": TEXT, "expression": EXPRESSION}), minimum=1, maximum=2
        ),
        "aggregations": _array(
            _object(
                {
                    "id": TEXT,
                    "metric_id": {
                        "enum": [
                            "revenue",
                            "order_count",
                            "inventory_on_hand",
                            "cancelled_order_count",
                            "placed_order_count",
                            "calendar_day_count",
                            "unsupported",
                        ]
                    },
                    "window_ref": TEXT,
                    "scope": {"enum": ["whole_business", "product", "unsupported"]},
                    "filters": _array(TEXT),
                    "group_by": _array(TEXT),
                    "unit": TEXT,
                },
                optional=("unit",),
            ),
            minimum=1,
            maximum=2,
        ),
        "derived_measures": _array(
            _object(
                {
                    "id": TEXT,
                    "operator": {
                        "enum": [
                            "difference",
                            "percent_change",
                            "average_order_value",
                            "cancellation_rate",
                            "share",
                            "average_daily",
                        ]
                    },
                    "current_ref": TEXT,
                    "baseline_ref": TEXT,
                }
            ),
            maximum=1,
        ),
        "output": _object(
            {
                "expected_type": {"enum": ["number", "percent", "unsupported"]},
                "result_refs": _array(TEXT, minimum=1, maximum=1),
            }
        ),
        "uncertainties": _array(TEXT),
    }
)


def _schema(value, schema: dict, path: str = "plan") -> None:
    """Validate exactly the small JSON Schema subset used above; bool is not integer."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _schema(value, option, path)
                return
            except ContractError:
                pass
        raise ContractError(f"{path}: unsupported time expression shape")
    if "const" in schema and value != schema["const"]:
        raise ContractError(f"{path}: wrong constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path}: unknown operator/value")
    kind = schema.get("type")
    if kind == "object":
        if (
            not isinstance(value, dict)
            or not set(schema["required"]) <= set(value)
            or set(value) - set(schema["properties"])
        ):
            raise ContractError(f"{path}: missing or unexpected fields")
        for key, field in schema["properties"].items():
            if key in value:
                _schema(value[key], field, f"{path}.{key}")
    elif kind == "array":
        if (
            not isinstance(value, list)
            or not schema["minItems"] <= len(value) <= schema["maxItems"]
        ):
            raise ContractError(f"{path}: expected bounded array")
        items = schema["items"]
        if isinstance(items, list):
            if len(items) != len(value):
                raise ContractError(f"{path}: wrong dependency count")
            for item, item_schema in zip(value, items, strict=True):
                _schema(item, item_schema, path)
        else:
            for item in value:
                _schema(item, items, path)
    elif kind == "string" and (not isinstance(value, str) or len(value) > schema["maxLength"]):
        raise ContractError(f"{path}: expected bounded text")
    elif kind == "integer" and type(value) is not int:
        raise ContractError(f"{path}: expected integer")


def _relative_kinds(question: Question) -> set[str]:
    text = plain(question.question)
    cues = {
        r"\b(thang nay|thang hien tai)\b": "current_month",
        r"\b(thang (truoc|roi|vua roi))\b": "previous_month",
        r"\b(tuan nay|tuan hien tai)\b": "current_week",
        r"\b(tuan (truoc|roi|vua roi))\b": "previous_week",
        r"\b(hom qua|hqua|ngay hom qua)\b": "yesterday",
        r"\b(hom nay|bua nay|hnay)\b": "today",
    }
    return {kind for pattern, kind in cues.items() if re.search(pattern, text)}


def _metric_cues(question: Question) -> set[str]:
    """Literal intent cross-check, independent of model output; no numeric answers."""
    text = plain(question.question)
    cues = {
        r"\b(doanh thu|doanh so|dthu|tien ban|tien hang|ban duoc bao nhieu tien|"
        r"thu ve bao nhieu tien)\b": "revenue",
        r"\b((so|may|bao nhieu|bn|dem|tong) (don|bill)|don hang duoc bao nhieu)\b": "order_count",
    }
    return {metric for pattern, metric in cues.items() if re.search(pattern, text)}


def _literal_windows(question: Question) -> list[dict]:
    """Finite relative or fully specified date/period cues, never business IDs."""
    text = re.sub(r"\bthangs\b", "thang", plain(question.question))
    dates = r"(\d{1,2})/(\d{1,2})/(\d{4})"

    def iso(parts) -> str:
        day, month, year = map(int, parts)
        return f"{year:04}-{month:02}-{day:02}"

    span = re.search(r"\btu (?:ngay )?" + dates + r" (?:den|toi)(?: het)? (?:ngay )?" + dates, text)
    if span:
        return [{"kind": "range", "start": iso(span.groups()[:3]), "end": iso(span.groups()[3:])}]
    expressions = [{"kind": kind} for kind in sorted(_relative_kinds(question))]
    for label, kind, field in [("thang", "month", "month"), ("quy", "quarter", "quarter")]:
        for value, year in re.findall(r"\b" + label + r" (\d{1,2})(?:/| nam )(\d{4})\b", text):
            expressions.append({"kind": kind, "year": int(year), field: int(value)})
    for match in re.finditer(r"\bngay " + dates + r"\b", text):
        expressions.append({"kind": "day", "date": iso(match.groups())})
    for days in re.findall(r"\b(\d+) ngay gan nhat\b", text):
        expressions.append({"kind": "last_n_days", "days": int(days)})
    if re.search(r"\btu dau nam(?: toi nay)?\b", text):
        expressions.append({"kind": "year_to_date"})
    for year in re.findall(r"\bnam (\d{4})\b", text):
        if not expressions:
            expressions.append({"kind": "year", "year": int(year)})
    return expressions


def _period_measure(question: Question) -> dict | None:
    """Recognize bounded S2 same-period intents without looking at source values."""
    text = plain(question.question)
    op, metrics, filters, refs = None, [], [], []
    weekday = re.search(r"\b(thu (?:hai|ba|tu|nam|sau|bay)|chu nhat)\b", text)
    weekdays = {
        "thu hai": 0,
        "thu ba": 1,
        "thu tu": 2,
        "thu nam": 3,
        "thu sau": 4,
        "thu bay": 5,
        "chu nhat": 6,
    }
    average = bool(re.search(r"\b(trung binh|binh quan|aov)\b", text))
    if re.search(r"\b(ti le|ty le) don (?:bi |bij )?huy\b", text):
        op, metrics, refs = (
            "cancellation_rate",
            ["cancelled_order_count", "placed_order_count"],
            ["cancellation_rate"],
        )
        text = re.sub(r"\b(ti le|ty le) don (?:bi |bij )?huy\b|%|\bphan tram\b", "", text)
    elif average and re.search(r"\b(moi don(?: hang)?|mot don|aov)\b", text):
        op, metrics, refs = (
            "average_order_value",
            ["revenue", "order_count"],
            ["average_order_value", "rounding"],
        )
        text = re.sub(r"\b(trung binh|binh quan|gia tri|moi don(?: hang)?|mot don|aov)\b", "", text)
    elif average and (weekday or re.search(r"\b(moi ngay|mot ngay|hang ngay)\b", text)):
        cue = _metric_cues(question)
        if len(cue) != 1:
            return None
        op, metrics, refs = "average_daily", [cue.pop(), "calendar_day_count"], ["rounding"]
        text = re.sub(r"\b(trung binh|binh quan|moi ngay|mot ngay|hang ngay|cac ngay)\b", "", text)
        if weekday:
            filters = ["weekday=" + str(weekdays[weekday[0]])]
            text = text.replace(weekday[0], "")
    elif re.search(r"\b(ti trong|ty trong)\b", text):
        customer = re.search(
            r"\bkhach (?:hang )?nhom (.+?) trong (?:tong )?(?:doanh thu|dthu)\b", text
        )
        item = re.search(r"\bnhom (.+?) trong (?:tong )?(?:doanh thu|dthu)\b", text)
        match = customer or item
        if not match:
            return None
        field = "customer_group" if customer else "item_group"
        op, metrics, refs = (
            "share",
            ["revenue", "revenue"],
            ["customer_share"] if customer else ["item_group"],
        )
        filters = [field + "=" + match[1].strip()]
        text = text[: match.start()] + text[match.end() :]
        text = re.sub(r"\b(ti trong|ty trong|cua)\b|%|\bphan tram\b", "", text)
    if op is None:
        return None
    # Strip only the newly authorized intent; all existing filter/negation guards
    # still inspect the remaining wording. Never repair the model's plan.
    return {
        "operator": op,
        "metrics": metrics,
        "filters": filters,
        "rule_refs": refs,
        "guard_question": replace(question, question=text),
    }


def _difference_windows(question: Question, *, percent_change: bool = False) -> list[dict]:
    """Finite directional comparisons: current so với baseline, or baseline first.

    Both operands need one literal period. Current-month comparisons use prior MTD
    per runtime authority, checked during validation, never a whole prior month.
    """
    text = plain(question.question)
    percentage_cue = bool(re.search(r"%|\bphan tram\b", text))
    if percent_change:
        text = re.sub(r"^(?:cho (?:minh|toi) hoi|xem giup|coi gium)\s+", "", text)
        text = re.sub(r"\bso vs\b", "so voi", text)
        text = re.sub(r"\bth\b", "thang", text)
        text = re.sub(r"\btrc\b", "truoc", text)
        text = re.sub(r"\bthang lien truoc\b", "thang truoc", text)
    if (
        percentage_cue != percent_change
        or re.search(r"\b(ty le|ti le|ti trong|ty trong|trung binh|tuyet doi)\b", text)
        or (not percent_change and "tang truong" in text)
        or not re.search(r"\b(chenh(?: lech)?|hon|kem|tang|giam|thay doi)\b", text)
        or len(re.findall(r"\bso voi\b", text)) != 1
    ):
        return []
    current, baseline = re.split(r"\bso voi\b", text)
    if not current.strip():
        parts = baseline.split(",", 1)
        if len(parts) != 2:
            return []
        baseline, current = parts
    expressions = []
    for part in (current, baseline):
        found = _literal_windows(
            Question(question.id, question.business_id, part, question.as_of_date)
        )
        if len(found) != 1:
            return []
        expressions.append(found[0])
    if expressions[0]["kind"] == "current_month" and expressions[1]["kind"] == "previous_month":
        expressions[1] = {"kind": "previous_mtd"}
    elif expressions[1]["kind"] == "previous_month":
        if expressions[0]["kind"] != "month":
            return []
        # Anchor "previous month" to the asked explicit month, never the machine/as-of month.
        current = expressions[0]
        try:
            previous = date(current["year"], current["month"], 1) - timedelta(days=1)
        except ValueError:
            return []
        expressions[1] = {"kind": "month", "year": previous.year, "month": previous.month}
    return expressions


def _bind_dependencies(
    schema: dict, metrics: list[str], *, filters: list[str], operator: str, comparison: bool
) -> None:
    """Constrain symbolic operand roles before generation, never repair a draft.

    Tuple items are supported by the pinned llama.cpp JSON-schema converter.
    These names bind dependencies only; no source values or numeric answers enter
    the grammar. The general QueryPlan validator still accepts arbitrary valid IDs.
    """
    props = schema["properties"]
    window = props["time_windows"]["items"]
    agg = props["aggregations"]["items"]
    props["time_windows"]["items"] = []
    for index, name in enumerate(["period", "baseline_period"] if comparison else ["period"]):
        item = deepcopy(window)
        item["properties"]["id"] = {"const": name}
        if comparison:
            item["properties"]["expression"] = deepcopy(
                window["properties"]["expression"]["anyOf"][index]
            )
        props["time_windows"]["items"].append(item)
    props["aggregations"]["items"] = []
    for index, (name, metric) in enumerate(zip(["total", "baseline_total"], metrics, strict=True)):
        item = deepcopy(agg)
        item["properties"].update(
            id={"const": name},
            metric_id={"const": metric},
            window_ref={"const": "baseline_period" if comparison and index else "period"},
            scope={"const": "whole_business"},
            group_by=_array(TEXT, maximum=0),
        )
        values = filters if index == 0 or operator == "average_daily" else []
        item["properties"]["filters"] = _array(
            {"const": values[0]} if values else TEXT, minimum=len(values), maximum=len(values)
        )
        props["aggregations"]["items"].append(item)
    props["derived_measures"]["items"]["properties"].update(
        id={"const": "delta"},
        current_ref={"const": "total"},
        baseline_ref={"const": "baseline_total"},
    )
    props["output"]["properties"]["result_refs"]["items"] = {"const": "delta"}


def generation_schema(
    question: Question,
    *,
    stock: bool = False,
    entity_codes: list[str] | None = None,
) -> dict:
    """Narrow the same QueryPlan schema for an unambiguous literal relative cue.

    No IDs, answers or dates are looked up. Unknown wording retains the full S1
    expression grammar; the resolver/validator still applies runtime rules/as-of.
    """
    schema = deepcopy(PLAN_SCHEMA)
    agg = schema["properties"]["aggregations"]["items"]
    if stock:
        agg["properties"]["metric_id"] = {"const": "inventory_on_hand"}
        agg["properties"]["scope"] = {"const": "product"}
        agg["required"].append("unit")
        schema["properties"]["entities"]["minItems"] = 1
        schema["properties"]["entities"]["maxItems"] = 1
        if entity_codes:
            schema["properties"]["entities"]["items"] = {"enum": entity_codes}
    else:
        metrics = _metric_cues(question)
        if len(metrics) == 1:
            agg["properties"]["metric_id"] = {"const": metrics.pop()}
    measure = None if stock else _period_measure(question)
    if measure:
        for key in ("aggregations",):
            schema["properties"][key].update(minItems=2, maxItems=2)
        schema["properties"]["time_windows"].update(minItems=1, maxItems=1)
        schema["properties"]["derived_measures"].update(minItems=1, maxItems=1)
        agg["properties"]["metric_id"] = {"enum": list(dict.fromkeys(measure["metrics"]))}
        schema["properties"]["derived_measures"]["items"]["properties"]["operator"] = {
            "const": measure["operator"]
        }
        schema["properties"]["output"]["properties"]["expected_type"] = {
            "const": "percent"
            if measure["operator"] in {"share", "cancellation_rate"}
            else "number"
        }
        expressions = _literal_windows(question)
        if len(expressions) == 1:
            schema["properties"]["time_windows"]["items"]["properties"]["expression"] = _object(
                {key: {"const": value} for key, value in expressions[0].items()}
            )
        _bind_dependencies(
            schema,
            measure["metrics"],
            filters=measure["filters"],
            operator=measure["operator"],
            comparison=False,
        )
        return schema
    growth_windows = [] if stock else _difference_windows(question, percent_change=True)
    comparison = growth_windows or ([] if stock else _difference_windows(question))
    difference = bool(comparison)
    for key in ("time_windows", "aggregations"):
        schema["properties"][key].update(
            minItems=2 if difference else 1, maxItems=2 if difference else 1
        )
    schema["properties"]["derived_measures"].update(
        minItems=1 if difference else 0, maxItems=1 if difference else 0
    )
    if difference:
        operator = "percent_change" if growth_windows else "difference"
        schema["properties"]["derived_measures"]["items"]["properties"]["operator"] = {
            "const": operator
        }
        schema["properties"]["output"]["properties"]["expected_type"] = {
            "const": "percent" if growth_windows else "number"
        }
        window = schema["properties"]["time_windows"]["items"]["properties"]
        window["expression"] = {
            "anyOf": [
                _object({key: {"const": value} for key, value in expression.items()})
                for expression in comparison
            ]
        }
        metrics = _metric_cues(question)
        if len(metrics) == 1:
            _bind_dependencies(
                schema, [next(iter(metrics))] * 2, filters=[], operator=operator, comparison=True
            )
        return schema
    expressions = [{"kind": "today"}] if stock else _literal_windows(question)
    if len(expressions) != 1:
        return schema
    window = schema["properties"]["time_windows"]["items"]["properties"]
    window["expression"] = _object({key: {"const": value} for key, value in expressions[0].items()})
    return schema


def _check_question_scope(
    question: Question, *, difference: bool = False, percent_change: bool = False
) -> None:
    """Fail closed for explicit unsupported S1 cues even if the model drops filters.

    This finite guard cannot certify all entity aliases, negations or paraphrases.
    It does not infer a replacement intent or authorize execution.
    """
    text = plain(question.question)
    # A polite trailing question is not a negated business condition.
    text = re.sub(r"\b(duoc\s+)?khong(?:\s+(a|nhi|nhe))?\s*[?!.]*$", "", text)
    # "Tính riêng ngày hôm nay" narrows time, not the business/entity scope.
    text = re.sub(r"\btinh rieng ngay (?=hom nay\b|hom qua\b)", "ngay ", text)
    if percent_change:
        text = re.sub(r"%|\b(phan tram|tang truong)\b", "", text)
        text = re.sub(r"\bso vs\b", "so voi", text)
    if re.search(
        r"%|\b(phan tram|ty le|ti le|ti trong|ty trong|tang truong|trung binh|"
        r"binh quan|aov|growth|ratio|average|tuyet doi)\b",
        text,
    ):
        raise ContractError("question_scope_or_operator_unsupported")
    if not difference and re.search(
        r"%|\b(chenh|chenh lech|hon|kem|tang|giam|thay doi|so voi|so vs|so sanh)\b", text
    ):
        raise ContractError("question_comparison_operator_unsupported")
    if difference:
        text = re.sub(r"\bso voi\b", "", text)
    if re.search(
        r"\b(rieng|san pham|mat hang|khach|nhan vien|ty le|trung binh|so voi|"
        r"nhieu nhat|cao nhat|thap nhat|top|kg|don huy|don tra|khong|chi tinh|"
        r"ngoai tru|loai tru|tru don|tien mat|chuyen khoan|cod|theo kenh|tung nhom|"
        r"ca sang|ca chieu)\b",
        text,
    ):
        raise ContractError("question_scope_or_operator_unsupported")
    owners_removed = re.sub(r"\b(cua\s+)?(cua hang|shop|tiem|doanh nghiep)\b", "", text)
    if re.search(r"\bcua\s+\w", owners_removed):
        raise ContractError("question_owner_filter_unsupported")


def _check_relative_window(
    question: Question,
    context: BusinessContext,
    window: TimeWindow,
) -> list[str]:
    """Check requested bounds before coverage clipping can conceal a wrong period.

    This finite guard and generation hint are not a general Vietnamese parser;
    unknown wording remains the model's responsibility. Never repair a draft.
    """
    expressions = _literal_windows(question)
    if len(expressions) > 1:
        raise ContractError("question_multiple_windows_unsupported")
    if not expressions:
        return []
    expected = resolve_window(expressions[0], question.as_of_date, context.rules, context.calendar)
    if (window.start, min(window.end, window.cutoff or window.end)) != (
        expected.start,
        min(expected.end, expected.cutoff or expected.end),
    ):
        raise ContractError("question_time_mismatch_before_coverage")
    return list(expected.rule_refs)


def validate_query_plan(draft: dict, question: Question, context: BusinessContext) -> dict:
    """Bind server-owned identity/policy/requirements; never trust model policy or numbers.

    The model supplies only the draft fields in PLAN_SCHEMA. Identity, rule references,
    requirements and resolved windows are authoritative fields of the same QueryPlan.
    Structural validity cannot prove perfect interpretation of natural language.
    """
    _schema(draft, PLAN_SCHEMA)
    windows, aggregations, derived = (
        draft["time_windows"],
        draft["aggregations"],
        draft["derived_measures"],
    )
    period_measure = _period_measure(question)
    if period_measure:
        return _validate_period_measure(draft, question, context, period_measure)
    difference = bool(derived)
    stock = len(aggregations) == 1 and aggregations[0]["metric_id"] == "inventory_on_hand"
    growth = difference and derived[0]["operator"] == "percent_change"
    expected_expressions = _difference_windows(question, percent_change=growth)
    if not stock:
        _check_question_scope(
            question,
            difference=difference and bool(expected_expressions),
            percent_change=growth and bool(expected_expressions),
        )
        metrics = _metric_cues(question)
        if len(metrics) > 1 or any(a["metric_id"] not in metrics for a in aggregations if metrics):
            raise ContractError("question_metric_mismatch_or_multiple_metrics")
    if question.business_id != context.catalog["business_id"]:
        raise ContractError("question/context namespace mismatch")
    if (draft["entities"] and not stock) or draft["uncertainties"]:
        raise ContractError("entities or uncertainty unsupported")
    size = 2 if difference else 1
    if len(windows) != size or len(aggregations) != size or (stock and difference):
        raise ContractError("requires one aggregate/window, or two for difference")
    nodes = [*windows, *aggregations, *derived]
    ids = [node["id"] for node in nodes]
    if any(not key.strip() for key in ids) or len(set(ids)) != len(ids):
        raise ContractError("dependency IDs must be nonempty and distinct")
    window_map = {window["id"]: window for window in windows}
    agg_map = {agg["id"]: agg for agg in aggregations}
    if {agg["window_ref"] for agg in aggregations} != set(window_map):
        raise ContractError("unknown or unused window dependency")
    if len({agg["metric_id"] for agg in aggregations}) != 1:
        raise ContractError("difference requires the same metric and unit")
    for agg in aggregations:
        if (
            agg["scope"] != ("product" if stock else "whole_business")
            or agg["filters"]
            or agg["group_by"]
        ):
            raise ContractError("only whole-business totals are supported")
    window, agg = windows[0], aggregations[0]
    contract = metric_contract(context.rules, agg["metric_id"])
    rule_refs = list(contract.rule_refs)
    if difference:
        if not expected_expressions or len(metrics) != 1 or stock:
            raise ContractError("question_difference_direction_or_windows_unsupported")
        operator = derived[0]["operator"]
        context.rules.require(operator)
        rule_refs.append(operator)
        if growth:
            context.rules.require("rounding")
            rule_refs.append("rounding")
        operands = [derived[0][key] for key in ("current_ref", "baseline_ref")]
        if len(set(operands)) != 2 or set(operands) != set(agg_map):
            raise ContractError("difference must reference two distinct scalar aggregations")
        if expected_expressions[1]["kind"] == "previous_mtd":
            rule = context.rules.require("current_month")
            if rule.parameters.get("comparison") != "previous_month_same_days":
                raise ContractError("current_month_comparison_policy_unsupported")
            rule_refs.append("current_month")
        result_ref = derived[0]["id"]
    else:
        result_ref = agg["id"]
    output_type = "percent" if growth else "number"
    if draft["output"] != {"expected_type": output_type, "result_refs": [result_ref]}:
        raise ContractError("output must reference its scalar result")
    entity = None
    if stock:
        text = plain(question.question)
        if (
            len(draft["entities"]) != 1
            or window["expression"] != {"kind": "today"}
            or "hom nay" not in text
            or not re.search(r"\b(ton|kho)\b", text)
            or re.search(r"\b(doanh thu|dthu|tien ban|so don|so bill)\b", text)
            or re.search(r"\b(khong|nhat|ty le|trung binh|so voi|tong gia tri)\b", text)
        ):
            raise ContractError("snapshot_today_single_product_only")
        entity = resolve_stock_entity(
            context, question.question, draft["entities"][0], agg.get("unit", "")
        )
    for entry in aggregations:
        if not stock and "unit" in entry and entry["unit"] != contract.unit:
            raise ContractError("unit_mismatch")
    resolved = {
        w["id"]: resolve_window(
            w["expression"], question.as_of_date, context.rules, context.calendar
        )
        for w in windows
    }
    if difference:
        for ref, expression in zip(operands, expected_expressions, strict=True):
            expected = resolve_window(
                expression, question.as_of_date, context.rules, context.calendar
            )
            actual = resolved[agg_map[ref]["window_ref"]]
            if (actual.start, min(actual.end, actual.cutoff or actual.end)) != (
                expected.start,
                min(expected.end, expected.cutoff or expected.end),
            ):
                raise ContractError("question_time_mismatch_before_coverage")
            rule_refs.extend(expected.rule_refs)
    else:
        rule_refs.extend(_check_relative_window(question, context, resolved[window["id"]]))
    plan = deepcopy(draft)
    for entry in plan["time_windows"]:
        value = resolved[entry["id"]]
        rule_refs.extend(value.rule_refs)
        entry.update({**value.summary(), "id": entry["id"], "time_axis": contract.time_axis})
    for entry in plan["aggregations"]:
        entry.update(
            unit=entity["base_unit"] if entity else contract.unit,
            count_unit=contract.count_unit,
            status_policy=contract.status_policy,
            time_axis=contract.time_axis,
        )
    plan.update(
        version=VERSION,
        question_id=question.id,
        business_id=question.business_id,
        as_of_date=question.as_of_date,
        rulebook_version=context.rules.sha256,
        rule_refs=list(dict.fromkeys(rule_refs)),
        requirements=[
            {
                "metric_id": contract.metric_id,
                "window_refs": [w["id"] for w in windows],
                "required_fields": contract.required_fields,
                "history_requirement": contract.history_requirement,
                "coverage_check": "each_window",
                "capability_check": True,
            }
        ],
    )
    if entity:
        plan["resolved_entity"] = {
            k: entity[k] for k in ["product_code", "product_name", "base_unit"]
        }
    if difference:
        plan["derived_measures"][0].update(
            unit="percent" if growth else contract.unit,
            input_unit=contract.unit,
            rule_refs=[derived[0]["operator"]],
            undefined_policy="abstain",
        )
    if growth:
        plan["derived_measures"][0].update(
            denominator_policy="positive_observed_baseline_only",
            undefined_policy_basis="zero/negative baseline not defined by authority; fail closed",
        )
    plan["output"].update(
        rounding={
            "decimal_places": context.rules.require("rounding").parameters["other_places"],
            "mode": "nearest",
            "unspecified_tie": "abstain",
        }
        if growth
        else contract.rounding,
        ordering="not_applicable",
        empty_policy=contract.empty_policy,
        undefined_policy=contract.undefined_policy,
    )
    return plan


def _validate_period_measure(
    draft: dict, question: Question, context: BusinessContext, measure: dict
) -> dict:
    """Validate one shared period and two scalar dependencies in the same QueryPlan."""
    _check_question_scope(measure["guard_question"])
    if (
        question.business_id != context.catalog["business_id"]
        or draft["entities"]
        or draft["uncertainties"]
    ):
        raise ContractError("period_measure_namespace_or_scope")
    windows, aggs, derived = draft["time_windows"], draft["aggregations"], draft["derived_measures"]
    if len(windows) != 1 or len(aggs) != 2 or len(derived) != 1:
        raise ContractError("period_measure_requires_one_window_two_aggregates")
    ids = [x["id"] for x in [*windows, *aggs, *derived]]
    if any(not x.strip() for x in ids) or len(set(ids)) != len(ids):
        raise ContractError("dependency IDs must be nonempty and distinct")
    d = derived[0]
    amap = {a["id"]: a for a in aggs}
    if (
        d["operator"] != measure["operator"]
        or d["current_ref"] == d["baseline_ref"]
        or {d["current_ref"], d["baseline_ref"]} != set(amap)
    ):
        raise ContractError("period_measure_operator_or_references")
    operands = [amap[d[key]] for key in ["current_ref", "baseline_ref"]]
    contracts = []
    for index, (a, metric) in enumerate(zip(operands, measure["metrics"], strict=True)):
        expected_filters = (
            measure["filters"] if index == 0 or measure["operator"] == "average_daily" else []
        )
        if (
            a["metric_id"] != metric
            or a["window_ref"] != windows[0]["id"]
            or a["scope"] != "whole_business"
            or a["group_by"]
            or a["filters"] != expected_filters
        ):
            raise ContractError("period_measure_metric_window_scope_or_filters")
        c = metric_contract(context.rules, metric)
        if a.get("unit", c.unit) != c.unit:
            raise ContractError("unit_mismatch")
        contracts.append(c)
    for ref in measure["rule_refs"]:
        context.rules.require(ref)
    resolved = resolve_window(
        windows[0]["expression"], question.as_of_date, context.rules, context.calendar
    )
    refs = [
        *measure["rule_refs"],
        *resolved.rule_refs,
        *_check_relative_window(question, context, resolved),
    ]
    for c in contracts:
        refs.extend(c.rule_refs)
    percent = measure["operator"] in {"cancellation_rate", "share"}
    output_type = "percent" if percent else "number"
    if draft["output"] != {"expected_type": output_type, "result_refs": [d["id"]]}:
        raise ContractError("output must reference its scalar result")
    money = contracts[0].unit == "VND" and not percent
    rounding = {
        "decimal_places": 0
        if money
        else context.rules.require("rounding").parameters["other_places"],
        "mode": "nearest",
        "unspecified_tie": "abstain",
    }
    if measure["operator"] == "average_order_value":
        rounding = context.rules.require("average_order_value").parameters
    plan = deepcopy(draft)
    plan["time_windows"][0].update({**resolved.summary(), "id": windows[0]["id"]})
    for a in plan["aggregations"]:
        c = metric_contract(context.rules, a["metric_id"])
        a.update(
            unit=c.unit,
            count_unit=c.count_unit,
            status_policy=c.status_policy,
            time_axis=c.time_axis,
        )
    plan["derived_measures"][0].update(
        unit="percent" if percent else contracts[0].unit,
        input_unit=contracts[0].unit,
        rule_refs=measure["rule_refs"],
        denominator_policy="positive_observed_denominator_only",
        undefined_policy="abstain",
    )
    plan["output"].update(
        rounding=rounding,
        ordering="not_applicable",
        empty_policy="zero_numerator_if_positive_denominator",
        undefined_policy="abstain",
    )
    plan.update(
        version=VERSION,
        question_id=question.id,
        business_id=question.business_id,
        as_of_date=question.as_of_date,
        rulebook_version=context.rules.sha256,
        rule_refs=list(dict.fromkeys(refs)),
        requirements=[
            {
                "metric_id": c.metric_id,
                "window_refs": [windows[0]["id"]],
                "required_fields": c.required_fields,
                "history_requirement": c.history_requirement,
                "coverage_check": "each_window",
                "capability_check": True,
            }
            for c in contracts
        ],
    )
    return plan
