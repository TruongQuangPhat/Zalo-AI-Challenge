"""QueryPlan S1 draft grammar and authoritative enrichment (roadmap appendix E)."""

from __future__ import annotations

import re
from copy import deepcopy

from src.data.catalog import BusinessContext
from src.data.normalization.stock import resolve_stock_entity
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.metric_registry import metric_contract
from src.semantics.rulebook import plain
from src.temporal.resolver import TimeWindow, resolve_window

VERSION = "query-plan-v4"


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
# One grammar shared by generation and validation. S2 can add operators/array cardinality.
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
            _object({"id": TEXT, "expression": EXPRESSION}), minimum=1, maximum=1
        ),
        "aggregations": _array(
            _object(
                {
                    "id": TEXT,
                    "metric_id": {
                        "enum": ["revenue", "order_count", "inventory_on_hand", "unsupported"]
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
            maximum=1,
        ),
        "derived_measures": _array(TEXT),
        "output": _object(
            {
                "expected_type": {"enum": ["number", "unsupported"]},
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
        for item in value:
            _schema(item, schema["items"], path)
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
    text = plain(question.question)
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
    return expressions


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
    expressions = [{"kind": "today"}] if stock else _literal_windows(question)
    if len(expressions) != 1:
        return schema
    window = schema["properties"]["time_windows"]["items"]["properties"]
    window["expression"] = _object({key: {"const": value} for key, value in expressions[0].items()})
    return schema


def _check_question_scope(question: Question) -> None:
    """Fail closed for explicit unsupported S1 cues even if the model drops filters.

    This finite guard cannot certify all entity aliases, negations or paraphrases.
    It does not infer a replacement intent or authorize execution.
    """
    text = plain(question.question)
    # A polite trailing question is not a negated business condition.
    text = re.sub(r"\b(duoc\s+)?khong(?:\s+(a|nhi|nhe))?\s*[?!.]*$", "", text)
    # "Tính riêng ngày hôm nay" narrows time, not the business/entity scope.
    text = re.sub(r"\btinh rieng ngay (?=hom nay\b|hom qua\b)", "ngay ", text)
    if re.search(
        r"\b(rieng|san pham|mat hang|khach|nhan vien|ty le|trung binh|so voi|"
        r"nhieu nhat|cao nhat|thap nhat|top|kg|don huy|don tra|khong|chi tinh|"
        r"ngoai tru|loai tru|tru don|tien mat|chuyen khoan|cod|theo kenh|tung nhom)\b",
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
    stock = (
        len(draft["aggregations"]) == 1
        and draft["aggregations"][0]["metric_id"] == "inventory_on_hand"
    )
    if not stock:
        _check_question_scope(question)
        metrics = _metric_cues(question)
        if len(metrics) > 1 or (metrics and draft["aggregations"][0]["metric_id"] not in metrics):
            raise ContractError("question_metric_mismatch_or_multiple_metrics")
    if question.business_id != context.catalog["business_id"]:
        raise ContractError("question/context namespace mismatch")
    if (draft["entities"] and not stock) or draft["derived_measures"] or draft["uncertainties"]:
        raise ContractError("entities, derived measures or uncertainty unsupported in S1")
    if len(draft["time_windows"]) != 1 or len(draft["aggregations"]) != 1:
        raise ContractError("S1 requires one window and one aggregation")
    window, agg = draft["time_windows"][0], draft["aggregations"][0]
    if not window["id"].strip() or not agg["id"].strip() or window["id"] == agg["id"]:
        raise ContractError("dependency IDs must be nonempty and distinct")
    if agg["window_ref"] != window["id"]:
        raise ContractError("unknown window dependency")
    if (
        agg["scope"] != ("product" if stock else "whole_business")
        or agg["filters"]
        or agg["group_by"]
    ):
        raise ContractError("only whole-business totals are supported")
    if draft["output"] != {"expected_type": "number", "result_refs": [agg["id"]]}:
        raise ContractError("S1 output must reference its scalar aggregation")
    contract = metric_contract(context.rules, agg["metric_id"])
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
    elif "unit" in agg and agg["unit"] != contract.unit:
        raise ContractError("unit_mismatch")
    resolved = resolve_window(
        window["expression"], question.as_of_date, context.rules, context.calendar
    )
    question_refs = _check_relative_window(question, context, resolved)
    plan = deepcopy(draft)
    plan.update(
        {
            "version": VERSION,
            "question_id": question.id,
            "business_id": question.business_id,
            "as_of_date": question.as_of_date,
            "rulebook_version": context.rules.sha256,
            "rule_refs": list(
                dict.fromkeys(contract.rule_refs + list(resolved.rule_refs) + question_refs)
            ),
            "requirements": [
                {
                    "metric_id": contract.metric_id,
                    "window_refs": [window["id"]],
                    "required_fields": contract.required_fields,
                    "history_requirement": contract.history_requirement,
                    "coverage_check": "each_window",
                    "capability_check": True,
                }
            ],
        }
    )
    plan["time_windows"][0].update(resolved.summary())
    plan["time_windows"][0]["id"] = window["id"]
    plan["time_windows"][0]["time_axis"] = contract.time_axis
    plan["aggregations"][0].update(
        {
            "unit": entity["base_unit"] if entity else contract.unit,
            "count_unit": contract.count_unit,
            "status_policy": contract.status_policy,
            "time_axis": contract.time_axis,
        }
    )
    if entity:
        plan["resolved_entity"] = {
            k: entity[k] for k in ["product_code", "product_name", "base_unit"]
        }
    plan["output"].update(
        {
            "rounding": contract.rounding,
            "ordering": "not_applicable",
            "empty_policy": contract.empty_policy,
            "undefined_policy": contract.undefined_policy,
        }
    )
    return plan
