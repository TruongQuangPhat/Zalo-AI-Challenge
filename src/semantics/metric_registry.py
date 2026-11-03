"""Small executable-policy contracts for prepared S1 facts, separate from ETL."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from src.semantics.rulebook import RuleProfile, SemanticError

VERSION = "metric-contracts-v2"
METRICS = ("revenue", "order_count", "inventory_on_hand")


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


def metric_contract(rules: RuleProfile, metric_id: str) -> MetricContract:
    """A contract authorizes no output until requirements/execution are verified."""
    if metric_id not in METRICS:
        raise SemanticError("metric_unsupported", f"metric is not implemented: {metric_id}")
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
    for rule_id in refs:
        rules.require(rule_id)
    revenue = metric_id == "revenue"
    statuses = rules.require("revenue_status" if revenue else "order_count_status").parameters[
        "statuses"
    ]
    fields = {"orders": ["order_key", "ordered_at", "status", "mapping_status"]}
    if revenue:
        fields["order_lines"] = ["order_key", "net_amount", "row_role"]
    return MetricContract(
        metric_id=metric_id,
        rulebook_hash=rules.sha256,
        required_fields=fields,
        input_grain="order_line" if revenue else "order",
        count_unit=None if revenue else "order",
        status_policy=list(statuses),
        row_roles=["sale_item"] if revenue else [],
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
        rule_refs=refs,
        acceptance_refs=["C03", "C04", "C06", "C12", "C17", "C18", "C27", "C30"],
    )
