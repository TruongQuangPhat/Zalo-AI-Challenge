"""Fixed read-only aggregates and two-window difference with exact Decimal verification."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from src.agent.plan import validate_query_plan
from src.contracts import validate_answer
from src.data.catalog import BusinessContext
from src.data.normalization.values import exact_add, parse_datetime
from src.data.split import Question
from src.jsonio import ContractError, loads
from src.semantics.answerability import preflight
from src.semantics.rulebook import plain


def _channel_label(value: str) -> str:
    label = plain(value)
    if not label or label == "none":
        raise ContractError("order_channel_missing")
    return {
        "dien thoai": "phone",
        "qua dien thoai": "phone",
        "telephone": "phone",
        "store": "counter",
        "tai quay": "counter",
        "quay": "counter",
        "tai cua hang": "counter",
    }.get(label, label)


def _rounded(value: Decimal, policy: dict) -> Decimal:
    places = policy["decimal_places"]
    with localcontext() as arithmetic:
        arithmetic.prec = max(len(value.as_tuple().digits), value.adjusted() + 1) + places + 4
        quantum = Decimal(1).scaleb(-places)
        rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
        if abs(value - rounded) == quantum / 2 and policy.get("unspecified_tie") == "abstain":
            raise ContractError("rounding_tie_unspecified")
        return rounded


def _divide(numerator: Decimal, denominator: Decimal, policy: dict, *, scale: int = 1) -> dict:
    """Round an exact rational once, without finite-precision division near a half tie."""
    if denominator <= 0:
        raise ContractError("nonpositive_denominator_undefined")
    places = policy["decimal_places"]
    percent = Fraction(numerator) / Fraction(denominator) * scale
    scaled = percent * 10**places
    whole, remainder = divmod(abs(scaled.numerator), scaled.denominator)
    if remainder * 2 == scaled.denominator and policy.get("mode") != "half_up":
        raise ContractError("rounding_tie_unspecified")
    whole += remainder * 2 >= scaled.denominator
    rounded = Decimal((int(percent < 0), tuple(int(c) for c in str(whole)), -places))
    if abs(percent - Fraction(rounded)) > Fraction(1, 2 * 10**places):
        raise ContractError("percent_change_rounding_verification_failed")
    return {
        "exact_ratio": {"numerator": percent.numerator, "denominator": percent.denominator},
        "numerator_value": numerator,
        "denominator_value": denominator,
        "rounded_value": rounded,
    }


def _percent_change(change: Decimal, baseline: Decimal, policy: dict) -> dict:
    if baseline <= 0:
        raise ContractError("percent_change_nonpositive_baseline_undefined")
    return {
        **_divide(change, baseline, policy, scale=100),
        "exact_change": change,
        "baseline_value": baseline,
    }


def _compute(
    context: BusinessContext,
    check: dict,
    *,
    round_result: bool = True,
    deadline: float | None = None,
) -> dict:
    contract, window = check["contract"], check["effective_window"]
    if contract["formula"] in {
        "sum_exact_order_net_merchandise",
        "sum_exact_sale_events_no_order_inference",
    } or (
        contract["metric_id"] == "calendar_day_count"
        and context.prepared.summary.get("shape") == "sales_ledger"
    ):
        return _compute_non_line(context, check, round_result=round_result, deadline=deadline)
    statuses = contract["status_policy"]
    filters = check.get("filters", {})
    sql = (
        "SELECT order_key, ordered_at, status, mapping_status FROM orders "
        f"WHERE status IN ({','.join('?' for _ in statuses)}) ORDER BY order_key"
    )
    started = time.monotonic()

    def budget() -> None:
        if time.monotonic() - started > 15 or (
            deadline is not None and time.monotonic() >= deadline
        ):
            raise ContractError("execution_timeout")

    selected = set()
    value = Decimal(0)
    lines_seen = set()
    lineage_samples = []
    with closing(context.prepared.connect()) as conn:
        customer_groups = (
            dict(conn.execute("SELECT order_key,customer_group FROM order_groups"))
            if "customer_group" in filters
            else {}
        )
        clocks = (
            dict(conn.execute("SELECT order_key,seconds_of_day FROM order_times"))
            if "shift" in filters
            else {}
        )
        channels = (
            dict(conn.execute("SELECT order_key,channel FROM order_scopes"))
            if "channel" in filters
            else {}
        )
        item_groups = (
            dict(conn.execute("SELECT line_key,item_group FROM line_groups"))
            if "item_group" in filters
            else {}
        )
        for field, groups in [("customer_group", customer_groups), ("item_group", item_groups)]:
            if field in filters:
                matches = {
                    v.casefold().strip() for v in groups.values() if plain(v) == filters[field]
                }
                if len(matches) > 1:
                    raise ContractError("group_name_ambiguous")
        conn.set_progress_handler(lambda: int(time.monotonic() - started > 15), 1000)
        for key, ordered_at, status, mapping in conn.execute(sql, statuses):
            budget()
            day = parse_datetime(ordered_at).date().isoformat()
            if not window["start"] <= day <= window["end"]:
                continue
            if mapping != "resolved" or key in selected or status not in statuses:
                raise ContractError("order_grain_or_mapping_failed")
            if "weekday" in filters and date.fromisoformat(day).weekday() != filters["weekday"]:
                continue
            if "customer_group" in filters:
                if key not in customer_groups:
                    raise ContractError("order_group_missing")
                if plain(customer_groups[key]) != filters["customer_group"]:
                    continue
            if "channel" in filters:
                if key not in channels:
                    raise ContractError("order_channel_missing")
                if _channel_label(channels[key]) != filters["channel"]:
                    continue
            if "shift" in filters:
                if key not in clocks:
                    raise ContractError("order_clock_missing")
                first, last = context.rules.require("shift").parameters[filters["shift"]]
                if not Decimal(first) <= Decimal(clocks[key]) < Decimal(last):
                    continue
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
                if role == "order_adjustment":
                    orders_with_lines.add(order)
                    continue
                if role not in contract["row_roles"] or amount is None or line in lines_seen:
                    raise ContractError("line_grain_role_or_amount_failed")
                if "item_group" in filters:
                    if line not in item_groups:
                        raise ContractError("line_group_missing")
                    orders_with_lines.add(order)
                    if plain(item_groups[line]) != filters["item_group"]:
                        continue
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
        elif contract["metric_id"] == "calendar_day_count":
            start, end = (date.fromisoformat(window[k]) for k in ["start", "end"])
            value = Decimal(
                sum(
                    1
                    for offset in range((end - start).days + 1)
                    if "weekday" not in filters
                    or (start + timedelta(days=offset)).weekday() == filters["weekday"]
                )
            )
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


def execute_plan(
    draft: dict, question: Question, context: BusinessContext, *, deadline: float | None = None
) -> dict:
    """Validate and preflight every question; no model-provided numeric values or SQL."""
    plan = validate_query_plan(draft, question, context)
    if "business_spec" in plan:
        from src.agent.business_execution import execute_business_plan

        return execute_business_plan(plan, question, context, deadline)
    for requirement in plan["requirements"]:
        requirement["source_joins"] = (
            context.prepared.summary.get("joins", []) if context.prepared else []
        )
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
        entry_filters = filters
        if entry["filters"]:
            key, value = entry["filters"][0].split("=", 1)
            entry_filters = {key: int(value) if key == "weekday" else value}
        window = windows[entry["window_ref"]]
        check = preflight(
            context,
            entry["metric_id"],
            window["expression"],
            as_of_date=question.as_of_date,
            filters=entry_filters,
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
            if deadline is not None and time.monotonic() >= deadline:
                raise ContractError("question_timeout_before_execution")
            if plan["derived_measures"]:
                operands = {
                    c["aggregation_ref"]: _compute(
                        context, c, round_result=False, deadline=deadline
                    )
                    for c in checks
                }
                measure = plan["derived_measures"][0]
                current = operands[measure["current_ref"]]["exact_value"]
                baseline = operands[measure["baseline_ref"]]["exact_value"]
                comparison = measure["operator"] in {"difference", "percent_change"}
                value = exact_add(current, baseline.copy_negate()) if comparison else current
                if not value.is_finite() or (comparison and exact_add(value, baseline) != current):
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
                        "validated_metric_scope_dependencies",
                        "signed_difference_recomposition"
                        if comparison
                        else "same_effective_window",
                        "finite",
                        "round_once_at_output",
                    ],
                }
                if measure["operator"] == "percent_change":
                    execution.pop("exact_value")
                    execution.update(_percent_change(value, baseline, plan["output"]["rounding"]))
                    execution["checks"] += ["positive_baseline", "exact_rational", "percent_scale"]
                elif not comparison:
                    execution.pop("exact_value")
                    if measure["operator"] == "cancellation_rate" and not 0 <= current <= baseline:
                        raise ContractError("cancelled_count_outside_denominator")
                    execution.update(
                        _divide(
                            current,
                            baseline,
                            plan["output"]["rounding"],
                            scale=100
                            if measure["operator"] in {"share", "cancellation_rate"}
                            else 1,
                        )
                    )
                    execution["checks"] += [
                        "positive_denominator",
                        "exact_rational",
                        "denominator_scope",
                    ]
            elif plan["output"]["expected_type"] == "list":
                by_ref = {c["aggregation_ref"]: c for c in checks}
                operands = {
                    ref: _compute(context, by_ref[ref], deadline=deadline)
                    for ref in plan["output"]["result_refs"]
                }
                execution = {
                    "status": "verified",
                    "operator": "ordered_results",
                    "aggregations": operands,
                    "rounded_value": [
                        operands[ref]["rounded_value"] for ref in plan["output"]["result_refs"]
                    ],
                    "checks": [
                        "each_window",
                        "each_scalar_contract",
                        "question_order",
                        "no_missing_operand_substitution",
                    ],
                }
            else:
                execution = (
                    _snapshot_value(context, checks[0])
                    if stock
                    else _compute(context, checks[0], deadline=deadline)
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise ContractError("question_timeout_after_execution")
            answer = {
                "id": question.id,
                "type": plan["output"]["expected_type"],
                "answer": execution["rounded_value"],
            }
        except (ContractError, sqlite3.Error, ArithmeticError, ValueError, OSError) as exc:
            execution = {"status": "failed", "reason": str(exc)}
    validate_answer(answer)
    return {"answer": answer, "plan": plan, "execution": execution}


def _compute_non_line(
    context: BusinessContext, check: dict, *, round_result: bool, deadline: float | None = None
) -> dict:
    """Reduce native header amounts or ledger records without inventing order lines."""
    c, w = check["contract"], check["effective_window"]
    value = Decimal(0)
    count = 0
    started = time.monotonic()
    if c["metric_id"] == "calendar_day_count":
        start, end = (date.fromisoformat(w[k]) for k in ["start", "end"])
        value = Decimal(
            sum(
                1
                for n in range((end - start).days + 1)
                if "weekday" not in check.get("filters", {})
                or (start + timedelta(days=n)).weekday() == check["filters"]["weekday"]
            )
        )
    else:
        sql = (
            "SELECT o.order_key,o.ordered_at,o.status,r.net_amount,'sale_item' FROM orders o "
            "JOIN order_revenues r USING(order_key)"
            if c["formula"] == "sum_exact_order_net_merchandise"
            else "SELECT event_key,occurred_at,status,net_amount,row_role FROM sales_events"
        )
        with closing(context.prepared.connect()) as conn:
            filters = check.get("filters", {})
            if set(filters) - {"weekday", "customer_group", "item_group", "channel"}:
                raise ContractError("non_line_scope_unsupported")
            channels = (
                dict(conn.execute("SELECT order_key,channel FROM order_scopes"))
                if "channel" in filters
                else {}
            )
            group_field = next((k for k in filters if k.endswith("_group")), None)
            groups = {}
            if group_field:
                group_sql = (
                    "SELECT order_key,customer_group FROM order_groups"
                    if group_field == "customer_group"
                    else "SELECT event_key,item_group FROM event_groups"
                )
                groups = dict(conn.execute(group_sql))
                matches = {
                    v.casefold().strip()
                    for v in groups.values()
                    if plain(v) == filters[group_field]
                }
                if len(matches) > 1:
                    raise ContractError("group_name_ambiguous")
            for key, when, status, money, role in conn.execute(sql):
                if time.monotonic() - started > 15 or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise ContractError("execution_timeout")
                if group_field:
                    if key not in groups:
                        raise ContractError("group_reference_missing")
                    if plain(groups[key]) != filters[group_field]:
                        continue
                day = parse_datetime(when).date()
                if (
                    status not in c["status_policy"]
                    or not w["start"] <= day.isoformat() <= w["end"]
                ):
                    continue
                if "channel" in filters:
                    if key not in channels:
                        raise ContractError("order_channel_missing")
                    if _channel_label(channels[key]) != filters["channel"]:
                        continue
                if (
                    "weekday" in check.get("filters", {})
                    and day.weekday() != check["filters"]["weekday"]
                ):
                    continue
                if role == "order_adjustment":
                    continue
                if role not in c["row_roles"] or money is None:
                    raise ContractError("non_line_amount_or_role_missing")
                amount = Decimal(money)
                if not amount.is_finite():
                    raise ContractError("nonfinite_fact")
                value = exact_add(value, amount)
                count += 1
    return {
        "status": "verified",
        "metric_id": c["metric_id"],
        "exact_value": value,
        "rounded_value": _rounded(value, c["rounding"]) if round_result else None,
        "records_selected": count,
        "checks": [
            "coverage",
            "native_grain",
            "no_invented_order_lines",
            "exact_decimal",
            "status",
            "finite",
        ],
    }
