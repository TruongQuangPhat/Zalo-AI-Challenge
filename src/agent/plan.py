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
from src.semantics.rulebook import question_plain
from src.temporal.resolver import TimeWindow, resolve_window

VERSION = "query-plan-v14"


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
            _object({"id": TEXT, "expression": EXPRESSION}), minimum=1, maximum=3
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
                            "quantity",
                            "gross_profit",
                            "receivable",
                            "receivable_customer_count",
                            "debt_collection",
                            "purchase_amount",
                            "stock_balance",
                            "lot_expiry",
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
                    "limit": INTEGER,
                },
                optional=("unit", "limit"),
            ),
            minimum=1,
            maximum=3,
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
                "expected_type": {
                    "enum": [
                        "number",
                        "percent",
                        "string",
                        "list",
                        "table",
                        "boolean",
                        "date",
                        "unsupported",
                    ]
                },
                "result_refs": _array(TEXT, minimum=1, maximum=3),
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


RELATIVE_CUES = {
    r"\b(thang nay|thang hien tai)\b": "current_month",
    r"\b(thang (truoc|trc|roi|vua roi))\b": "previous_month",
    r"\b(tuan nay|tuan hien tai)\b": "current_week",
    r"\b(tuan (truoc|trc|roi|vua roi))\b": "previous_week",
    r"\b(hom qua|hqua|ngay hom qua|bua qua)\b": "yesterday",
    r"\b(hom nay|bua nay|hnay)\b": "today",
}


def _metric_cues(question: Question) -> set[str]:
    """Literal intent cross-check, independent of model output; no numeric answers."""
    text = question_plain(question.question)
    cues = {
        r"\b(doanh thu|doanh so|dthu|revenue|tien ban|tien hang|"
        r"ban duoc (?:tong (?:cong )?|tat ca )?bao nhieu tien|"
        r"thu ve bao nhieu tien)\b": "revenue",
        r"\b((so|may|bao nhieu|bao nhiu|bn|bnhiu|dem|tong) (don|bill)|"
        r"don hang duoc bao nhieu)\b": "order_count",
    }
    metrics = {metric for pattern, metric in cues.items() if re.search(pattern, text)}
    if (
        metrics == {"order_count"}
        and re.search(r"\b(?:don (?:da |bi )?huy|trang thai huy)\b", text)
        and not re.search(r"\b(khong|tra)\b", text)
    ):
        return {"cancelled_order_count"}
    return metrics


DATES = r"(\d{1,2})/(\d{1,2})/(\d{4})"
# Spans _literal_windows turns into windows; anything date-like left over is unparsed.
_PARSED_TIME = (
    r"\btu (?:ngay )?" + DATES + r" (?:den|toi)(?: het)? (?:ngay )?" + DATES,
    r"\b(?:thang|quy) \d{1,2}(?:/| nam )\d{4}\b",
    r"\b(?:ngay|hom|bua) " + DATES + r"\b",
    r"\b(?:ke tu|tinh tu|tu|truoc|sau) " + DATES + r"\b",  # "30 ngày kể từ 18/10/2025"
    r"\btu (?:ngay )?1/1\b",  # year-to-date wording, read with "nam nay"/"dau nam"
    r"\b\d+ ngay gan nhat\b",
    r"\b(?:truoc tet|tet)(?: nam)? \d{4}\b",
    r"\bnam \d{4}\b",
)
_UNPARSED_TIME = r"\b\d{1,2} ?[/-] ?\d{1,4}\b|\b(?:thang|thg|th|t|quy|q) ?\d{1,2}\b"


def _period(prefix: str, max_value: int):
    """Rewrite '<period>/<year>' to the canonical cue; leave impossible values untouched."""

    def rewrite(m: re.Match) -> str:
        value, year = int(m[1]), int(m[2])
        if not 1 <= value <= max_value:
            return m[0]
        return f"{prefix} {value}/{year + 2000 if year < 100 else year}"

    return rewrite


def _time_text(question: Question) -> str:
    """Canonical time wording: common abbreviations become 'thang M/YYYY' or 'quy Q/YYYY'."""
    text = re.sub(r"\bthangs\b", "thang", question_plain(question.question))
    text = re.sub(r"\bnawm\b", "nam", text)
    text = re.sub(r"\bquys\b", "quy", text)
    # Rulebook: "N ngày gần nhất" counts today; "N ngày qua, tính cả hôm nay" says so outright
    # and its "hôm nay" is the range's end, not a second window.
    text = re.sub(
        r"\b(\d+) ngay (?:qua|vua qua|gan day|gan nhat|cuoi(?: cung)?)\s*[,(-]?\s*"
        r"(?:tinh|ke) ca (?:hom nay|bua nay|hnay)\b\)?",
        r"\1 ngay gan nhat",
        text,
    )
    text = re.sub(
        r"\b(?:tu )?dau nam (?:toi|den)(?: het)? (?:gio|nay|chu|bay gio|hom nay|bua nay)\b",
        "tu dau nam",
        text,
    )
    text = re.sub(r"\b((?:den|toi) het) (?=\d{1,2}/\d{1,2}/\d{4})", r"\1 ngay ", text)
    # "từ đầu tháng / từ mùng 1 (tháng này) tới hết hôm nay" is this month to date.
    text = re.sub(
        r"\btu (?:dau thang|mung 1|mong 1|ngay 1)(?: thang nay)? (?:den|toi)(?: het)? "
        r"(?:hom nay|hnay|bua nay|nay)\b",
        "thang nay",
        text,
    )
    if question.as_of_date:
        last = int(question.as_of_date[:4]) - 1
        text = re.sub(r"\b(thang|quy) (\d{1,2}) nam (?:ngoai|roi|truoc)\b", rf"\1 \2/{last}", text)
        text = re.sub(r"\b(thang|quy) (\d{1,2}) nam nay\b", rf"\1 \2/{last + 1}", text)

    # Tết: "Tết âm lịch / Nguyên đán / cổ truyền 2026", "Tết năm nay / năm ngoái".
    text = re.sub(r"\btet (?:am lich|nguyen dan|co truyen)\b", "tet", text)
    if question.as_of_date:
        this_year = int(question.as_of_date[:4])
        text = re.sub(r"\btet nam nay\b", f"tet {this_year}", text)
        text = re.sub(r"\btet nam (?:ngoai|truoc|trc|roi)\b", f"tet {this_year - 1}", text)
    # "17 ngày (ngay) trước Tết" next to one dated Tết is the rulebook's "trước Tết" of that year.
    years = set(re.findall(r"\btet(?: nam)? (\d{4})\b", text))
    if len(years) == 1:
        text = re.sub(
            r"\b(?:\d+ ngay )?(?:ngay )?truoc tet\b(?!(?: nam)? \d{4})",
            f"truoc tet {years.pop()}",
            text,
        )

    # "tháng 10, 11, 12 năm 2025 lần lượt", "th9, th10, th11/2025": an ascending list of
    # months shares the written year when the question reads them one by one (three or
    # more, or "lần lượt"/"nào"); "tháng 7 và tháng 8/2025" alone stays unparsed.
    def _shared_year(m: re.Match) -> str:
        months = [int(n) for n in [m[1], *re.findall(r"\d{1,2}", m[2])]]
        listed = len(months) >= 3 or re.search(r"\b(?:lan luot|nao)\b", text)
        if not listed or months != sorted(set(months)) or not 1 <= months[0] <= months[-1] <= 12:
            return m[0]
        return ", ".join(f"thang {n}/{m[3]}" for n in months)

    text = re.sub(
        r"\bthang (\d{1,2})((?:\s*(?:,|va)\s*(?:thang )?\d{1,2})+)\s*(?:/|-| nam )(\d{4})\b",
        _shared_year,
        text,
    )
    year = r"(\d{4}|\d{2})(?![/-]?\d)"
    # T5/2025, Th 11/25, thg7-2026, thang 5-2026, thang 11/25
    month = _period("thang", 12)
    text = re.sub(r"\b(?:thang|thg|th|t) ?(\d{1,2}) ?[/-] ?" + year, month, text)
    text = re.sub(r"\b(?:quy|q) ?(\d) ?[/-] ?" + year, _period("quy", 4), text)
    # Bare 07/2025 or 5-2026; never part of a day date or an existing cue.
    bare = r"(?<![\d/-])(?<!ngay )(?<!thang )(?<!quy )"
    text = re.sub(bare + r"(\d{1,2}) ?[/-] ?(\d{4})(?![/-]?\d)", month, text)
    # Bare 10/25 only when the second number cannot be a month (two-digit year > 12).
    text = re.sub(
        bare + r"(\d{1,2})/(\d{2})(?![/-]?\d)",
        lambda m: month(m) if int(m[2]) > 12 else m[0],
        text,
    )
    return text


# "tháng 7" with no year anywhere in the question: rulebook "Tháng M không ghi năm".
_BARE_MONTH = r"(?<![\d/])\bthang (\d{1,2})\b(?![/-]?\d| nam \d)"


def _bare_months(text: str) -> list[re.Match]:
    if re.search(r"\b\d{4}\b|\b\d{1,2}/\d{2}\b", text):
        return []
    return [m for m in re.finditer(_BARE_MONTH, text) if 1 <= int(m[1]) <= 12]


def unparsed_time(question: Question) -> bool:
    """Whether a date-like token remains that no finite cue turns into a window."""
    text = _time_text(question)
    for m in reversed(_bare_months(text)):
        text = text[: m.start()] + " " + text[m.end() :]
    for pattern in _PARSED_TIME:
        text = re.sub(pattern, " ", text)
    return bool(re.search(_UNPARSED_TIME, text))


def _literal_windows(question: Question) -> list[dict]:
    """Finite time cues in question order; endpoints do not become extra windows."""
    text = _time_text(question)
    dates = DATES

    def iso(parts) -> str:
        day, month, year = map(int, parts)
        return f"{year:04}-{month:02}-{day:02}"

    span = re.search(r"\btu (?:ngay )?" + dates + r" (?:den|toi)(?: het)? (?:ngay )?" + dates, text)
    if span:
        return [{"kind": "range", "start": iso(span.groups()[:3]), "end": iso(span.groups()[3:])}]
    events = []
    ytd = re.search(r"\btu dau nam\b", text)
    if not ytd and not re.search(r"\b(thang|quy) \d+ nam nay\b|\btet\b", text):
        ytd = re.search(r"\bnam nay\b", text)
    if ytd:
        events.append((ytd.start(), {"kind": "year_to_date"}))
    if ytd or re.search(r"\b\d+ ngay gan nhat\b|\b(thang|tuan) nay\b", text):
        # A relative endpoint belongs to its range, not a second today query.
        text = re.sub(r"\b(?:toi|den)(?: het)?(?: ngay)? (hom nay|bua nay|hnay)\b", "", text)
    for pattern, kind in RELATIVE_CUES.items():
        events.extend((m.start(), {"kind": kind}) for m in re.finditer(pattern, text))
    for label, kind, field in [("thang", "month", "month"), ("quy", "quarter", "quarter")]:
        for m in re.finditer(r"\b" + label + r" (\d{1,2})(?:/| nam )(\d{4})\b", text):
            events.append((m.start(), {"kind": kind, "year": int(m[2]), field: int(m[1])}))
    for m in re.finditer(r"\b(?:ngay|hom|bua) " + dates + r"\b", text):
        events.append((m.start(), {"kind": "day", "date": iso(m.groups())}))
    for m in _bare_months(text):
        events.append((m.start(), {"kind": "month", "month": int(m[1])}))
    for m in re.finditer(r"\b(\d+) ngay gan nhat\b", text):
        events.append((m.start(), {"kind": "last_n_days", "days": int(m[1])}))
    for m in re.finditer(r"\b(truoc tet|tet)(?: nam)? (\d{4})\b", text):
        events.append(
            (m.start(), {"kind": "before_tet" if m[1] == "truoc tet" else "tet", "year": int(m[2])})
        )
    if not events:
        events.extend(
            (m.start(), {"kind": "year", "year": int(m[1])})
            for m in re.finditer(r"\bnam (\d{4})\b", text)
        )
    return [expression for _, expression in sorted(events, key=lambda event: event[0])]


def _multiple_results(question: Question) -> dict | None:
    """Bounded ordered scalar results in the existing QueryPlan, without a DAG."""
    text = question_plain(question.question)
    metrics = _metric_cues(question)
    expressions = _literal_windows(question)
    series = re.search(
        r"\b([23]) thang lien nhau ket thuc o thang (\d{1,2})(?:/| nam )(\d{4})\b", text
    )
    if series and len(metrics) == 1 and "moi thang" in text and "tu thang cu den thang moi" in text:
        size, month, year = map(int, series.groups())
        if not 1 <= month <= 12:
            return None
        final = year * 12 + month - 1
        windows = [
            {"kind": "month", "year": i // 12, "month": i % 12 + 1}
            for i in range(final - size + 1, final + 1)
        ]
        guard = text[: series.start()] + text[series.end() :]
        guard = re.sub(r"\bmoi thang\b|\btu thang cu den thang moi\b", "", guard)
        return {
            "metrics": [next(iter(metrics))] * size,
            "expressions": windows,
            "window_indices": list(range(size)),
            "filters": [[] for _ in windows],
            "guard_question": replace(question, question=guard),
        }
    phone = re.search(r"\b(?:ban )?qua dien thoai\b", text)
    counter = re.search(r"\b(?:ban )?tai quay\b", text)
    explicit_pair = (
        bool(re.search(r"\b(lan luot|moi ben)\b", text))
        or len(re.findall(r"\b(?:bao nhieu|bao nhiu|bnhiu)\b", text)) >= 2
    )
    if phone and counter and explicit_pair and len(metrics) == 1 and len(expressions) == 1:
        scopes = ["phone", "counter"] if phone.start() < counter.start() else ["counter", "phone"]
        guard = re.sub(r"\b(?:ban )?(?:qua dien thoai|tai quay)\b|\b(lan luot|moi ben)\b", "", text)
        return {
            "metrics": [next(iter(metrics))] * 2,
            "expressions": expressions,
            "window_indices": [0, 0],
            "filters": [["channel=" + x] for x in scopes],
            "guard_question": replace(question, question=guard),
        }
    if re.search(r"\bva\b", text) and not re.search(
        r"%|phan tram|so voi|chenh|trung binh|binh quan", text
    ):
        if len(metrics) == 1 and 2 <= len(expressions) <= 3:
            return {
                "metrics": [next(iter(metrics))] * len(expressions),
                "expressions": expressions,
                "window_indices": list(range(len(expressions))),
                "filters": [[] for _ in expressions],
                "guard_question": question,
            }
        if metrics == {"revenue", "order_count"} and len(expressions) == 1:
            revenue = re.search(r"\b(doanh thu|dthu|revenue|tien ban|tien hang)\b", text)
            count = re.search(r"\b(so|may|bao nhieu|bn|bnhiu|dem|tong) (don|bill)\b", text)
            if revenue and count:
                ordered = (
                    ["revenue", "order_count"]
                    if revenue.start() < count.start()
                    else ["order_count", "revenue"]
                )
                return {
                    "metrics": ordered,
                    "expressions": expressions,
                    "window_indices": [0, 0],
                    "filters": [[], []],
                    "guard_question": question,
                }
    return None


def _multiple_schema(schema: dict, spec: dict) -> dict:
    props = schema["properties"]
    window_template = props["time_windows"]["items"]
    agg_template = props["aggregations"]["items"]
    windows, aggs = [], []
    for index, expression in enumerate(spec["expressions"]):
        item = deepcopy(window_template)
        item["properties"] = {
            "id": {"const": f"period_{index}"},
            "expression": _object({k: {"const": v} for k, v in expression.items()}),
        }
        windows.append(item)
    for index, metric in enumerate(spec["metrics"]):
        item = deepcopy(agg_template)
        filters = spec["filters"][index]
        item["properties"].update(
            id={"const": f"total_{index}"},
            metric_id={"const": metric},
            window_ref={"const": f"period_{spec['window_indices'][index]}"},
            scope={"const": "whole_business"},
            group_by=_array(TEXT, maximum=0),
            filters=_array(
                {"const": filters[0]} if filters else TEXT,
                minimum=len(filters),
                maximum=len(filters),
            ),
        )
        aggs.append(item)
    props["time_windows"] = _array(windows, minimum=len(windows), maximum=len(windows))
    props["aggregations"] = _array(aggs, minimum=len(aggs), maximum=len(aggs))
    props["derived_measures"]["maxItems"] = 0
    props["output"]["properties"] = {
        "expected_type": {"const": "list"},
        "result_refs": _array(
            [{"const": f"total_{i}"} for i in range(len(aggs))],
            minimum=len(aggs),
            maximum=len(aggs),
        ),
    }
    return schema


def _shift_text(question: Question) -> str:
    text = re.sub(r"\bca sangs\b", "ca sang", question_plain(question.question))
    return re.sub(r"\bso vs\b", "so voi", text)


def _shift_operand_order(text: str) -> list[str]:
    found = re.findall(r"\bca (sang|chieu)\b", text)
    prefix = re.search(r"\bso voi ca (sang|chieu)\b", text)
    first = re.search(r"\bca (sang|chieu)\b", text)
    if prefix and prefix.start() < first.start():
        baseline = "morning" if prefix[1] == "sang" else "afternoon"
        return ["afternoon" if baseline == "morning" else "morning", baseline]
    current = "morning" if found[0] == "sang" else "afternoon"
    return [current, "afternoon" if current == "morning" else "morning"]


def _single_shift(question: Question) -> dict | None:
    text = _shift_text(question)
    found = re.findall(r"\bca (sang|chieu)\b", text)
    if len(found) != 1:
        return None
    guard = re.sub(r"\b(?:cua )?(?:rieng )?ca (sang|chieu)\b", "", text)
    return {
        "filters": ["shift=" + ("morning" if found[0] == "sang" else "afternoon")],
        "guard_question": replace(question, question=guard),
    }


def _single_scope(question: Question) -> dict | None:
    shift = _single_shift(question)
    if shift:
        return {**shift, "rule_refs": ["shift"]}
    text = question_plain(question.question)
    if re.search(r"%|phan tram|ti trong|ty trong|trung binh|binh quan|chenh|so voi", text):
        return None
    metrics = _metric_cues(question)
    if len(metrics) != 1:
        return None
    phone = re.search(r"\b(?:ban )?qua dien thoai\b", text)
    counter = re.search(r"\b(?:ban )?tai quay\b", text)
    if bool(phone) != bool(counter):
        match = phone or counter
        return {
            "filters": ["channel=" + ("phone" if phone else "counter")],
            "guard_question": replace(
                question, question=text[: match.start()] + text[match.end() :]
            ),
            "rule_refs": [],
        }
    if metrics != {"revenue"}:
        return None
    match = re.search(
        # "nhom hang X" names group X; "khach nhom X mua" names customer group X.
        r"\b(khach (?:hang )?nhom|nhom(?: hang)?) (.+?) (?:da )?(?:mua )?"
        r"(?=ban\b|trong\b|thang\b|quy\b|nam\b)",
        text,
    )
    if match and not re.search(r"\b(va|nhom|ngoai tru|so voi)\b", match[2]):
        customer = match[1].startswith("khach")
        field = "customer_group" if customer else "item_group"
        guard = text[: match.start()] + text[match.end() :]
        return {
            "filters": [field + "=" + match[2].strip()],
            "guard_question": replace(question, question=guard),
            "rule_refs": ["customer_share" if customer else "item_group"],
        }
    return None


def _period_measure(question: Question) -> dict | None:
    """Recognize bounded S2 same-period intents without looking at source values."""
    text = _shift_text(question)
    op, metrics, filters, refs = None, [], [], []
    if "ca chieu" in text and "ca sang" in text and re.search(r"\b(hon|kem|chenh|so voi)\b", text):
        metrics = _metric_cues(question)
        if len(metrics) != 1:
            return None
        metric = next(iter(metrics))
        percent = bool(re.search(r"%|phan tram", text))
        guard = re.sub(
            r"\b(?:cua )?(?:rieng )?ca (sang|chieu)\b|%|"
            r"\b(phan tram|so voi|hon(?: hay kem)?|kem|chenh(?: lech)?|"
            r"tang(?: hay giam)?|giam)\b",
            "",
            text,
        )
        return {
            "operator": "percent_change" if percent else "difference",
            "metrics": [metric, metric],
            "filters": [],
            "operand_filters": [["shift=" + x] for x in _shift_operand_order(text)],
            "rule_refs": ["shift", "percent_change" if percent else "difference"],
            "guard_question": replace(question, question=guard),
        }
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
            r"\bkhach (?:hang )?nhom (.+?) trong (?:tong )?(?:doanh thu|dthu|revenue)\b", text
        )
        item = re.search(r"\bnhom (.+?) trong (?:tong )?(?:doanh thu|dthu|revenue)\b", text)
        match = customer or item
        channel = re.search(
            r"\bcac don ban qua dien thoai trong (?:tong )?(?:doanh thu|dthu|revenue)\b", text
        )
        if not match and not channel:
            return None
        field = "customer_group" if customer else "channel" if channel else "item_group"
        op, metrics, refs = (
            "share",
            ["revenue", "revenue"],
            ["customer_share"] if customer else [] if channel else ["item_group"],
        )
        filters = [field + "=" + ("phone" if channel else match[1].strip())]
        chosen = channel or match
        text = text[: chosen.start()] + text[chosen.end() :]
        text = re.sub(r"\b(ti trong|ty trong|cua|cuar)\b|%|\bphan tram\b", "", text)
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
    text = question_plain(question.question)
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
    schema: dict,
    metrics: list[str],
    *,
    filters: list[str],
    operator: str,
    comparison: bool,
    operand_filters: list[list[str]] | None = None,
) -> None:
    """Constrain symbolic operand roles before generation, never repair a draft.

    Tuple items are converted to ``prefixItems`` for the vLLM grammar backend.
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
        values = (
            operand_filters[index]
            if operand_filters is not None
            else filters
            if index == 0 or operator == "average_daily"
            else []
        )
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
    candidates: list[dict] | None = None,
) -> dict:
    """Narrow the same QueryPlan schema for an unambiguous literal relative cue.

    No IDs, answers or dates are looked up. Unknown wording retains the full S1
    expression grammar; the resolver/validator still applies runtime rules/as-of.
    """
    schema = deepcopy(PLAN_SCHEMA)
    from src.agent.entities import business_schema, business_spec

    spec = None if stock else business_spec(question)
    if spec:
        return business_schema(schema, spec, candidates or [])
    # New grouped/business operators never enter the legacy scalar decoder.
    from src.semantics.metric_registry import BUSINESS_METRICS

    scalar = schema["properties"]["aggregations"]["items"]["properties"]
    scalar.pop("limit", None)
    scalar["metric_id"]["enum"] = [
        metric for metric in scalar["metric_id"]["enum"] if metric not in BUSINESS_METRICS
    ]
    multiple = None if stock else _multiple_results(question)
    if multiple:
        return _multiple_schema(schema, multiple)
    schema["properties"]["output"]["properties"]["result_refs"].update(maxItems=1)
    schema["properties"]["output"]["properties"]["expected_type"] = {"const": "number"}
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
            if measure["operator"] in {"share", "cancellation_rate", "percent_change"}
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
            operand_filters=measure.get("operand_filters"),
        )
        return schema
    shift = None if stock else _single_scope(question)
    if shift:
        agg["properties"]["filters"] = _array({"const": shift["filters"][0]}, minimum=1, maximum=1)
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
    text = question_plain(question.question)
    if match := re.search(
        r"\b(no|nowj|cong no|gia von|lai gop|lai rong|phieu nhap|han dung|het han)\b", text
    ):
        raise ContractError("business_metric_or_scope_unsupported")
    if _metric_cues(question) == {"cancelled_order_count"}:
        text = re.sub(r"\b(?:da huy|bi huy|trang thai huy|don huy)\b", "", text)
    if match := re.search(r"\b\d{1,2}(?::\d{2}|h)\b", text):
        raise ContractError("explicit_clock_range_unsupported")
    # A polite trailing question is not a negated business condition.
    text = re.sub(r"\b(duoc\s+)?khong(?:\s+(a|nhi|nhe))?\s*[?!.]*$", "", text)
    # "Tính riêng ngày hôm nay" narrows time, not the business/entity scope.
    text = re.sub(r"\btinh rieng ngay (?=hom nay\b|hom qua\b)", "ngay ", text)
    if percent_change:
        text = re.sub(r"%|\b(phan tram|tang truong)\b", "", text)
        text = re.sub(r"\bso vs\b", "so voi", text)
    if match := re.search(
        r"%|\b(phan tram|ty le|ti le|ti trong|ty trong|tang truong|trung binh|"
        r"binh quan|aov|growth|ratio|average|tuyet doi)\b",
        text,
    ):
        raise ContractError(
            "question_scope_or_operator_unsupported",
            diagnostics={
                "guard": "_check_question_scope",
                "branch": "derived_operator_not_bound",
                "condition_status": "unresolved",
                "unresolved_conditions": [match[0]],
                "capability": "requires_explicit_binding",
            },
        )
    if not difference and re.search(
        r"%|\b(chenh|chenh lech|hon|kem|tang|giam|thay doi|so voi|so vs|so sanh)\b", text
    ):
        raise ContractError("question_comparison_operator_unsupported")
    if difference:
        text = re.sub(r"\bso voi\b", "", text)
    if match := re.search(
        r"\b(rieng|san pham|mat hang|khach|nhan vien|ty le|trung binh|so voi|"
        r"nhieu nhat|cao nhat|thap nhat|top|kg|don huy|don tra|khong|chi tinh|"
        r"ngoai tru|loai tru|tru don|tien mat|chuyen khoan|cod|theo kenh|tung nhom|"
        r"ca sang|ca chieu|da huy|bi huy|trang thai huy|da tra|bi tra|"
        r"co doanh thu tu|co dthu tu|hoat chat|qua dien thoai|tai quay|"
        r"may ngay|bao nhieu ngay|doanh thu dat|dthu dat|tro len|tro xuong)\b",
        text,
    ):
        raise ContractError(
            "question_scope_or_operator_unsupported",
            diagnostics={
                "guard": "_check_question_scope",
                "branch": "filter_entity_or_qualifier_not_bound",
                "condition_status": "unresolved",
                "unresolved_conditions": [match[0]],
                "capability": "requires_explicit_binding",
            },
        )
    scope_text = re.sub(r"\b(cua hang|ban hang|don hang|tien hang)\b", "", text)
    scope_text = re.sub(r"\b\d+ ngay gan nhat\b", "", scope_text)
    if match := re.search(
        r"\b(hang|mon)\s+\w|\b(nhat|nhap|nhom|ngay nao|lien nhau|moi thang)\b", scope_text
    ):
        raise ContractError(
            "question_scope_or_operator_unsupported",
            diagnostics={
                "guard": "_check_question_scope",
                "branch": "group_ranking_or_history_not_bound",
                "condition_status": "unresolved",
                "unresolved_conditions": [match[0]],
                "capability": "requires_explicit_binding",
            },
        )
    owners_removed = re.sub(
        r"\b(cua\s+)?(cua hang|shop|sop|tiem|quan|nha thuoc|doanh nghiep)( minh)?\b", "", text
    )
    if match := re.search(r"\b(cua|cuar)\s+\w", owners_removed):
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
    from src.agent.entities import business_spec, validate_business_plan

    spec = (
        None
        if any(a["metric_id"] == "inventory_on_hand" for a in aggregations)
        else business_spec(question)
    )
    if spec:
        return validate_business_plan(draft, question, context, spec)
    from src.semantics.metric_registry import BUSINESS_METRICS

    if any(a["metric_id"] in BUSINESS_METRICS for a in aggregations):
        raise ContractError("question_business_metric_unsupported")
    if any("limit" in a for a in aggregations):
        raise ContractError("ranking_limit_unsupported_for_scalar")
    multiple = _multiple_results(question)
    if multiple:
        return _validate_multiple_results(draft, question, context, multiple)
    period_measure = _period_measure(question)
    if period_measure:
        return _validate_period_measure(draft, question, context, period_measure)
    difference = bool(derived)
    stock = len(aggregations) == 1 and aggregations[0]["metric_id"] == "inventory_on_hand"
    growth = difference and derived[0]["operator"] == "percent_change"
    expected_expressions = _difference_windows(question, percent_change=growth)
    shift = None if stock else _single_scope(question)
    if not stock:
        _check_question_scope(
            shift["guard_question"] if shift else question,
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
            or agg["filters"] != (shift["filters"] if shift else [])
            or agg["group_by"]
            or "limit" in agg
        ):
            raise ContractError("only whole-business totals are supported")
    window, agg = windows[0], aggregations[0]
    contract = metric_contract(
        context.rules,
        agg["metric_id"],
        shape=context.prepared.summary.get("shape") if context.prepared else None,
    )
    rule_refs = list(contract.rule_refs)
    if shift:
        for ref in shift["rule_refs"]:
            context.rules.require(ref)
            rule_refs.append(ref)
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
        text = question_plain(question.question)
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
            measure["operand_filters"][index]
            if "operand_filters" in measure
            else measure["filters"]
            if index == 0 or measure["operator"] == "average_daily"
            else []
        )
        if (
            a["metric_id"] != metric
            or a["window_ref"] != windows[0]["id"]
            or a["scope"] != "whole_business"
            or a["group_by"]
            or a["filters"] != expected_filters
        ):
            raise ContractError("period_measure_metric_window_scope_or_filters")
        c = metric_contract(
            context.rules,
            metric,
            shape=context.prepared.summary.get("shape") if context.prepared else None,
        )
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
    percent = measure["operator"] in {"cancellation_rate", "share", "percent_change"}
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
    if measure["operator"] == "difference":
        rounding = contracts[0].rounding
    if measure["operator"] == "average_order_value":
        rounding = context.rules.require("average_order_value").parameters
    plan = deepcopy(draft)
    plan["time_windows"][0].update({**resolved.summary(), "id": windows[0]["id"]})
    for a in plan["aggregations"]:
        c = metric_contract(
            context.rules,
            a["metric_id"],
            shape=context.prepared.summary.get("shape") if context.prepared else None,
        )
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


def _validate_multiple_results(
    draft: dict, question: Question, context: BusinessContext, spec: dict
) -> dict:
    """Validate every result against question order and registered scalar contracts."""
    _check_question_scope(spec["guard_question"])
    context.rules.require("ordered_results")
    if (
        question.business_id != context.catalog["business_id"]
        or draft["entities"]
        or draft["uncertainties"]
        or draft["derived_measures"]
    ):
        raise ContractError("multiple_results_namespace_or_scope")
    windows, aggs = draft["time_windows"], draft["aggregations"]
    nodes = [*windows, *aggs]
    ids = [n["id"] for n in nodes]
    if any(not i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ContractError("dependency IDs must be nonempty and distinct")
    if len(windows) != len(spec["expressions"]) or len(aggs) != len(spec["metrics"]):
        raise ContractError("question_multiple_windows_or_results_dependency_count")
    amap = {a["id"]: a for a in aggs}
    wmap = {w["id"]: w for w in windows}
    refs = draft["output"]["result_refs"]
    if (
        draft["output"]["expected_type"] != "list"
        or len(refs) != len(aggs)
        or len(set(refs)) != len(refs)
        or set(refs) != set(amap)
    ):
        raise ContractError("multiple_results_output_references")
    if {a["window_ref"] for a in aggs} != set(wmap):
        raise ContractError("unknown or unused window dependency")
    resolved = {
        key: resolve_window(w["expression"], question.as_of_date, context.rules, context.calendar)
        for key, w in wmap.items()
    }
    contracts = []
    rule_refs = ["ordered_results"]
    for i, ref in enumerate(refs):
        a = amap[ref]
        c = metric_contract(
            context.rules,
            spec["metrics"][i],
            shape=context.prepared.summary.get("shape") if context.prepared else None,
        )
        if (
            a["metric_id"] != c.metric_id
            or a["scope"] != "whole_business"
            or a["filters"] != spec["filters"][i]
            or a["group_by"]
            or a.get("unit", c.unit) != c.unit
        ):
            raise ContractError("multiple_results_metric_scope_filters_or_order")
        expected = resolve_window(
            spec["expressions"][spec["window_indices"][i]],
            question.as_of_date,
            context.rules,
            context.calendar,
        )
        actual = resolved[a["window_ref"]]
        if (actual.start, min(actual.end, actual.cutoff or actual.end)) != (
            expected.start,
            min(expected.end, expected.cutoff or expected.end),
        ):
            raise ContractError("question_time_mismatch_before_coverage")
        contracts.append(c)
        rule_refs.extend(c.rule_refs)
        rule_refs.extend(expected.rule_refs)
    plan = deepcopy(draft)
    for w in plan["time_windows"]:
        w.update(
            {**resolved[w["id"]].summary(), "id": w["id"], "time_axis": contracts[0].time_axis}
        )
    by_ref = dict(zip(refs, contracts, strict=True))
    for a in plan["aggregations"]:
        c = by_ref[a["id"]]
        a.update(
            unit=c.unit,
            count_unit=c.count_unit,
            status_policy=c.status_policy,
            time_axis=c.time_axis,
        )
    plan["output"].update(
        ordering="question_order",
        rounding=[c.rounding for c in contracts],
        undefined_policy="abstain_if_any_operand_unverified",
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
                "metric_id": by_ref[ref].metric_id,
                "window_refs": [amap[ref]["window_ref"]],
                "required_fields": by_ref[ref].required_fields,
                "history_requirement": by_ref[ref].history_requirement,
                "coverage_check": "each_window",
                "capability_check": True,
            }
            for ref in refs
        ],
    )
    return plan
