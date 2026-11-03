"""Fixed read-only S1 queries, exact Decimal reduction and result verification."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

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


def _compute(context: BusinessContext, check: dict) -> dict:
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
    rounded = _rounded(value, contract["rounding"])
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
        "checks": ["coverage", "status", "mapping", "grain", "finite", "rounding"],
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
    check = preflight(
        context,
        agg["metric_id"],
        draft["time_windows"][0]["expression"],
        as_of_date=question.as_of_date,
        filters=filters,
    )
    plan["preflight"] = [check]
    plan["time_windows"][0]["effective_window"] = check["effective_window"]
    answer = {"id": question.id, "type": "abstain"}
    execution = {"status": "not_performed", "reason": check["reason"]}
    if check["decision"] == "unanswerable":
        answer = {"id": question.id, "type": "unanswerable", "reason": check["reason"]}
    elif check["decision"] == "eligible_to_execute":
        try:
            execution = _snapshot_value(context, check) if stock else _compute(context, check)
            answer = {"id": question.id, "type": "number", "answer": execution["rounded_value"]}
        except (ContractError, sqlite3.Error, ArithmeticError, ValueError, OSError) as exc:
            execution = {"status": "failed", "reason": str(exc)}
    validate_answer(answer)
    return {"answer": answer, "plan": plan, "execution": execution}
