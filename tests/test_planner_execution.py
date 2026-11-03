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
from src.agent.local import LocalModel
from src.agent.plan import PLAN_SCHEMA, _schema, generation_schema, validate_query_plan
from src.agent.planner import LocalPlanner
from src.data.split import Question
from src.evaluation.scorer import evaluate
from src.jsonio import ContractError, dumps, loads
from src.runtime import run_inference


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
    assert manifest["is_mock"] is False and manifest["question_errors"] == 0
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
    manifest = run_inference(synthetic_split, output, question_ids=["q_two"])
    assert manifest["questions_written"] == 1
    assert loads((output / "submission.jsonl").read_text())["id"] == "q_two"
    with pytest.raises(ContractError):
        run_inference(synthetic_split, tmp_path / "bad", question_ids=["unknown"])
    assert not (tmp_path / "bad").exists()


def test_local_loader_rejects_remote_or_missing_weights(tmp_path):
    with pytest.raises(ContractError, match="local GGUF"):
        LocalModel(tmp_path / "missing.gguf")


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
        assert (schema["minItems"], schema["maxItems"]) == (1, 2)
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


def test_growth_requires_its_runtime_rule_and_rejects_unsupported_count(
    semantic_source, rulebook_text
):
    context = semantic_source(
        rules=rulebook_text.replace("so với kỳ trước; giảm thì ra số âm", "không có quy định")
    )
    with pytest.raises(ContractError):
        validate_query_plan(growth_draft(), growth_question(), context)
    context = semantic_source()
    p = growth_draft()
    for a in p["aggregations"]:
        a["metric_id"] = "order_count"
    with pytest.raises(ContractError, match="revenue_only"):
        validate_query_plan(
            p, question(question="Số đơn hôm nay so với hôm qua tăng bao nhiêu phần trăm?"), context
        )


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
