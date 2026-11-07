"""SQL selection must retain source-local days, exact arithmetic and fail-closed budgets."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from src.agent.execution import _compute, execute_plan
from src.agent.scanning import Scan, failure, scanning
from src.data.normalization.store import PreparedBusiness
from tests.test_planner_execution import draft, question


def test_sql_date_selection_keeps_fraction_offset_day_and_avoids_outside_decodes(semantic_source):
    c = semantic_source(
        rows=[
            ["a", "2026-01-01T00:00:00+14:00", "Xong", "123456789012345678901234567890.125", "A"],
            ["b", "2026-01-01T23:59:59.999999-12:00", "Đang giao", "0.875", "B"],
            ["c", "2026-01-02T00:00:00+14:00", "Xong", "999999", "C"],
            ["d", "2026-01-01T15:00:00+07:00", "Đổi trả", "999999", "D"],
        ]
    )
    result = execute_plan(
        draft(expression={"kind": "day", "date": "2026-01-01"}),
        question(question="Doanh thu ngày 01/01/2026?"),
        c,
    )
    assert result["execution"]["status"] == "verified", result
    assert result["answer"]["answer"] == Decimal("123456789012345678901234567891")
    assert result["execution"]["orders_selected"] == 2
    assert result["execution"]["lines_selected"] == 2
    assert result["execution"]["scan"]["rows_delivered"] == 6


def test_sql_pushdown_does_not_hide_selected_missing_amount_or_lines(semantic_source):
    c = semantic_source()
    with sqlite3.connect(c.prepared.path) as conn:
        key = conn.execute(
            "SELECT order_key FROM orders WHERE status='completed' LIMIT 1"
        ).fetchone()[0]
        conn.execute("DELETE FROM order_lines WHERE order_key=?", (key,))
    result = execute_plan(draft(), question(), c)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "selected_order_missing_lines"


def test_calendar_count_runs_after_preflight_without_fact_scan(semantic_source, monkeypatch):
    from src.semantics.answerability import preflight

    c = semantic_source()
    check = preflight(c, "calendar_day_count", {"kind": "current_month"}, as_of_date="2026-01-03")
    assert check["decision"] == "eligible_to_execute"
    monkeypatch.setattr(
        PreparedBusiness, "connect", lambda self: pytest.fail("calendar read facts")
    )
    assert _compute(c, check)["exact_value"] == 3


def test_sql_interrupt_preserves_sqlite_code_phase_and_execution_budget(tmp_path, monkeypatch):
    path = tmp_path / "facts.sqlite"
    sqlite3.connect(path).close()

    def exhausted(self):
        self.stats["budget_hit"] = "execution_15_seconds"
        return 1

    monkeypatch.setattr(Scan, "progress", exhausted)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with scanning(PreparedBusiness(path, {}), None) as scan:
            list(
                scan.rows(
                    "WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL "
                    "SELECT n+1 FROM x WHERE n<10000) SELECT n FROM x"
                )
            )
    diagnostic = failure(caught.value, "execution")
    assert diagnostic["sqlite_errorcode"] == sqlite3.SQLITE_INTERRUPT
    assert diagnostic["sqlite_errorname"] == "SQLITE_INTERRUPT"
    assert diagnostic["budget"] == "execution_15_seconds"
    assert diagnostic["error_stage"] == "sql_scan"


def test_identity_cache_is_bounded_without_truncating_catalog_and_invalidates_revision(tmp_path):
    from types import SimpleNamespace

    from src.agent.entities import entity_candidates
    from src.data.normalization.entities import SCHEMA

    path = tmp_path / "catalog.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO entities VALUES (?,'product',?,?,?, 'unit','{}')",
            ((str(i), f"sku{i}", f"Name{i}", f'["Alias{i}"]') for i in range(5000)),
        )
    context = SimpleNamespace(
        prepared=PreparedBusiness(path, {"extensions": {"counts": {"entities": 5000}}})
    )
    assert entity_candidates(context, "Alias4999")[0]["entity_key"] == "4999"
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM entities WHERE entity_key!='4999'")
    result = entity_candidates(context, "Alias4999")
    result[0]["name"] = "caller changed its copy"
    assert entity_candidates(context, "Alias4999")[0]["name"] == "Name4999"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE entities SET aliases='[\"Renamed\"]' WHERE entity_key='4999'")
    assert entity_candidates(context, "Alias4999") == []
    assert entity_candidates(context, "Renamed")[0]["entity_key"] == "4999"


def test_planning_diagnostics_record_semantic_branch_without_retry_or_scope_erasure(
    semantic_source,
):
    from src.agent.planner import LocalPlanner
    from src.jsonio import dumps
    from tests.test_planner_execution import ScriptedModel

    context = semantic_source()
    model = ScriptedModel(dumps(draft()))
    result = LocalPlanner(model).decide(
        question(question="Doanh thu tháng này không tính đơn trả?"), context=context
    )
    attempt = result["plan"]["attempts"][0]
    assert result["answer"]["type"] == "abstain"
    assert model.calls == 1
    assert attempt["error"] == "question_scope_or_operator_unsupported"
    assert attempt["error_stage"] == "semantic_validation"
    assert attempt["diagnostics"]["branch"] == "filter_entity_or_qualifier_not_bound"
    assert attempt["intended_metrics"] == ["revenue"]
    assert attempt["generation_seconds"] >= 0
    assert attempt["diagnostics"]["semantic_validation_seconds"] >= 0


def test_valid_json_with_invalid_aggregate_shape_keeps_planning_diagnostic(semantic_source):
    from src.agent.planner import LocalPlanner
    from src.jsonio import dumps
    from tests.test_planner_execution import ScriptedModel

    invalid = draft()
    invalid["aggregations"] = None
    model = ScriptedModel(dumps(invalid))
    result = LocalPlanner(model).decide(question(), context=semantic_source())
    assert result["plan"]["reason"] == "planning_failed"
    assert result["plan"]["attempts"][0]["error_stage"] == "semantic_validation"
    assert result["plan"]["attempts"][0]["intended_metrics"] == []
