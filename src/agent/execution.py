"""Fixed read-only aggregates and two-window difference with exact Decimal verification."""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from src.agent.plan import validate_query_plan
from src.agent.scanning import Scan, failure, scanning
from src.contracts import validate_answer
from src.data.catalog import BusinessContext
from src.data.normalization.values import exact_add
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
    if contract["metric_id"] == "calendar_day_count":
        return _compute_non_line(context, check, round_result=round_result, deadline=deadline)
    statuses = contract["status_policy"]
    filters = check.get("filters", {})
    # Prepared timestamps are ISO source-local values. Prefix predicates retain the
    # entire end day, fractions and offsets; SQLite date() would convert to UTC.
    predicate = (
        f"o.status IN ({','.join('?' for _ in statuses)}) "
        "AND substr(o.ordered_at,1,10) BETWEEN ? AND ?"
    )
    parameters = [*statuses, window["start"], window["end"]]
    sql = (
        "SELECT o.order_key,o.ordered_at,o.status,o.mapping_status,"
        "g.customer_group,t.seconds_of_day,s.channel FROM orders o "
        "LEFT JOIN order_groups g USING(order_key) "
        "LEFT JOIN order_times t USING(order_key) "
        "LEFT JOIN order_scopes s USING(order_key) WHERE " + predicate
    )
    started = time.monotonic()
    selected = set()
    value = Decimal(0)
    lines_seen = set()
    lineage_samples = []
    with scanning(context.prepared, deadline) as scan:
        for field, table in [("customer_group", "order_groups"), ("item_group", "line_groups")]:
            if field in filters:
                matches = {
                    v.casefold().strip()
                    for (v,) in scan.rows(f"SELECT DISTINCT {field} FROM {table}")
                    if plain(v) == filters[field]
                }
                # No matching label means the filter names an unknown group: abstain
                # rather than report 0 for a group the source never records.
                if len(matches) != 1:
                    raise ContractError(
                        "group_name_ambiguous" if matches else "group_name_unmatched"
                    )
        for key, ordered_at, status, mapping, customer_group, clock, channel in scan.rows(
            sql, parameters
        ):
            scan.budget()
            day = ordered_at[:10]
            if mapping != "resolved" or key in selected or status not in statuses:
                raise ContractError("order_grain_or_mapping_failed")
            if "weekday" in filters and date.fromisoformat(day).weekday() != filters["weekday"]:
                continue
            if "customer_group" in filters:
                if customer_group is None:
                    raise ContractError("order_group_missing")
                if plain(customer_group) != filters["customer_group"]:
                    continue
            if "channel" in filters:
                if channel is None:
                    raise ContractError("order_channel_missing")
                if _channel_label(channel) != filters["channel"]:
                    continue
            if "shift" in filters:
                if clock is None:
                    raise ContractError("order_clock_missing")
                first, last = context.rules.require("shift").parameters[filters["shift"]]
                if not Decimal(first) <= Decimal(clock) < Decimal(last):
                    continue
            selected.add(key)
        line_sql = (
            "SELECT l.line_key,l.order_key,l.net_amount,l.row_role,g.item_group "
            "FROM orders o JOIN order_lines l USING(order_key) "
            "LEFT JOIN line_groups g USING(line_key) WHERE " + predicate
        )
        if contract["metric_id"] == "revenue":
            orders_with_lines = set()
            for line, order, amount, role, item_group in scan.rows(line_sql, parameters):
                scan.budget()
                if order not in selected:
                    continue
                if role == "order_adjustment":
                    orders_with_lines.add(order)
                    continue
                if role not in contract["row_roles"] or amount is None or line in lines_seen:
                    raise ContractError("line_grain_role_or_amount_failed")
                if "item_group" in filters:
                    if item_group is None:
                        raise ContractError("line_group_missing")
                    orders_with_lines.add(order)
                    if plain(item_group) != filters["item_group"]:
                        continue
                money = scan.number(amount)
                if not money.is_finite():
                    raise ContractError("nonfinite_fact")
                lines_seen.add(line)
                orders_with_lines.add(order)
                value = scan.add(value, money)
                if len(lineage_samples) < 5:
                    lineage_samples.extend(
                        loads(row[0])
                        for row in scan.rows(
                            "SELECT source_ref FROM lineage WHERE line_key=? LIMIT 1", (line,)
                        )
                    )
            if orders_with_lines != selected:
                raise ContractError("selected_order_missing_lines")
        else:
            value = Decimal(len(selected))
        scan.stage = "verifier"
    verifier_started = time.monotonic()
    # S1 verifier: no cardinality fan-out, finite scalar, nonnegative exact count,
    # required coverage and metric policy. Revenue may be signed; do not clamp it.
    if not value.is_finite() or (contract["metric_id"] == "order_count" and value < 0):
        raise ContractError("scalar_verification_failed")
    rounded = _rounded(value, contract["rounding"]) if round_result else None
    scan.stats["verifier_seconds"] = time.monotonic() - verifier_started
    return {
        "status": "verified",
        "metric_id": contract["metric_id"],
        "selected_tables": ["orders"]
        + (["order_lines"] if contract["metric_id"] == "revenue" else []),
        "sql": [sql] + ([line_sql] if contract["metric_id"] == "revenue" else []),
        "parameters": parameters,
        "date_filter": window,
        "scan": scan.summary(),
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


def _snapshot_value(
    context: BusinessContext, check: dict, *, deadline: float | None = None
) -> dict:
    started = time.monotonic()
    entity = check["entity"]
    sql = (
        "SELECT quantity,source_ref FROM inventory_snapshot "
        "WHERE product_code=? AND base_unit=? AND snapshot_at=?"
    )
    params = [entity["product_code"], entity["base_unit"], check["effective_window"]["end"]]
    with scanning(context.prepared, deadline) as scan:
        rows = list(scan.rows(sql, params))
    if len(rows) != 1:
        raise ContractError("snapshot_cardinality_failed")
    value = scan.number(rows[0][0])
    if not value.is_finite():
        raise ContractError("snapshot_nonfinite_quantity")
    return {
        "status": "verified",
        "metric_id": "inventory_on_hand",
        "scan": scan.summary(),
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
    validation_started = time.monotonic()
    try:
        plan = validate_query_plan(draft, question, context)
    except ContractError as exc:
        exc.diagnostics.update(
            error_stage="semantic_validation",
            semantic_validation_seconds=time.monotonic() - validation_started,
        )
        raise
    timings = {"semantic_validation": time.monotonic() - validation_started}
    plan["timings_seconds"] = timings
    preflight_started = time.monotonic()
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
    timings["preflight"] = time.monotonic() - preflight_started
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
                    _snapshot_value(context, checks[0], deadline=deadline)
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
            execution = failure(exc, "execution")
    verifier_started = time.monotonic()
    validate_answer(answer)
    timings["output_verifier"] = time.monotonic() - verifier_started
    return {"answer": answer, "plan": plan, "execution": execution}


def _compute_non_line(
    context: BusinessContext, check: dict, *, round_result: bool, deadline: float | None = None
) -> dict:
    """Reduce native header amounts or ledger records without inventing order lines."""
    c, w = check["contract"], check["effective_window"]
    value = Decimal(0)
    count = 0
    scan = Scan(None, deadline)
    scan.budget()
    filters = check.get("filters", {})
    if c["metric_id"] == "calendar_day_count":
        start, end = (date.fromisoformat(w[k]) for k in ["start", "end"])
        days = (end - start).days + 1
        if "weekday" in filters:
            first = (filters["weekday"] - start.weekday()) % 7
            days = max(0, (days - first + 6) // 7)
        value = Decimal(days)
    else:
        header = c["formula"] == "sum_exact_order_net_merchandise"
        sql = (
            "SELECT o.order_key,o.ordered_at,o.status,r.net_amount,'sale_item',"
            "g.customer_group,s.channel FROM orders o "
            "JOIN order_revenues r USING(order_key) "
            "LEFT JOIN order_groups g USING(order_key) "
            "LEFT JOIN order_scopes s USING(order_key) WHERE "
            if header
            else "SELECT e.event_key,e.occurred_at,e.status,e.net_amount,e.row_role,"
            "g.item_group,NULL FROM sales_events e "
            "LEFT JOIN event_groups g USING(event_key) WHERE "
        )
        axis = "o.ordered_at" if header else "e.occurred_at"
        status = "o.status" if header else "e.status"
        statuses = c["status_policy"]
        sql += (
            f"{status} IN ({','.join('?' for _ in statuses)}) "
            f"AND substr({axis},1,10) BETWEEN ? AND ?"
        )
        parameters = [*statuses, w["start"], w["end"]]
        with scanning(context.prepared, deadline) as scan:
            if set(filters) - {"weekday", "customer_group", "item_group", "channel"}:
                raise ContractError("non_line_scope_unsupported")
            group_field = next((k for k in filters if k.endswith("_group")), None)
            if group_field:
                table = "order_groups" if group_field == "customer_group" else "event_groups"
                matches = {
                    v.casefold().strip()
                    for (v,) in scan.rows(f"SELECT DISTINCT {group_field} FROM {table}")
                    if plain(v) == filters[group_field]
                }
                # No matching label means the filter names an unknown group: abstain
                # rather than report 0 for a group the source never records.
                if len(matches) != 1:
                    raise ContractError(
                        "group_name_ambiguous" if matches else "group_name_unmatched"
                    )
            for _key, when, _status, money, role, group_value, channel in scan.rows(
                sql, parameters
            ):
                scan.budget()
                if group_field:
                    if group_value is None:
                        raise ContractError("group_reference_missing")
                    if plain(group_value) != filters[group_field]:
                        continue
                day = date.fromisoformat(when[:10])
                if "channel" in filters:
                    if channel is None:
                        raise ContractError("order_channel_missing")
                    if _channel_label(channel) != filters["channel"]:
                        continue
                if "weekday" in filters and day.weekday() != filters["weekday"]:
                    continue
                if role == "order_adjustment":
                    continue
                if role not in c["row_roles"] or money is None:
                    raise ContractError("non_line_amount_or_role_missing")
                amount = scan.number(money)
                if not amount.is_finite():
                    raise ContractError("nonfinite_fact")
                value = scan.add(value, amount)
                count += 1
    scan.budget()
    return {
        "status": "verified",
        "metric_id": c["metric_id"],
        "exact_value": value,
        "rounded_value": _rounded(value, c["rounding"]) if round_result else None,
        "records_selected": count,
        "scan": scan.summary(),
        "checks": [
            "coverage",
            "native_grain",
            "no_invented_order_lines",
            "exact_decimal",
            "status",
            "finite",
        ],
    }
