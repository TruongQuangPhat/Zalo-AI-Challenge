"""Fixed read-only aggregates and two-window difference with exact Decimal verification."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from src.agent.plan import validate_query_plan
from src.contracts import validate_answer
from src.data.catalog import BusinessContext
from src.data.normalization.values import exact_add, parse_datetime
from src.data.split import Question
from src.jsonio import ContractError, loads
from src.semantics.answerability import preflight


def _rounded(value: Decimal, policy: dict) -> Decimal:
    places = policy["decimal_places"]
    with localcontext() as arithmetic:
        arithmetic.prec = max(len(value.as_tuple().digits), value.adjusted() + 1) + places + 4
        quantum = Decimal(1).scaleb(-places)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
        if abs(value - rounded) == quantum / 2 and policy.get("unspecified_tie") == "abstain":
            raise ContractError("rounding_tie_unspecified")
        return rounded


def _percent_change(change: Decimal, baseline: Decimal, policy: dict) -> dict:
    """Round an exact rational once, without finite-precision division near a half tie."""
    if baseline <= 0:
        raise ContractError("percent_change_nonpositive_baseline_undefined")
    places = policy["decimal_places"]
    percent = Fraction(change) / Fraction(baseline) * 100
    scaled = percent * 10**places
    whole, remainder = divmod(abs(scaled.numerator), scaled.denominator)
    if remainder * 2 == scaled.denominator:
        raise ContractError("rounding_tie_unspecified")
    whole += remainder * 2 > scaled.denominator
    rounded = Decimal((int(percent < 0), tuple(int(c) for c in str(whole)), -places))
    if abs(percent - Fraction(rounded)) >= Fraction(1, 2 * 10**places):
        raise ContractError("percent_change_rounding_verification_failed")
    return {
        "exact_ratio": {"numerator": percent.numerator, "denominator": percent.denominator},
        "exact_change": change,
        "baseline_value": baseline,
        "rounded_value": rounded,
    }


def _compute(context: BusinessContext, check: dict, *, round_result: bool = True) -> dict:
    contract, window = check["contract"], check["effective_window"]
    statuses = contract["status_policy"]
    sql = (
        "SELECT order_key, ordered_at, status, mapping_status FROM orders "
        f"WHERE status IN ({','.join('?' for _ in statuses)}) ORDER BY order_key"
    )
    started = time.monotonic()

    def budget() -> None:
        if time.monotonic() - started > 15:
            raise ContractError("execution_timeout")

    selected = set()
    value = Decimal(0)
    lines_seen = set()
    lineage_samples = []
    with closing(context.prepared.connect()) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() - started > 15), 1000)
        for key, ordered_at, status, mapping in conn.execute(sql, statuses):
            budget()
            day = parse_datetime(ordered_at).date().isoformat()
            if not window["start"] <= day <= window["end"]:
                continue
            if mapping != "resolved" or key in selected or status not in statuses:
                raise ContractError("order_grain_or_mapping_failed")
            selected.add(key)
        line_sql = (
            "SELECT line_key, order_key, net_amount, row_role FROM order_lines ORDER BY line_key"
        )
        if contract["metric_id"] == "revenue":
            orders_with_lines = set()
            for line, order, amount, role in conn.execute(line_sql):
                budget()
                if order not in selected:
                    continue
                if role not in contract["row_roles"] or amount is None or line in lines_seen:
                    raise ContractError("line_grain_role_or_amount_failed")
                money = Decimal(amount)
                if not money.is_finite():
                    raise ContractError("nonfinite_fact")
                lines_seen.add(line)
                orders_with_lines.add(order)
                value = exact_add(value, money)
                if len(lineage_samples) < 5:
                    lineage_samples.extend(
                        loads(row[0])
                        for row in conn.execute(
                            "SELECT source_ref FROM lineage WHERE line_key=? LIMIT 1", (line,)
                        )
                    )
            if orders_with_lines != selected:
                raise ContractError("selected_order_missing_lines")
        else:
            value = Decimal(len(selected))
    # S1 verifier: no cardinality fan-out, finite scalar, nonnegative exact count,
    # required coverage and metric policy. Revenue may be signed; do not clamp it.
    if not value.is_finite() or (contract["metric_id"] == "order_count" and value < 0):
        raise ContractError("scalar_verification_failed")
    rounded = _rounded(value, contract["rounding"]) if round_result else None
    return {
        "status": "verified",
        "metric_id": contract["metric_id"],
        "selected_tables": ["orders"]
        + (["order_lines"] if contract["metric_id"] == "revenue" else []),
        "sql": [sql] + ([line_sql] if contract["metric_id"] == "revenue" else []),
        "parameters": statuses,
        "date_filter": window,
        "orders_selected": len(selected),
        "lines_selected": len(lines_seen),
        "exact_value": value,
        "rounded_value": rounded,
        "source_ref_samples": lineage_samples,
        "lineage": "full lineage retained in prepared database; samples bounded to five",
        "checks": ["coverage", "status", "mapping", "grain", "finite"]
        + (["rounding"] if round_result else []),
        "elapsed_seconds": time.monotonic() - started,
    }


def _snapshot_value(context: BusinessContext, check: dict) -> dict:
    started = time.monotonic()
    entity = check["entity"]
    sql = (
        "SELECT quantity,source_ref FROM inventory_snapshot "
        "WHERE product_code=? AND base_unit=? AND snapshot_at=?"
    )
    params = [entity["product_code"], entity["base_unit"], check["effective_window"]["end"]]
    with closing(context.prepared.connect()) as connection:
        rows = connection.execute(sql, params).fetchall()
    if len(rows) != 1:
        raise ContractError("snapshot_cardinality_failed")
    value = Decimal(rows[0][0])
    if not value.is_finite():
        raise ContractError("snapshot_nonfinite_quantity")
    return {
        "status": "verified",
        "metric_id": "inventory_on_hand",
        "selected_tables": ["inventory_snapshot"],
        "sql": [sql],
        "parameters": params,
        "rows_selected": 1,
        "exact_value": value,
        "rounded_value": value,
        "rounding": "none_exact_source",
        "source_ref_samples": [loads(rows[0][1])],
        "checks": [
            "snapshot_authority",
            "snapshot_date",
            "unique_entity",
            "base_unit",
            "read_only",
            "finite",
        ],
        "elapsed_seconds": time.monotonic() - started,
    }


def execute_plan(draft: dict, question: Question, context: BusinessContext) -> dict:
    """Validate and preflight every question; no model-provided numeric values or SQL."""
    plan = validate_query_plan(draft, question, context)
    agg = plan["aggregations"][0]
    stock = agg["metric_id"] == "inventory_on_hand"
    filters = (
        {
            "entity": plan["resolved_entity"]["product_code"],
            "unit": agg["unit"],
            "question": question.question,
        }
        if stock
        else None
    )
    windows = {w["id"]: w for w in plan["time_windows"]}
    checks = []
    for entry in plan["aggregations"]:
        window = windows[entry["window_ref"]]
        check = preflight(
            context,
            entry["metric_id"],
            window["expression"],
            as_of_date=question.as_of_date,
            filters=filters,
        )
        check.update(aggregation_ref=entry["id"], window_ref=window["id"])
        effective = check["effective_window"]
        coverage_status = "blocked"
        if effective:
            requested = (
                window["requested_start"],
                min(window["requested_end"], window["cutoff"] or window["requested_end"]),
            )
            coverage_status = (
                "full" if (effective["start"], effective["end"]) == requested else "partial"
            )
        elif check["decision"] == "unanswerable":
            coverage_status = "outside"
        elif check["reason"] == "coverage_unknown":
            coverage_status = "unknown"
        window.update(effective_window=effective, coverage_status=coverage_status)
        window["availability_basis"] = context.semantics["coverage"]["availability_basis"]
        checks.append(check)
    plan["preflight"] = checks
    answer = {"id": question.id, "type": "abstain"}
    # Resolve every dependency before any numeric query; uncertainty takes precedence
    # over outside coverage in another window. Never substitute a missing operand by 0.
    blocked = next((c for c in checks if c["decision"] == "abstain"), None)
    outside = next((c for c in checks if c["decision"] == "unanswerable"), None)
    stop = blocked or outside
    execution = {"status": "not_performed", "reason": (stop or checks[0])["reason"]}
    if outside and not blocked:
        answer = {"id": question.id, "type": "unanswerable", "reason": outside["reason"]}
    elif not stop:
        try:
            if plan["derived_measures"]:
                operands = {
                    c["aggregation_ref"]: _compute(context, c, round_result=False) for c in checks
                }
                measure = plan["derived_measures"][0]
                current = operands[measure["current_ref"]]["exact_value"]
                baseline = operands[measure["baseline_ref"]]["exact_value"]
                value = exact_add(current, baseline.copy_negate())
                if not value.is_finite() or exact_add(value, baseline) != current:
                    raise ContractError("difference_verification_failed")
                execution = {
                    "status": "verified",
                    "operator": measure["operator"],
                    "result_ref": measure["id"],
                    "aggregations": operands,
                    "current_ref": measure["current_ref"],
                    "baseline_ref": measure["baseline_ref"],
                    "unit": measure["unit"],
                    "exact_value": value,
                    "rounded_value": _rounded(value, plan["output"]["rounding"])
                    if measure["operator"] == "difference"
                    else None,
                    "checks": [
                        "each_window",
                        "same_metric_unit_scope",
                        "signed_difference",
                        "recomposition",
                        "finite",
                        "round_once_at_output",
                    ],
                }
                if measure["operator"] == "percent_change":
                    execution.pop("exact_value")
                    execution.update(_percent_change(value, baseline, plan["output"]["rounding"]))
                    execution["checks"] += ["positive_baseline", "exact_rational", "percent_scale"]
            else:
                execution = (
                    _snapshot_value(context, checks[0]) if stock else _compute(context, checks[0])
                )
            answer = {
                "id": question.id,
                "type": plan["output"]["expected_type"],
                "answer": execution["rounded_value"],
            }
        except (ContractError, sqlite3.Error, ArithmeticError, ValueError, OSError) as exc:
            execution = {"status": "failed", "reason": str(exc)}
    validate_answer(answer)
    return {"answer": answer, "plan": plan, "execution": execution}
