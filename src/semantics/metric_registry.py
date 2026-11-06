"""Small executable-policy contracts for prepared S1 facts, separate from ETL."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from src.semantics.rulebook import RuleProfile, SemanticError

VERSION = "metric-contracts-v6"
METRICS = (
    "revenue",
    "order_count",
    "inventory_on_hand",
    "cancelled_order_count",
    "placed_order_count",
    "calendar_day_count",
    "quantity",
    "gross_profit",
    "receivable",
    "receivable_customer_count",
    "debt_collection",
    "purchase_amount",
    "stock_balance",
    "lot_expiry",
)


BUSINESS_METRICS = {
    "lot_expiry": (
        {
            "lots": ["lot_id", "product_key", "purchase_id", "expiry", "received_quantity"],
            "lot_allocations": [
                "order_key",
                "line_key",
                "lot_id",
                "quantity",
                "returned_quantity",
                "returned_at",
            ],
            "purchases": ["purchase_key", "purchased_at", "status"],
        },
        ["lot_expiry"],
        "snapshot_at",
        "date",
    ),
    "quantity": (
        {"line_items": ["line_key", "product_key", "base_quantity"]},
        ["quantity_ranking"],
        "ordered_at",
        "source_base_unit",
    ),
    "gross_profit": (
        {
            "line_items": ["line_key", "sold_quantity", "unit_cost"],
            "order_lines": ["net_amount", "row_role"],
        },
        ["gross_profit"],
        "ordered_at",
        "VND",
    ),
    "receivable": (
        {
            "order_parties": ["order_key", "customer_key", "payable", "payment_method"],
            "payments": ["order_key", "paid_at", "amount"],
        },
        ["debt_status", "debt_balance", "debt_cod", "debt_customer"],
        "ordered_at",
        "VND",
    ),
    "receivable_customer_count": (
        {
            "order_parties": ["order_key", "customer_key", "payable", "payment_method"],
            "payments": ["order_key", "paid_at", "amount"],
        },
        ["debt_status", "debt_balance", "debt_cod", "debt_customer", "customer_identity"],
        "ordered_at",
        "customer",
    ),
    "debt_collection": (
        {
            "order_parties": ["order_key", "payment_method"],
            "payments": ["order_key", "paid_at", "amount"],
        },
        ["debt_collection"],
        "paid_at",
        "VND",
    ),
    "purchase_amount": (
        {"purchases": ["purchase_key", "purchased_at", "status", "amount"]},
        ["purchase_amount"],
        "purchased_at",
        "VND",
    ),
    "stock_balance": (
        {"stock_balances": ["product_key", "occurred_at", "balance"]},
        [],
        "occurred_at",
        "source_base_unit",
    ),
}


@dataclass(frozen=True)
class MetricContract:
    metric_id: str
    rulebook_hash: str
    required_fields: dict[str, list[str]]
    input_grain: str
    count_unit: str | None
    status_policy: list[str]
    row_roles: list[str]
    time_axis: str
    history_requirement: str
    unit: str
    price_basis: str | None
    formula: str
    numerator_scope: str | None
    denominator_scope: str | None
    empty_policy: str
    undefined_policy: str
    rounding: dict
    output: dict
    rule_refs: list[str]
    acceptance_refs: list[str]

    def summary(self) -> dict:
        return {"version": VERSION, **asdict(self)}


def metric_contract(
    rules: RuleProfile, metric_id: str, *, shape: str | None = None
) -> MetricContract:
    """A contract authorizes no output until requirements/execution are verified."""
    if metric_id not in METRICS:
        raise SemanticError("metric_unsupported", f"metric is not implemented: {metric_id}")
    # These catalogue clauses qualify existing contracts when present. Older
    # minimal authorities still need the original mandatory core clauses below.
    qualifiers = {
        "revenue": ["revenue_terms"],
        "quantity": ["base_unit_total", "unit_conversion"],
        "cancelled_order_count": ["cancelled_status"],
        "receivable": ["end_day_state"],
        "receivable_customer_count": ["end_day_state"],
        "stock_balance": ["end_day_state", "unit_conversion"],
    }.get(metric_id, [])
    qualifiers = [key for key in qualifiers if key in rules.rules]
    for key in qualifiers:
        rules.require(key)
    if metric_id in BUSINESS_METRICS:
        # Only sold-line measures inherit the sales contract. Debt has a different
        # return/status policy; purchase, payment and stock have their own axes.
        base = metric_contract(
            rules,
            "revenue"
            if metric_id == "gross_profit"
            else "order_count"
            if metric_id == "quantity"
            else "inventory_on_hand",
            shape=shape,
        )
        fields, refs, axis, unit = BUSINESS_METRICS[metric_id]
        if metric_id not in {"purchase_amount", "stock_balance"}:
            fields = {
                "orders": ["order_key", "ordered_at", "status", "mapping_status"],
                **fields,
            }
        if unit == "VND":
            rules.require("rounding")
            refs = [*refs, "rounding"]
        for ref in refs:
            rules.require(ref)
        grain = {
            "quantity": "order_line",
            "gross_profit": "order_line",
            "receivable": "order",
            "receivable_customer_count": "customer",
            "debt_collection": "payment",
            "purchase_amount": "purchase_header",
            "stock_balance": "product_balance",
            "lot_expiry": "lot_snapshot",
        }[metric_id]
        return replace(
            base,
            metric_id=metric_id,
            required_fields=fields,
            rule_refs=list(dict.fromkeys(base.rule_refs + refs + qualifiers)),
            time_axis=axis,
            unit=unit,
            input_grain=grain,
            empty_policy="abstain"
            if metric_id in {"stock_balance", "lot_expiry"}
            else "zero_if_complete",
            output={
                "type": "date" if metric_id == "lot_expiry" else "number",
                "ordering": "not_applicable",
            },
            acceptance_refs={
                "quantity": ["C13", "C18", "C21"],
                "gross_profit": ["C13", "C18", "C21"],
                "receivable": ["C09", "C12", "C15", "C18"],
                "receivable_customer_count": ["C09", "C15", "C18"],
                "debt_collection": ["C09", "C15", "C18"],
                "purchase_amount": ["C09", "C18"],
                "stock_balance": ["C10", "C11", "C12", "C18"],
                "lot_expiry": ["C12", "C14", "C18"],
            }[metric_id],
            count_unit="customer" if metric_id == "receivable_customer_count" else None,
            formula=metric_id,
            status_policy=["completed", "shipping", "returned"]
            if metric_id in {"receivable", "receivable_customer_count", "debt_collection"}
            else ["received"]
            if metric_id == "purchase_amount"
            else []
            if metric_id in {"stock_balance", "lot_expiry"}
            else base.status_policy,
            row_roles=["sale_item"] if metric_id in {"quantity", "gross_profit"} else [grain],
            price_basis="line_cost_per_selling_unit"
            if metric_id == "gross_profit"
            else "order_payable"
            if metric_id == "receivable"
            else "explicit_purchase_header_total"
            if metric_id == "purchase_amount"
            else "payment_amount"
            if metric_id == "debt_collection"
            else None,
            history_requirement="all_prior_orders_and_payments_through_cutoff"
            if metric_id in {"receivable", "receivable_customer_count"}
            else "payment_window_of_all_debt_orders_after_purchase_day"
            if metric_id == "debt_collection"
            else "declared_export_end_with_checked_allocations_and_dated_returns"
            if metric_id == "lot_expiry"
            else "last_recorded_balance_or_exact_declared_snapshot"
            if metric_id == "stock_balance"
            else "fully_read_metric_export",
            rounding={
                "decimal_places": rules.require("rounding").parameters["money_places"],
                "mode": "nearest",
                "unspecified_tie": "abstain",
            }
            if unit == "VND"
            else {"mode": "exact_source"},
        )
    if metric_id in {"cancelled_order_count", "placed_order_count", "calendar_day_count"}:
        base = metric_contract(rules, "order_count", shape=shape)
        refs = list(base.rule_refs)
        statuses = ["completed", "shipping", "cancelled", "returned"]
        if metric_id != "calendar_day_count":
            rules.require("cancellation_rate")
            refs.append("cancellation_rate")
        if metric_id == "cancelled_order_count":
            statuses = ["cancelled"]
        return replace(
            base,
            required_fields={"sales_events": ["occurred_at"]}
            if metric_id == "calendar_day_count" and shape == "sales_ledger"
            else base.required_fields,
            metric_id=metric_id,
            time_axis="occurred_at" if shape == "sales_ledger" else base.time_axis,
            status_policy=statuses,
            rule_refs=refs + qualifiers,
            unit="day" if metric_id == "calendar_day_count" else "order",
            count_unit="day" if metric_id == "calendar_day_count" else "order",
            formula="count_calendar_days_in_effective_window"
            if metric_id == "calendar_day_count"
            else "count_distinct_order_key",
        )
    if metric_id == "inventory_on_hand":
        return MetricContract(
            metric_id=metric_id,
            rulebook_hash=rules.sha256,
            required_fields={
                "inventory_snapshot": [
                    "product_code",
                    "product_name",
                    "base_unit",
                    "quantity",
                    "snapshot_at",
                    "source_ref",
                ]
            },
            input_grain="product_snapshot",
            count_unit=None,
            status_policy=[],
            row_roles=["inventory_snapshot"],
            time_axis="snapshot_at",
            history_requirement="explicit_source_snapshot_only",
            unit="source_base_unit",
            price_basis=None,
            formula="exact_snapshot_quantity",
            numerator_scope=None,
            denominator_scope=None,
            empty_policy="abstain",
            undefined_policy="abstain",
            rounding={"mode": "exact_source"},
            output={"type": "number", "ordering": "not_applicable"},
            rule_refs=[],
            acceptance_refs=["C12", "C17", "C18", "C30"],
        )
    refs = ["dedup", "return_policy", "recorded_status", "time_axis"]
    refs.append("revenue_status" if metric_id == "revenue" else "order_count_status")
    if metric_id == "revenue":
        refs.extend(["revenue_basis", "rounding"])
    if (
        metric_id == "revenue"
        and rules.rules.get("item_role")
        and rules.rules["item_role"].status == "resolved"
    ):
        refs.append("item_role")
    for rule_id in refs:
        rules.require(rule_id)
    revenue = metric_id == "revenue"
    statuses = rules.require("revenue_status" if revenue else "order_count_status").parameters[
        "statuses"
    ]
    fields = {"orders": ["order_key", "ordered_at", "status", "mapping_status"]}
    if revenue:
        fields["order_lines"] = ["order_key", "net_amount", "row_role"]
    result = MetricContract(
        metric_id=metric_id,
        rulebook_hash=rules.sha256,
        required_fields=fields,
        input_grain="order_line" if revenue else "order",
        count_unit=None if revenue else "order",
        status_policy=list(statuses),
        row_roles=["sale_item", "service_fee"]
        if revenue
        and rules.rules.get("item_role")
        and rules.rules["item_role"].status == "resolved"
        else ["sale_item"]
        if revenue
        else [],
        time_axis=rules.require("time_axis").operator,
        history_requirement="full_recorded_order_status; no event cutoff on returns",
        unit="VND" if revenue else "order",
        price_basis="line_net_amount" if revenue else None,
        formula="sum_exact_decimal" if revenue else "count_distinct_order_key",
        numerator_scope=None,
        denominator_scope=None,
        empty_policy="zero_if_complete",
        undefined_policy="abstain",
        rounding={
            "decimal_places": rules.require("rounding").parameters["money_places"],
            "mode": "nearest",
            "unspecified_tie": "abstain",
        }
        if revenue
        else {"decimal_places": 0, "mode": "exact_integer"},
        output={"type": "number", "ordering": "not_applicable"},
        rule_refs=refs + qualifiers,
        acceptance_refs=["C03", "C04", "C06", "C12", "C17", "C18", "C27", "C30"],
    )

    if revenue and shape == "order_headers":
        result = replace(
            result,
            input_grain="order",
            price_basis="explicit_order_net_merchandise",
            formula="sum_exact_order_net_merchandise",
            required_fields={
                "orders": fields["orders"],
                "order_revenues": ["order_key", "net_amount", "source_ref"],
            },
        )
    elif revenue and shape == "sales_ledger":
        result = replace(
            result,
            input_grain="recorded_sale_event",
            time_axis="occurred_at",
            history_requirement="recorded sale-event status; order identity is not recorded",
            formula="sum_exact_sale_events_no_order_inference",
            required_fields={
                "sales_events": ["event_key", "occurred_at", "status", "net_amount", "row_role"]
            },
        )
    return result
