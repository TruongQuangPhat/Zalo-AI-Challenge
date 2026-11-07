from __future__ import annotations

import csv
import hashlib
import multiprocessing
import socket
import sqlite3
import time
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from src.agent.context import build_context
from src.agent.execution import execute_plan
from src.agent.local import LocalModel, guided_schema
from src.agent.plan import PLAN_SCHEMA, _schema, generation_schema, validate_query_plan
from src.agent.planner import LocalPlanner
from src.data.split import Question
from src.evaluation.scorer import evaluate
from src.jsonio import ContractError, dumps, loads
from src.runtime import run_inference
from tests.helpers import StubPlanner


def question(**changes):
    return replace(Question("synthetic", "shop_a", "Doanh thu tháng này?", "2026-01-03"), **changes)


def draft(metric="revenue", expression=None):
    return {
        "entities": [],
        "time_windows": [{"id": "period", "expression": expression or {"kind": "current_month"}}],
        "aggregations": [
            {
                "id": "total",
                "metric_id": metric,
                "window_ref": "period",
                "scope": "whole_business",
                "filters": [],
                "group_by": [],
            }
        ],
        "derived_measures": [],
        "output": {"expected_type": "number", "result_refs": ["total"]},
        "uncertainties": [],
    }


def multiple_draft(expressions, metrics, *, indices=None, filters=None):
    indices = indices or list(range(len(metrics)))
    return {
        "entities": [],
        "time_windows": [{"id": f"period_{i}", "expression": e} for i, e in enumerate(expressions)],
        "aggregations": [
            {
                "id": f"total_{i}",
                "metric_id": metric,
                "window_ref": f"period_{indices[i]}",
                "scope": "whole_business",
                "filters": filters[i] if filters else [],
                "group_by": [],
            }
            for i, metric in enumerate(metrics)
        ],
        "derived_measures": [],
        "output": {
            "expected_type": "list",
            "result_refs": [f"total_{i}" for i in range(len(metrics))],
        },
        "uncertainties": [],
    }


def test_ordered_three_month_results_keep_question_order_and_each_window(semantic_source):
    c = semantic_source(
        rows=[
            ["a", "01/11/2025", "Xong", "300", "A"],
            ["b", "01/12/2025", "Xong", "100", "A"],
            ["c", "03/01/2026", "Xong", "200", "A"],
        ],
        complete=False,
    )
    expr = [
        {"kind": "month", "year": y, "month": m} for y, m in [(2025, 11), (2025, 12), (2026, 1)]
    ]
    p = multiple_draft(expr, ["revenue"] * 3)
    q = question(
        question="Doanh thu 3 tháng liền nhau kết thúc ở tháng 1/2026 là bao nhiêu mỗi tháng, "
        "từ tháng cũ đến tháng mới?"
    )
    _schema(p, generation_schema(q))
    p["time_windows"].reverse()
    p["aggregations"].reverse()
    result = execute_plan(p, q, c)
    assert result["answer"] == {"id": q.id, "type": "list", "answer": [300, 100, 200]}
    assert len(result["plan"]["preflight"]) == 3
    assert result["execution"]["checks"][-1] == "no_missing_operand_substitution"
    p["output"]["result_refs"].reverse()
    with pytest.raises(ContractError):
        execute_plan(p, q, c)


def test_ordered_channels_keep_requested_direction_and_status_policy(semantic_source):
    c = semantic_source(
        rows=[
            ["a", "01/01/2026", "Xong", "90", "A", "store"],
            ["b", "02/01/2026", "Xong", "30", "A", "phone"],
            ["c", "03/01/2026", "Huỷ", "900", "A", "phone"],
        ],
        extra_columns=["channel"],
        readme_extra="- **channel**: sales channel\n",
    )
    p = multiple_draft(
        [{"kind": "current_month"}],
        ["revenue"] * 2,
        indices=[0, 0],
        filters=[["channel=counter"], ["channel=phone"]],
    )
    q = question(
        question="Doanh thu các đơn bán tại quầy và doanh thu các đơn bán qua điện thoại "
        "tháng này lần lượt là bao nhiêu?"
    )
    _schema(p, generation_schema(q))
    assert execute_plan(p, q, c)["answer"]["answer"] == [90, 30]
    p["aggregations"][0]["filters"] = ["channel=phone"]
    with pytest.raises(ContractError):
        execute_plan(p, q, c)


def test_multiple_metrics_do_not_sum_or_sort_heterogeneous_units(semantic_source):
    c = semantic_source(
        rows=[["a", "01/01/2026", "Xong", "100", "A"], ["b", "02/01/2026", "Xong", "50", "B"]]
    )
    p = multiple_draft([{"kind": "current_month"}], ["order_count", "revenue"], indices=[0, 0])
    q = question(question="Số đơn và doanh thu tháng này lần lượt bao nhiêu?")
    assert execute_plan(p, q, c)["answer"]["answer"] == [2, 150]


@pytest.mark.parametrize(
    "text,expression",
    [
        ("Số đơn tuần trc?", {"kind": "previous_week"}),
        ("Số đơn từ đầu nawm tới hết bữa nay?", {"kind": "year_to_date"}),
        ("Doanh thu Tết năm 2026?", {"kind": "tet", "year": 2026}),
        ("Doanh thu 30 ngày gần nhất tính đến hôm nay?", {"kind": "last_n_days", "days": 30}),
    ],
)
def test_time_guard_keeps_relative_alias_and_range_endpoints(text, expression):
    from src.agent.plan import _literal_windows

    assert _literal_windows(question(question=text)) == [expression]


class ScriptedModel:
    metadata = {"test_double": True}

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def generate(self, messages, schema, timeout):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return {"text": response, "finish_reason": "stop"}


@pytest.mark.parametrize(
    "metric,expected", [("revenue", Decimal(151)), ("order_count", Decimal(2))]
)
def test_exact_execution_status_dedup_lineage_readonly(semantic_source, metric, expected):
    context = semantic_source(
        rows=[
            ["001", "01/01/2026", "Xong", "100.25", "A"],
            ["001", "01/01/2026", "Xong", "100.25", "A"],
            ["002", "03/01/2026", "Đang giao", "50.35", "B"],
            ["003", "03/01/2026", "Đổi trả", "9999", "C"],
            ["004", "03/01/2026", "Huỷ", "8888", "D"],
        ]
    )
    before = context.prepared.path.read_bytes()
    result = execute_plan(
        draft(metric),
        question(
            question="Số đơn tháng này?" if metric == "order_count" else "Doanh thu tháng này?"
        ),
        context,
    )
    assert result["answer"] == {"id": "synthetic", "type": "number", "answer": expected}
    assert result["execution"]["status"] == "verified"
    assert result["execution"]["orders_selected"] == 2
    assert result["plan"]["requirements"][0]["coverage_check"] == "each_window"
    assert result["plan"]["rulebook_version"] == context.rules.sha256
    assert context.prepared.path.read_bytes() == before
    with context.prepared.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM orders")


@pytest.mark.parametrize(
    "complete,expression,kind",
    [
        (True, {"kind": "day", "date": "2026-01-02"}, "number"),
        (False, {"kind": "day", "date": "2026-01-02"}, "number"),
        (False, {"kind": "day", "date": "2026-01-04"}, "unanswerable"),
    ],
)
def test_empty_unknown_and_proven_outside_are_distinct(semantic_source, complete, expression, kind):
    context = semantic_source(complete=complete)
    result = execute_plan(
        draft(expression=expression),
        question(question=f"Doanh thu ngày {expression['date']}?"),
        context,
    )
    assert result["answer"]["type"] == kind
    if kind == "number":
        assert result["answer"]["answer"] == 0
    else:
        assert result["execution"]["status"] == "not_performed"


def test_partial_window_and_large_decimal_do_not_use_float_or_default_precision(semantic_source):
    context = semantic_source(
        rows=[
            ["001", "01/01/2026", "Xong", "123456789012345678901234567890.49", "A"],
            ["002", "03/01/2026", "Xong", "0.02", "B"],
        ]
    )
    result = execute_plan(
        draft(expression={"kind": "range", "start": "2025-12-01", "end": "2026-01-03"}),
        question(question="Doanh thu từ 01/12/2025 đến 03/01/2026?"),
        context,
    )
    assert result["execution"]["exact_value"] == Decimal("123456789012345678901234567890.51")
    assert result["answer"]["answer"] == Decimal("123456789012345678901234567891")
    assert result["plan"]["time_windows"][0]["effective_window"]["start"] == "2026-01-01"


@pytest.mark.parametrize("metric,current,whole", [("order_count", 1, 2), ("revenue", 100, 1100)])
def test_current_month_excludes_future_facts_even_when_whole_month_is_available(
    semantic_source,
    metric,
    current,
    whole,
):
    context = semantic_source(
        coverage_end="31/01/2026",
        rows=[
            ["1", "01/01/2026", "Xong", "100", "A"],
            ["2", "20/01/2026", "Xong", "1000", "B"],
        ],
    )
    relative = execute_plan(
        draft(metric),
        question(
            question="Số đơn tháng này?" if metric == "order_count" else "Doanh thu tháng này?"
        ),
        context,
    )
    explicit = execute_plan(
        draft(metric, {"kind": "month", "year": 2026, "month": 1}),
        question(question="Tổng trong tháng 1/2026?"),
        context,
    )
    assert relative["answer"]["answer"] == current
    assert explicit["answer"]["answer"] == whole
    assert relative["plan"]["time_windows"][0]["requested_end"] == "2026-01-03"
    assert explicit["plan"]["time_windows"][0]["requested_end"] == "2026-01-31"
    with pytest.raises(ContractError, match="question_time_mismatch"):
        execute_plan(
            draft(metric, {"kind": "month", "year": 2026, "month": 1}),
            question(
                question="Số đơn tháng này?" if metric == "order_count" else "Doanh thu tháng này?"
            ),
            context,
        )


def test_quality_is_metric_specific_and_unspecified_rounding_tie_abstains(semantic_source):
    context = semantic_source(rows=[["1", "01/01/2026", "Xong", "bad", "A"]])
    assert execute_plan(draft(), question(), context)["answer"]["type"] == "abstain"
    assert (
        execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), context)[
            "answer"
        ]["answer"]
        == 1
    )
    context = semantic_source(rows=[["1", "01/01/2026", "Xong", "10.5", "A"]])
    result = execute_plan(draft(), question(), context)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "rounding_tie_unspecified"


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(answer=999),
        lambda p: p.update(sql="SELECT 999"),
        lambda p: p["aggregations"][0].update(metric_id="quantity"),
        lambda p: p["aggregations"][0].update(scope="customer"),
        lambda p: p["aggregations"][0].update(filters=["item=A"]),
        lambda p: p["aggregations"][0].update(group_by=["item"]),
        lambda p: p["aggregations"][0].update(window_ref="missing"),
        lambda p: p["output"].update(result_refs=["missing"]),
        lambda p: p.update(entities=["A"]),
        lambda p: p.update(derived_measures=["growth"]),
        lambda p: p.update(uncertainties=["unsupported status override"]),
        lambda p: p["time_windows"].append(deepcopy(p["time_windows"][0])),
        lambda p: p["time_windows"][0].update(
            expression={"kind": "month", "year": True, "month": 1}
        ),
        lambda p: p["time_windows"][0].update(expression={"kind": "day", "date": "2026-02-30"}),
    ],
)
def test_validator_rejects_semantic_scope_dependencies_and_injection(semantic_source, change):
    context = semantic_source()
    plan = draft()
    change(plan)
    with pytest.raises(ContractError):
        validate_query_plan(plan, question(), context)


def test_retry_is_bounded_and_never_erases_unsupported_intent(semantic_source):
    context = semantic_source()
    model = ScriptedModel("{", dumps(draft("order_count")))
    result = LocalPlanner(model).decide(question(question="Số đơn tháng này?"), context=context)
    assert result["answer"]["answer"] == 2 and model.calls == 2
    bad = draft()
    bad["aggregations"][0]["filters"] = ["item=A"]
    model = ScriptedModel(dumps(bad), dumps(draft()))
    assert LocalPlanner(model).decide(question(), context=context)["answer"]["type"] == "abstain"
    assert model.calls == 1
    model = ScriptedModel(ContractError("timeout"), ContractError("worker unavailable"))
    assert LocalPlanner(model).decide(question(), context=context)["answer"]["type"] == "abstain"
    assert model.calls == 2


def test_context_and_runtime_are_offline_label_free_immutable_and_scored_separately(
    semantic_source,
    synthetic_split,
    tmp_path,
    monkeypatch,
):
    context = semantic_source()
    payload = build_context(question(), context)
    assert "coverage" in payload and "facts_schema" in payload and payload["warnings"]
    assert "source_values" not in dumps(payload)
    assert "100.25" not in dumps(payload)
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in synthetic_split.rglob("*")
        if p.is_file()
    }
    original = Path.open

    def guarded(path, *args, **kwargs):
        if path.name in {"answers.jsonl", "qa.jsonl"}:
            raise AssertionError("label read during inference")
        return original(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("network during inference")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded)
        patch.setattr(socket, "socket", no_network)
        patch.setattr(socket, "create_connection", no_network)
        today = dumps(draft(expression={"kind": "today"}))
        model = ScriptedModel(today, today)
        output = tmp_path / "end-to-end"
        manifest = run_inference(synthetic_split, output, planner=LocalPlanner(model))
    assert manifest["adapter"] == "local-planner" and manifest["question_errors"] == 0
    log = loads(next((output / "logs").glob("*.json")).read_text())
    assert log["execution"]["status"] == "verified"
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    # Only after inference: synthetic gold, using the existing evaluator.
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        "".join(
            dumps({"id": qid, "type": "number", "answer": 0}) + "\n" for qid in ["q_one", "q_two"]
        )
    )
    score = evaluate(gold, output / "submission.jsonl")
    assert score["correct"] == 2 and score["malformed"] == 0


def test_subset_keeps_input_order_and_rejects_unknown_before_writing(synthetic_split, tmp_path):
    output = tmp_path / "subset"
    manifest = run_inference(synthetic_split, output, question_ids=["q_two"], planner=StubPlanner())
    assert manifest["questions_written"] == 1
    assert loads((output / "submission.jsonl").read_text())["id"] == "q_two"
    with pytest.raises(ContractError):
        run_inference(
            synthetic_split, tmp_path / "bad", question_ids=["unknown"], planner=StubPlanner()
        )
    assert not (tmp_path / "bad").exists()


def test_local_loader_rejects_remote_or_missing_weights(tmp_path):
    with pytest.raises(ContractError, match="local model directory"):
        LocalModel(tmp_path / "missing")
    weights_only = tmp_path / "weights-only"
    weights_only.mkdir()
    (weights_only / "model.safetensors").write_bytes(b"synthetic")
    with pytest.raises(ContractError, match="local model directory"):
        LocalModel(weights_only)


def test_guided_schema_converts_tuple_items_without_mutating_plan_schema():
    schema = {
        "type": "object",
        "properties": {
            "pair": {"type": "array", "items": [{"const": "a"}, {"const": "b"}]},
            "list": {"type": "array", "items": {"type": "string", "maxLength": 256}},
        },
    }
    converted = guided_schema(schema)
    assert converted["properties"]["pair"] == {
        "type": "array",
        "prefixItems": [{"const": "a"}, {"const": "b"}],
        "items": False,
    }
    assert converted["properties"]["list"] == {"type": "array", "items": {"type": "string"}}
    assert isinstance(schema["properties"]["pair"]["items"], list)
    assert schema["properties"]["list"]["items"]["maxLength"] == 256


def test_plan_validator_rejects_overlong_text_that_the_grammar_no_longer_bounds():
    from src.agent.plan import validate_query_plan

    overlong = draft()
    overlong["aggregations"][0]["id"] = "x" * 257
    with pytest.raises(ContractError, match="bounded text"):
        validate_query_plan(overlong, question(), None)


def test_context_reuses_static_prefix_without_changing_authority_or_coverage(semantic_source):
    context = semantic_source(complete=False)
    before = dumps(context.semantics)
    one = build_context(question(), context)
    two = build_context(question(question="Số đơn hôm qua?", as_of_date="2026-01-02"), context)
    assert list(one)[-2:] == ["as_of_date", "question"]
    assert {k: v for k, v in one.items() if k not in {"question", "as_of_date"}} == {
        k: v for k, v in two.items() if k not in {"question", "as_of_date"}
    }
    assert one["authority"]["rulebook"]["sha256"] == context.rules.sha256
    assert one["coverage"]["available_span"] == one["coverage"]["observed_span"]
    assert all(rule["source_lines"] for rule in one["rules"])
    assert dumps(context.semantics) == before
    result = execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), context)
    assert result["answer"]["answer"] == 2
    assert result["execution"]["status"] == "verified"


def test_generation_schema_bounds_match_validator_and_preserve_unsupported_intent(semantic_source):
    context = semantic_source()
    for name in ["time_windows", "aggregations"]:
        schema = PLAN_SCHEMA["properties"][name]
        assert (schema["minItems"], schema["maxItems"]) == (1, 3)
        narrowed = generation_schema(question())["properties"][name]
        assert narrowed["minItems"] == narrowed["maxItems"] == 1
    for change in [
        lambda p: p.update(uncertainties=["x" * 257]),
        lambda p: p["aggregations"][0].update(filters=["item"] * 5),
        lambda p: p.update(time_windows=[]),
    ]:
        candidate = draft()
        change(candidate)
        with pytest.raises(ContractError):
            validate_query_plan(candidate, question(), context)


def test_relative_time_check_accepts_equivalent_bounds_and_rejects_multiple_periods(
    semantic_source,
):
    context = semantic_source()
    q = question(question="Số đơn hqua?", as_of_date="2026-01-03")
    candidate = draft("order_count", {"kind": "day", "date": "2026-01-02"})
    plan = validate_query_plan(candidate, q, context)
    assert "as_of" in plan["rule_refs"]
    with pytest.raises(ContractError, match="question_time_mismatch"):
        validate_query_plan(draft("order_count", {"kind": "today"}), q, context)
    with pytest.raises(ContractError, match="multiple_windows"):
        validate_query_plan(
            draft(), question(question="Doanh thu tháng này và tháng trước?"), context
        )


def test_relative_generation_hint_keeps_same_contract_and_scope_guards_reject_dropped_filters(
    semantic_source,
):
    schema = generation_schema(question(question="Cho tôi biết số đơn hqua"))
    expression = schema["properties"]["time_windows"]["items"]["properties"]["expression"]
    assert expression["properties"]["kind"] == {"const": "yesterday"}
    base = PLAN_SCHEMA["properties"]["time_windows"]["items"]["properties"]["expression"]
    assert "anyOf" in base
    assert generation_schema(question(question="Tháng 1/2026 có bao nhiêu đơn?"))["properties"][
        "time_windows"
    ]["items"]["properties"]["expression"]["properties"]["kind"] == {"const": "month"}
    context = semantic_source()
    for text in [
        "Doanh thu riêng sản phẩm A tháng này?",
        "Doanh thu của A tháng này?",
        "Số đơn của khách X hôm qua?",
        "Doanh thu không tính đơn đang giao tháng này?",
    ]:
        with pytest.raises(ContractError, match="question_.*unsupported"):
            validate_query_plan(draft(), question(question=text), context)


@pytest.mark.parametrize(
    "text,metric,kind",
    [
        ("Cho mình biết doanh thu tháng hiện tại được không?", "revenue", "current_month"),
        ("Tháng vừa rồi shop có bao nhiêu đơn?", "order_count", "previous_month"),
        ("Hnay bán được bao nhiêu tiền?", "revenue", "today"),
        ("Tuần hiện tại đếm đơn giúp mình", "order_count", "current_week"),
        ("Bữa nay có mấy bill?", "order_count", "today"),
        ("Tính riêng ngày hôm nay, cửa hàng có bao nhiêu đơn hàng?", "order_count", "today"),
    ],
)
def test_literal_metric_and_time_guards_accept_supported_variants(
    semantic_source, text, metric, kind
):
    context = semantic_source(complete=False, rows=[["1", "01/01/2026", "Xong", "100", "A"]])
    q = question(question=text)
    result = execute_plan(draft(metric, {"kind": kind}), q, context)
    assert result["answer"]["type"] in {"number", "unanswerable"}
    wrong_metric = "order_count" if metric == "revenue" else "revenue"
    with pytest.raises(ContractError, match="question_metric_mismatch"):
        validate_query_plan(draft(wrong_metric, {"kind": kind}), q, context)


@pytest.mark.parametrize(
    "text",
    [
        "Doanh thu trừ đơn đang giao tháng này?",
        "Số đơn chuyển khoản hôm nay?",
        "Doanh thu và số đơn tuần này?",
        "Doanh thu hôm nay không tính hàng A?",
    ],
)
def test_omitted_conditions_or_second_metric_are_rejected(semantic_source, text):
    context = semantic_source()
    with pytest.raises(ContractError, match="question_"):
        validate_query_plan(draft(), question(question=text), context)


@pytest.mark.parametrize(
    "text,expected,wrong",
    [
        (
            "Số đơn quý 2/2026?",
            {"kind": "quarter", "year": 2026, "quarter": 2},
            {"kind": "month", "year": 2026, "month": 2},
        ),
        (
            "Doanh thu tháng 7 năm 2026?",
            {"kind": "month", "year": 2026, "month": 7},
            {"kind": "month", "year": 2026, "month": 1},
        ),
        (
            "Doanh thu từ ngày 01/12/2025 đến ngày 02/01/2026?",
            {"kind": "range", "start": "2025-12-01", "end": "2026-01-02"},
            {"kind": "day", "date": "2026-01-02"},
        ),
    ],
)
def test_explicit_period_guard_rejects_wrong_bounds_before_preflight(
    semantic_source, text, expected, wrong
):
    context = semantic_source()
    q = question(question=text)
    metric = "order_count" if "Số đơn" in text else "revenue"
    validate_query_plan(draft(metric, expected), q, context)
    with pytest.raises(ContractError, match="question_time_mismatch"):
        validate_query_plan(draft(metric, wrong), q, context)
    expression = generation_schema(q)["properties"]["time_windows"]["items"]["properties"][
        "expression"
    ]
    assert expression["properties"] == {k: {"const": v} for k, v in expected.items()}


@pytest.mark.parametrize("bad_status", [False, True])
def test_invoice_headers_count_without_fabricating_sales_lines(
    synthetic_split, tmp_path, rulebook_text, bad_status
):
    from src.data.catalog import prepare_business
    from src.data.normalization import prepare_facts
    from src.data.split import load_split
    from src.semantics.answerability import prepare_semantics

    business = synthetic_split / "data/shop_a"
    (
        business / "README.md"
    ).write_text("""Mỗi dòng một đơn hàng (hoá đơn), không có chi tiết mặt hàng.
- **order_id**: mã đơn
- **date**: ngày bán
- **status**: trạng thái đơn
Dữ liệu có đến hết 03/01/2026.
""")
    (synthetic_split / "rulebook.md").write_text(rulebook_text)
    with (business / "sales.csv").open("w", newline="") as handle:
        csv.writer(handle).writerows(
            [
                ["order_id", "date", "status", "note"],
                ["001", "01/01/2026", "Xong", ""],
                ["001", "01/01/2026", "Huỷ" if bad_status else "Xong", ""],
                ["002", "03/01/2026", "Đang giao", ""],
                ["003", "03/01/2026", "Đổi trả", ""],
                ["004", "03/01/2026", "khách hủy", ""],
                ["005", "03/01/2026", "Hoàn trả", ""],
                ["006", "03/01/2026", "Chờ giao", ""],
                ["", "", "", "Cộng ngày 03/01/2026"],
            ]
        )
    context = prepare_business(load_split(synthetic_split), "shop_a")
    prepared = prepare_facts(context, tmp_path / "invoice")
    prepare_semantics(context)
    result = execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), context)
    assert prepared.summary["shape"] == "csv_order_headers"
    assert prepared.summary["counts"]["order_lines"] == 0
    assert prepared.summary["counts"]["lineage"] == 9
    assert prepared.summary["row_accounting"]["summary"] == 1
    if bad_status:
        assert result["answer"]["type"] == "abstain"
    else:
        assert result["answer"]["answer"] == 3
        assert prepared.summary["issue_counts"] == {}
        delta = execute_plan(
            difference_draft("order_count"), difference_question("order_count"), context
        )
        assert delta["answer"]["answer"] == 2
        assert all(a["lines_selected"] == 0 for a in delta["execution"]["aggregations"].values())
    revenue = execute_plan(draft(), question(), context)
    assert revenue["answer"]["type"] == "abstain"
    assert revenue["execution"]["reason"] == "required_facts_unsupported"


def test_native_worker_timeout_terminates_process():
    model = LocalModel.__new__(LocalModel)
    mp = multiprocessing.get_context("spawn")
    model.pipe, child = mp.Pipe()
    model.process = mp.Process(target=time.sleep, args=(30,))
    model.process.start()
    started = time.monotonic()
    try:
        with pytest.raises(ContractError, match="timeout"):
            model._receive(0.05)
        assert not model.process.is_alive()
        assert time.monotonic() - started < 3
    finally:
        child.close()
        model.close()


def difference_draft(metric="revenue", current=None, baseline=None):
    candidate = draft(metric, current or {"kind": "today"})
    candidate["time_windows"].append(
        {"id": "baseline_period", "expression": baseline or {"kind": "yesterday"}}
    )
    other = deepcopy(candidate["aggregations"][0])
    other.update(id="baseline_total", window_ref="baseline_period")
    candidate["aggregations"].append(other)
    candidate["derived_measures"] = [
        {
            "id": "delta",
            "operator": "difference",
            "current_ref": "total",
            "baseline_ref": "baseline_total",
        }
    ]
    candidate["output"]["result_refs"] = ["delta"]
    return candidate


def difference_question(metric="revenue", **changes):
    name = "Doanh thu" if metric == "revenue" else "Số đơn"
    return question(question=f"{name} hôm nay so với hôm qua chênh lệch bao nhiêu?", **changes)


@pytest.mark.parametrize(
    "current,baseline,expected",
    [
        ("100.50", "99.25", "1"),
        ("123456789012345678901234567890.51", "0.02", "123456789012345678901234567890"),
        ("0.02", "123456789012345678901234567890.51", "-123456789012345678901234567890"),
        ("0.50", "0.50", "0"),
    ],
)
def test_difference_uses_exact_operands_and_rounds_only_final_money(
    semantic_source, current, baseline, expected
):
    context = semantic_source(
        rows=[
            ["1", "03/01/2026", "Xong", current, "A"],
            ["2", "02/01/2026", "Đang giao", baseline, "B"],
            ["3", "03/01/2026", "Huỷ", "9999", "C"],
            ["4", "02/01/2026", "Đổi trả", "9999", "D"],
        ]
    )
    before = context.prepared.path.read_bytes()
    result = execute_plan(difference_draft(), difference_question(), context)
    assert result["answer"]["answer"] == Decimal(expected)
    assert result["execution"]["aggregations"]["total"]["exact_value"] == Decimal(current)
    assert result["execution"]["aggregations"]["total"]["rounded_value"] is None
    assert len(result["plan"]["preflight"]) == 2
    assert result["plan"]["derived_measures"][0]["unit"] == "VND"
    assert "difference" in result["plan"]["rule_refs"]
    assert context.prepared.path.read_bytes() == before


def test_difference_zero_negative_count_and_reference_order(semantic_source):
    context = semantic_source(
        rows=[
            ["1", "02/01/2026", "Xong", "100", "A"],
            ["1", "02/01/2026", "Xong", "100", "A"],
            ["2", "02/01/2026", "Đang giao", "200", "B"],
        ]
    )
    candidate = difference_draft("order_count")
    candidate["aggregations"].reverse()
    candidate["time_windows"].reverse()
    result = execute_plan(candidate, difference_question("order_count"), context)
    assert result["answer"]["answer"] == -2
    assert result["execution"]["unit"] == "order"
    q = question(question="So với hôm qua, số đơn hôm nay chênh lệch bao nhiêu?")
    assert execute_plan(candidate, q, context)["answer"]["answer"] == -2


def test_difference_partial_and_outside_windows_preflight_independently(semantic_source):
    context = semantic_source()
    current = {"kind": "range", "start": "2026-01-01", "end": "2026-01-05"}
    baseline = {"kind": "range", "start": "2025-12-25", "end": "2026-01-01"}
    q = question(
        question="Số đơn từ 01/01/2026 đến 05/01/2026 so với "
        "từ 25/12/2025 đến 01/01/2026 chênh lệch bao nhiêu?"
    )
    result = execute_plan(difference_draft("order_count", current, baseline), q, context)
    assert result["answer"]["answer"] == 1
    windows = result["plan"]["time_windows"]
    assert [w["coverage_status"] for w in windows] == ["partial", "partial"]
    assert windows[0]["requested_end"] == "2026-01-05"
    assert windows[0]["effective_window"]["end"] == "2026-01-03"
    assert windows[1]["requested_start"] == "2025-12-25"
    assert windows[1]["effective_window"]["start"] == "2026-01-01"
    q = difference_question(as_of_date="2026-01-01")
    result = execute_plan(difference_draft(), q, context)
    assert result["answer"]["type"] == "unanswerable"
    assert result["execution"]["status"] == "not_performed"
    assert [w["coverage_status"] for w in result["plan"]["time_windows"]] == ["full", "outside"]


def test_difference_unknown_operand_blocks_all_queries_even_with_other_outside(
    semantic_source, monkeypatch
):
    context = semantic_source(rows=[], complete=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("numeric query before all dependencies are eligible")

    monkeypatch.setattr("src.agent.execution._compute", forbidden)
    result = execute_plan(difference_draft(), difference_question(as_of_date="2026-01-04"), context)
    assert result["answer"]["type"] == "abstain"
    assert [w["coverage_status"] for w in result["plan"]["time_windows"]] == ["outside", "unknown"]
    assert len(result["plan"]["preflight"]) == 2


def test_difference_month_to_date_uses_runtime_comparison_and_ignores_future(semantic_source):
    context = semantic_source(
        coverage_end="31/03/2026",
        rows=[
            ["1", "05/03/2026", "Xong", "100", "A"],
            ["2", "05/02/2026", "Xong", "50", "B"],
            ["3", "20/02/2026", "Xong", "9999", "C"],
            ["4", "20/03/2026", "Xong", "9999", "D"],
        ],
    )
    candidate = difference_draft(
        current={"kind": "current_month"}, baseline={"kind": "previous_mtd"}
    )
    q = question(
        question="Doanh thu tháng này so với tháng trước chênh lệch bao nhiêu?",
        as_of_date="2026-03-05",
    )
    result = execute_plan(candidate, q, context)
    assert result["answer"]["answer"] == 50
    assert result["plan"]["time_windows"][1]["requested_end"] == "2026-02-05"
    candidate["time_windows"][1]["expression"] = {"kind": "previous_month"}
    with pytest.raises(ContractError, match="question_time_mismatch"):
        validate_query_plan(candidate, q, context)
    candidate["time_windows"][1]["expression"] = {"kind": "previous_mtd"}
    q = replace(q, as_of_date="2026-03-31")
    assert (
        validate_query_plan(candidate, q, context)["time_windows"][1]["requested_end"]
        == "2026-02-28"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["derived_measures"][0].update(
            current_ref="baseline_total", baseline_ref="total"
        ),
        lambda p: p["derived_measures"][0].update(current_ref="delta"),
        lambda p: p["derived_measures"][0].update(baseline_ref="missing"),
        lambda p: p["derived_measures"][0].update(baseline_ref="total"),
        lambda p: p["derived_measures"][0].update(operator="percent_change"),
        lambda p: p["derived_measures"][0].update(id="total"),
        lambda p: p["derived_measures"][0].update(answer=99),
        lambda p: p["aggregations"][1].update(metric_id="order_count"),
        lambda p: p["aggregations"][1].update(unit="USD"),
        lambda p: p["aggregations"][1].update(filters=["status=shipping"]),
        lambda p: p["aggregations"][1].update(window_ref="period"),
        lambda p: p["output"].update(result_refs=["total"]),
    ],
)
def test_difference_rejects_reversal_cycles_mixed_units_scope_and_unbound_refs(
    semantic_source, change
):
    context = semantic_source()
    candidate = difference_draft()
    change(candidate)
    with pytest.raises(ContractError):
        validate_query_plan(candidate, difference_question(), context)


@pytest.mark.parametrize(
    "text",
    [
        "Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm?",
        "Doanh thu hôm nay so với hôm qua chênh bao nhiêu %?",
        "Doanh thu hôm nay so với hôm qua tăng trưởng bao nhiêu?",
        "Doanh thu riêng sản phẩm A hôm nay so với hôm qua chênh lệch bao nhiêu?",
        "Doanh thu không tính đơn đang giao hôm nay so với hôm qua chênh lệch bao nhiêu?",
        "Doanh thu hôm nay và hôm qua?",
        "Doanh thu hôm nay so với hôm qua?",
        "Doanh thu tháng 2/2026 so với tháng trước chênh bao nhiêu?",
        "Doanh thu hôm nay so với hôm qua chênh lệch tuyệt đối bao nhiêu?",
        "Tỉ trọng doanh thu hôm nay bao nhiêu?",
        "Bình quân doanh thu hôm nay bao nhiêu?",
    ],
)
def test_difference_or_comparison_cannot_be_repaired_into_single_total(semantic_source, text):
    context = semantic_source()
    for candidate in (draft(), difference_draft()):
        model = ScriptedModel(dumps(candidate), dumps(draft()))
        result = LocalPlanner(model).decide(question(question=text), context=context)
        assert result["answer"]["type"] == "abstain"
        assert model.calls == 1


def test_difference_requires_resolved_runtime_rule_and_final_tie_abstains(
    semantic_source, rulebook_text
):
    context = semantic_source(
        rows=[
            ["1", "03/01/2026", "Xong", "100.75", "A"],
            ["2", "02/01/2026", "Xong", "100.25", "B"],
        ]
    )
    result = execute_plan(difference_draft(), difference_question(), context)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "rounding_tie_unspecified"
    context = semantic_source(rules=rulebook_text.replace("kỳ hỏi − kỳ so", "kỳ so − kỳ hỏi"))
    with pytest.raises(ContractError):
        validate_query_plan(difference_draft(), difference_question(), context)
    assert (
        execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), context)[
            "answer"
        ]["type"]
        == "number"
    )


def test_difference_generation_preserves_prior_mtd_before_coverage_clipping(semantic_source):
    context = semantic_source(coverage_end="31/03/2026")
    q = question(
        question="Doanh thu tháng này so với tháng trước chênh lệch bao nhiêu?",
        as_of_date="2026-03-05",
    )
    candidate = difference_draft(
        current={"kind": "current_month"}, baseline={"kind": "previous_mtd"}
    )
    schema = generation_schema(q)
    _schema(candidate, schema)
    candidate["time_windows"][1]["expression"] = {"kind": "previous_month"}
    with pytest.raises(ContractError):
        _schema(candidate, schema)
    with pytest.raises(ContractError, match="question_time_mismatch"):
        validate_query_plan(candidate, q, context)
    # Narrowing generation never authorizes reversed operand refs.
    candidate["time_windows"][1]["expression"] = {"kind": "previous_mtd"}
    candidate["derived_measures"][0].update(current_ref="baseline_total", baseline_ref="total")
    with pytest.raises(ContractError):
        _schema(candidate, schema)
    with pytest.raises(ContractError, match="question_time_mismatch"):
        validate_query_plan(candidate, q, context)


def growth_draft(current=None, baseline=None):
    candidate = difference_draft(current=current, baseline=baseline)
    candidate["derived_measures"][0]["operator"] = "percent_change"
    candidate["output"]["expected_type"] = "percent"
    return candidate


def growth_question(**changes):
    return question(
        question="Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm?", **changes
    )


@pytest.mark.parametrize(
    "current,baseline,expected",
    [
        ("100.50", "99.25", "1.26"),
        ("50.25", "100.50", "-50.00"),
        ("0", "100", "-100.00"),
        ("100.50", "100.50", "0.00"),
        ("123456789012345678901234567890.25", "123456789012345678901234567890.24", "0.00"),
    ],
)
def test_growth_exact_rational_round_once_and_signed_result(
    semantic_source, current, baseline, expected
):
    context = semantic_source(
        rows=[
            ["1", "03/01/2026", "Xong", current, "A"],
            ["2", "02/01/2026", "Xong", baseline, "B"],
        ]
    )
    result = execute_plan(growth_draft(), growth_question(), context)
    assert result["answer"] == {"id": "synthetic", "type": "percent", "answer": Decimal(expected)}
    assert result["execution"]["operator"] == "percent_change"
    assert result["execution"]["baseline_value"] == Decimal(baseline)
    assert result["execution"]["aggregations"]["total"]["rounded_value"] is None
    assert result["plan"]["derived_measures"][0]["unit"] == "percent"
    assert result["plan"]["output"]["rounding"]["decimal_places"] == 2


def test_growth_zero_baseline_unknown_and_outside_are_distinct(semantic_source):
    context = semantic_source(rows=[["1", "03/01/2026", "Xong", "100", "A"]])
    result = execute_plan(growth_draft(), growth_question(), context)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "percent_change_nonpositive_baseline_undefined"
    context = semantic_source(rows=[], complete=False)
    result = execute_plan(growth_draft(), growth_question(), context)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "coverage_unknown"
    context = semantic_source()
    result = execute_plan(growth_draft(), growth_question(as_of_date="2026-01-01"), context)
    assert result["answer"]["type"] == "unanswerable"
    assert result["execution"]["status"] == "not_performed"


def test_growth_rounding_half_tie_abstains_and_near_tie_is_exact():
    from src.agent.execution import _percent_change

    policy = {"decimal_places": 2}
    with pytest.raises(ContractError, match="tie"):
        _percent_change(Decimal("1"), Decimal("20000"), policy)
    with pytest.raises(ContractError, match="nonpositive"):
        _percent_change(Decimal("1"), Decimal("-1"), policy)
    # Difference is beyond ordinary Decimal precision, but on opposite sides of .005%.
    assert (
        _percent_change(Decimal("1"), Decimal("20000.000000000000000000000001"), policy)[
            "rounded_value"
        ]
        == 0
    )
    assert _percent_change(Decimal("1"), Decimal("19999.999999999999999999999999"), policy)[
        "rounded_value"
    ] == Decimal(".01")


@pytest.mark.parametrize(
    "text,year,month",
    [
        ("Doanh thu tháng 1/2026 so với tháng trước tăng bao nhiêu phần trăm?", 2025, 12),
        ("So với tháng liền trước, doanh thu tháng 5 năm 2025 thay đổi bao nhiêu %?", 2025, 4),
        (
            "Cho mình hỏi so với tháng trước đó, doanh thu tháng 2/2026 "
            "tăng hay giảm mấy phần trăm?",
            2026,
            1,
        ),
        ("so vs th truoc do, dthu thang 10/2025 tang hay giam may %", 2025, 9),
    ],
)
def test_growth_prior_month_is_anchored_to_asked_month_not_asof(semantic_source, text, year, month):
    context = semantic_source()
    q = question(question=text, as_of_date="2026-06-02")
    from src.agent.plan import _difference_windows

    expressions = _difference_windows(q, percent_change=True)
    assert expressions[1] == {"kind": "month", "year": year, "month": month}
    candidate = growth_draft(*expressions)
    _schema(candidate, generation_schema(q))
    validate_query_plan(candidate, q, context)
    candidate["derived_measures"][0].update(current_ref="baseline_total", baseline_ref="total")
    with pytest.raises(ContractError, match="time_mismatch"):
        validate_query_plan(candidate, q, context)


@pytest.mark.parametrize(
    "text",
    [
        "Tỉ trọng doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm?",
        "Doanh thu riêng sản phẩm A hôm nay so với hôm qua tăng bao nhiêu phần trăm?",
        "Doanh thu không tính đơn đang giao hôm nay so với hôm qua tăng bao nhiêu phần trăm?",
        "Doanh thu hôm nay so với hôm qua chênh lệch bao nhiêu?",
    ],
)
def test_growth_never_erases_filter_ratio_or_difference_semantics(semantic_source, text):
    with pytest.raises(ContractError):
        validate_query_plan(growth_draft(), question(question=text), semantic_source())


def test_growth_requires_its_runtime_rule_and_supports_count(semantic_source, rulebook_text):
    context = semantic_source(
        rules=rulebook_text.replace("so với kỳ trước; giảm thì ra số âm", "không có quy định")
    )
    with pytest.raises(ContractError):
        validate_query_plan(growth_draft(), growth_question(), context)
    context = semantic_source()
    p = growth_draft()
    for a in p["aggregations"]:
        a["metric_id"] = "order_count"
    result = execute_plan(
        p, question(question="Số đơn hôm nay so với hôm qua tăng bao nhiêu phần trăm?"), context
    )
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "percent_change_nonpositive_baseline_undefined"


def test_recorded_active_and_return_aliases_follow_metric_exclusion_policy(semantic_source):
    context = semantic_source(
        rows=[
            ["1", "03/01/2026", "Đang giao dịch", "100", "A"],
            ["2", "03/01/2026", "Đã hoàn hàng", "9999", "B"],
        ]
    )
    assert execute_plan(draft(), question(), context)["answer"]["answer"] == 100
    assert (
        execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), context)[
            "answer"
        ]["answer"]
        == 1
    )
    rows = list(context.prepared.iter_records("order_lines"))
    assert any("Đang giao dịch" in dumps(row["source_values"]) for row in rows)


def period_draft(operator, metrics, expression=None, filters=None):
    plan = draft(metrics[0], expression)
    plan["aggregations"][0]["id"] = "numerator"
    plan["aggregations"][0]["filters"] = filters or []
    baseline = deepcopy(plan["aggregations"][0])
    baseline.update(
        id="denominator",
        metric_id=metrics[1],
        filters=filters or [] if operator == "average_daily" else [],
    )
    plan["aggregations"].append(baseline)
    plan["derived_measures"] = [
        {
            "id": "result",
            "operator": operator,
            "current_ref": "numerator",
            "baseline_ref": "denominator",
        }
    ]
    plan["output"] = {
        "expected_type": "percent" if operator in {"share", "cancellation_rate"} else "number",
        "result_refs": ["result"],
    }
    return plan


def test_cancel_ratio_uses_all_statuses_and_distinct_orders(semantic_source):
    context = semantic_source(
        rows=[
            ["1", "01/01/2026", "Huỷ", "90", "A"],
            ["1", "01/01/2026", "Huỷ", "90", "A"],
            ["2", "01/01/2026", "Xong", "100", "B"],
            ["3", "01/01/2026", "Đổi trả", "20", "C"],
            ["4", "01/01/2026", "Đang giao", "30", "D"],
            ["5", "02/01/2026", "Xong", "200", "E"],
        ]
    )
    p = period_draft("cancellation_rate", ["cancelled_order_count", "placed_order_count"])
    result = execute_plan(
        p, question(question="Tháng này tỉ lệ đơn bị huỷ là bao nhiêu phần trăm?"), context
    )
    assert result["answer"]["answer"] == 20
    assert result["execution"]["denominator_value"] == 5
    assert result["execution"]["numerator_value"] == 1
    for agg in result["execution"]["aggregations"].values():
        assert agg["status"] == "verified"


def test_aov_uses_unrounded_revenue_and_rounds_half_up_once(semantic_source):
    context = semantic_source(
        rows=[
            ["1", "01/01/2026", "Xong", "100", "A"],
            ["1", "01/01/2026", "Xong", "1", "B"],
            ["2", "01/01/2026", "Đang giao", "100", "C"],
            ["3", "03/01/2026", "Huỷ", "10000", "D"],
        ]
    )
    p = period_draft("average_order_value", ["revenue", "order_count"])
    result = execute_plan(
        p, question(question="Giá trị trung bình một đơn tháng này là bao nhiêu?"), context
    )
    assert result["answer"]["answer"] == 101
    assert result["execution"]["numerator_value"] == 201
    assert result["execution"]["denominator_value"] == 2


@pytest.mark.parametrize(
    "operator,metrics,text",
    [
        (
            "average_order_value",
            ["revenue", "order_count"],
            "Giá trị trung bình một đơn tháng này?",
        ),
        (
            "cancellation_rate",
            ["cancelled_order_count", "placed_order_count"],
            "Tỉ lệ đơn huỷ tháng này là bao nhiêu %?",
        ),
    ],
)
def test_undefined_zero_and_unknown_coverage_are_not_fabricated(
    semantic_source, operator, metrics, text
):
    p = period_draft(operator, metrics, {"kind": "day", "date": "2026-01-02"})
    q = question(question=text.replace("tháng này", "ngày 02/01/2026"))
    result = execute_plan(p, q, semantic_source())
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "nonpositive_denominator_undefined"
    context = semantic_source(rows=[], complete=False)
    result = execute_plan(p, q, context)
    assert result["answer"]["type"] == "abstain"
    assert all(c["reason"] == "coverage_unknown" for c in result["plan"]["preflight"])


def test_weekday_average_counts_calendar_days_including_days_without_orders(semantic_source):
    context = semantic_source(
        coverage_end="31/01/2026",
        rows=[
            ["1", "05/01/2026", "Xong", "100", "A"],
            ["2", "12/01/2026", "Xong", "100", "B"],
            ["3", "19/01/2026", "Đang giao", "200", "C"],
            ["4", "06/01/2026", "Xong", "9999", "D"],
        ],
    )
    p = period_draft(
        "average_daily",
        ["revenue", "calendar_day_count"],
        {"kind": "month", "year": 2026, "month": 1},
        ["weekday=0"],
    )
    result = execute_plan(
        p, question(question="Doanh thu trung bình các ngày thứ Hai trong tháng 1/2026?"), context
    )
    assert result["answer"]["answer"] == 100
    assert result["execution"]["denominator_value"] == 4
    assert result["execution"]["numerator_value"] == 400


def test_share_denominator_keeps_other_groups_and_walkins(semantic_source):
    context = semantic_source(
        extra_columns=["group", "customer"],
        readme_extra="- **group**: customer group\n- **customer**: customer name\n",
        rows=[
            ["1", "01/01/2026", "Xong", "20", "A", "VIP", "Lan"],
            ["2", "01/01/2026", "Xong", "50", "B", "VIP", ""],
            ["3", "01/01/2026", "Đang giao", "30", "C", "Other", "Anh"],
        ],
    )
    p = period_draft("share", ["revenue", "revenue"], filters=["customer_group=vip"])
    result = execute_plan(
        p,
        question(
            question="Tỉ trọng doanh thu của khách nhóm VIP trong tổng doanh thu tháng này "
            "là bao nhiêu %?"
        ),
        context,
    )
    assert result["answer"]["answer"] == 20
    assert result["execution"]["denominator_value"] == 100
    assert result["execution"]["numerator_value"] == 20


@pytest.mark.parametrize(
    "change", ["denominator_filter", "reverse", "metric", "window", "unit", "operator"]
)
def test_period_measure_rejects_changed_dependencies(semantic_source, change):
    p = period_draft("average_order_value", ["revenue", "order_count"])
    if change == "denominator_filter":
        p["aggregations"][1]["filters"] = ["weekday=0"]
    if change == "reverse":
        p["derived_measures"][0].update(current_ref="denominator", baseline_ref="numerator")
    if change == "metric":
        p["aggregations"][1]["metric_id"] = "placed_order_count"
    if change == "window":
        p["aggregations"][1]["window_ref"] = "missing"
    if change == "unit":
        p["aggregations"][0]["unit"] = "order"
    if change == "operator":
        p["derived_measures"][0]["operator"] = "cancellation_rate"
    with pytest.raises(ContractError):
        execute_plan(
            p, question(question="Doanh thu trung bình mỗi đơn tháng này?"), semantic_source()
        )


def test_period_measure_does_not_drop_other_scope_conditions(semantic_source):
    p = period_draft("cancellation_rate", ["cancelled_order_count", "placed_order_count"])
    with pytest.raises(ContractError):
        execute_plan(
            p,
            question(question="Tỉ lệ đơn huỷ riêng khách Lan tháng này bao nhiêu %?"),
            semantic_source(),
        )
    p = period_draft("share", ["revenue", "revenue"], filters=["item_group=tra sua"])
    result = execute_plan(
        p,
        question(question="Tỉ trọng của nhóm trà sữa trong doanh thu tháng này là bao nhiêu %?"),
        semantic_source(),
    )
    assert result["answer"]["type"] == "abstain"
    assert result["plan"]["preflight"][0]["reason"] == "group_mapping_unsupported"


@pytest.mark.parametrize("partial,expected_denominator,expected", [(False, 31, 10), (True, 3, 100)])
def test_daily_average_partial_coverage_uses_effective_calendar_span(
    semantic_source, partial, expected_denominator, expected
):
    context = semantic_source(
        coverage_end="03/01/2026" if partial else "31/01/2026",
        rows=[["1", "01/01/2026", "Xong", "300" if partial else "310", "A"]],
    )
    p = period_draft(
        "average_daily",
        ["revenue", "calendar_day_count"],
        {"kind": "month", "year": 2026, "month": 1},
    )
    result = execute_plan(
        p, question(question="Doanh thu trung bình mỗi ngày tháng 1/2026?"), context
    )
    assert result["execution"]["denominator_value"] == expected_denominator
    assert result["answer"]["answer"] == expected
    assert result["plan"]["time_windows"][0]["coverage_status"] == (
        "partial" if partial else "full"
    )


def test_item_share_and_group_collision_fail_closed(semantic_source):
    context = semantic_source(
        extra_columns=["group"],
        readme_extra="- **group**: product group\n",
        rows=[
            ["1", "01/01/2026", "Xong", "20", "A", "Trà sữa"],
            ["1", "01/01/2026", "Xong", "30", "B", "Khác"],
            ["2", "02/01/2026", "Đang giao", "50", "C", "Khác"],
        ],
    )
    p = period_draft("share", ["revenue", "revenue"], filters=["item_group=tra sua"])
    q = question(question="Tỉ trọng của nhóm trà sữa trong doanh thu tháng này bao nhiêu %?")
    result = execute_plan(p, q, context)
    assert result["answer"]["answer"] == 20
    assert result["execution"]["denominator_value"] == 100
    ambiguous = semantic_source(
        extra_columns=["group"],
        readme_extra="- **group**: product group\n",
        rows=[
            ["1", "01/01/2026", "Xong", "20", "A", "Trà sữa"],
            ["2", "02/01/2026", "Xong", "30", "B", "Tra sua"],
        ],
    )
    result = execute_plan(p, q, ambiguous)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "group_name_ambiguous"


def test_count_ratio_not_blocked_by_irrelevant_amount_error(semantic_source):
    context = semantic_source(
        rows=[["1", "01/01/2026", "Huỷ", "bad", "A"], ["2", "02/01/2026", "Xong", "100", "B"]]
    )
    p = period_draft("cancellation_rate", ["cancelled_order_count", "placed_order_count"])
    assert (
        execute_plan(p, question(question="Tỉ lệ đơn huỷ tháng này bao nhiêu %?"), context)[
            "answer"
        ]["answer"]
        == 50
    )
    p = period_draft("average_order_value", ["revenue", "order_count"])
    result = execute_plan(p, question(question="Giá trị trung bình một đơn tháng này?"), context)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["status"] == "not_performed"


@pytest.mark.parametrize(
    "text",
    [
        "Doanh thu tuần này so với tuần trước chênh lệch bao nhiêu?",
        "Doanh thu 7 ngày gần nhất so với 14 ngày gần nhất tăng bao nhiêu %?",
        "Doanh thu tháng 1/2026 so với tháng trước chênh lệch bao nhiêu?",
    ],
)
def test_comparison_generation_preserves_two_temporal_dependencies(text):
    schema = generation_schema(question(question=text))
    assert schema["properties"]["time_windows"]["minItems"] == 2
    windows = schema["properties"]["time_windows"]["items"]
    assert len(windows) == 2
    assert windows[0]["properties"]["expression"] != {}


def test_period_measure_preserves_model_dependency_ids(semantic_source):
    p = period_draft("average_order_value", ["revenue", "order_count"])
    p["time_windows"][0]["id"] = "model_window"
    for a in p["aggregations"]:
        a["window_ref"] = "model_window"
    result = execute_plan(
        p, question(question="Giá trị trung bình một đơn tháng này?"), semantic_source()
    )
    assert result["plan"]["time_windows"][0]["id"] == "model_window"
    assert result["answer"]["answer"] == 75


def test_generation_binds_aov_roles_and_rejects_duplicate_or_wrong_operands():
    q = question(question="Giá trị trung bình một đơn tháng này?")
    p = difference_draft(current={"kind": "current_month"})
    p["time_windows"] = p["time_windows"][:1]
    p["aggregations"][1].update(metric_id="order_count", window_ref="period")
    p["derived_measures"][0]["operator"] = "average_order_value"
    schema = generation_schema(q)
    _schema(p, schema)
    p["aggregations"][1]["metric_id"] = "revenue"
    with pytest.raises(ContractError):
        _schema(p, schema)
    p["aggregations"][1].update(metric_id="order_count", id="total")
    with pytest.raises(ContractError):
        _schema(p, schema)


def test_generation_preserves_distinct_comparison_periods_before_model_output():
    q = question(question="Doanh thu tháng 12/2025 so với tháng trước tăng bao nhiêu %?")
    p = growth_draft(
        {"kind": "month", "year": 2025, "month": 12}, {"kind": "month", "year": 2025, "month": 11}
    )
    schema = generation_schema(q)
    _schema(p, schema)
    p["time_windows"][1]["expression"] = deepcopy(p["time_windows"][0]["expression"])
    with pytest.raises(ContractError):
        _schema(p, schema)


@pytest.mark.parametrize(
    "text,intent",
    [
        ("Ngày 03/01/2026 bán Trà đào với đơn giá bao nhiêu một ly?", "unit_price"),
        ("gia ban mot chai nuoc suoi ngay 02/01/2026 la bn", "unit_price"),
        ("Tháng này quán bán được bao nhiêu ly size M, cộng mọi món?", "size_quantity"),
        ("thang 12/2025 xe giao tong cong may dot hang", "delivery_batches"),
    ],
)
def test_intents_without_operator_abstain_before_calling_the_model(semantic_source, text, intent):
    model = ScriptedModel()
    result = LocalPlanner(model).decide(question(question=text), context=semantic_source())
    assert result["answer"] == {"id": "synthetic", "type": "abstain"}
    assert result["plan"]["reason"] == f"guard:unsupported_intent:{intent}"
    assert model.calls == 0


@pytest.mark.parametrize(
    "text",
    ["Doanh thu tháng này?", "Giá vốn hàng bán tháng này?", "Số đơn đột xuất tăng tháng này?"],
)
def test_unsupported_intent_guard_ignores_nearby_supported_wording(text):
    from src.agent.planner import unsupported_intent

    assert unsupported_intent(question(question=text)) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Doanh thu T5/2025?", {"kind": "month", "year": 2025, "month": 5}),
        ("Số đơn th 11/25?", {"kind": "month", "year": 2025, "month": 11}),
        ("Doanh thu 5-2026 được bao nhiêu?", {"kind": "month", "year": 2026, "month": 5}),
        ("07/2025 doanh thu bao nhiêu?", {"kind": "month", "year": 2025, "month": 7}),
        ("10/25 doanh thu bao nhiêu?", {"kind": "month", "year": 2025, "month": 10}),
        ("Số đơn quý 2/26?", {"kind": "quarter", "year": 2026, "quarter": 2}),
        ("so don quys 3/2025 la nhieu", {"kind": "quarter", "year": 2025, "quarter": 3}),
        ("Doanh thu bữa qua?", {"kind": "yesterday"}),
        ("Doanh thu hôm 26/12/2025?", {"kind": "day", "date": "2025-12-26"}),
        ("Doanh thu ngày 01/12/2025?", {"kind": "day", "date": "2025-12-01"}),
    ],
)
def test_abbreviated_periods_become_one_explicit_window(text, expected):
    from src.agent.plan import _literal_windows, unparsed_time

    assert _literal_windows(question(question=text)) == [expected]
    assert not unparsed_time(question(question=text))


@pytest.mark.parametrize(
    "text,unparsed",
    [
        ("Doanh thu th3 được bao nhiêu?", True),
        ("Trong tháng 6 bán được bao nhiêu tiền?", True),
        ("Doanh thu ngày 27/04 là bao nhiêu?", True),
        ("Tính từ ngày 1/1 đến hết hôm nay, doanh thu năm nay?", False),
        ("Doanh thu từ ngày 01/12/2025 đến ngày 02/01/2026?", False),
        ("Top 3 khách mua nhiều tiền nhất tháng này?", False),
    ],
)
def test_unparsed_period_is_detected_without_flagging_supported_wording(text, unparsed):
    from src.agent.plan import unparsed_time

    assert unparsed_time(question(question=text)) is unparsed


def test_unparsed_period_abstains_before_calling_the_model(semantic_source):
    model = ScriptedModel()
    result = LocalPlanner(model).decide(
        question(question="Doanh thu th3 được bao nhiêu?"), context=semantic_source()
    )
    assert result["answer"]["type"] == "abstain" and model.calls == 0
    assert result["plan"]["reason"].endswith(":unparsed_time")
