"""Evidence-based S1 preflight. Eligibility is not a computed or verified answer."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from src.data.cache import fingerprint
from src.data.catalog import digest
from src.data.normalization.values import parse_date
from src.semantics.metric_registry import METRICS, metric_contract
from src.semantics.metric_registry import VERSION as METRIC_VERSION
from src.semantics.rulebook import SemanticError, plain
from src.temporal.resolver import VERSION as TEMPORAL_VERSION
from src.temporal.resolver import iso_day, resolve_window

if TYPE_CHECKING:
    from src.data.catalog import BusinessContext

VERSION = "business-semantics-v3"


def _coverage(context: BusinessContext) -> dict:
    prepared = context.prepared
    source = (
        prepared.summary.get("coverage", {})
        if prepared and prepared.summary.get("shape") in {"csv_order_lines", "csv_order_headers"}
        else {}
    )
    readme_ref = {
        "split": context.catalog["split"],
        "file": f"data/{context.catalog['business_id']}/README.md",
        "sha256": context.catalog["readme_hash"],
    }
    result = {
        **source,
        "fact_type": "orders",
        "time_axis": "ordered_at",
        "available_span": None,
        "confirmed_end": None,
        "snapshot_at": None,
        "history_available": None,
        "source_refs": [readme_ref],
        "availability_basis": [],
        "uncertainties": ["available start/completeness unknown"],
        "conflicts": [],
    }
    text = plain(context.catalog["readme"])
    full = re.findall(
        r"du lieu day du tu (\d{1,2}/\d{1,2}/\d{4}) den het (\d{1,2}/\d{1,2}/\d{4})", text
    )
    ends = re.findall(r"du lieu co den het (\d{1,2}/\d{1,2}/\d{4})", text)
    try:
        spans = {(parse_date(a).isoformat(), parse_date(b).isoformat()) for a, b in full}
        end_days = {parse_date(value).isoformat() for value in ends}
        end_days.update(end for _, end in spans)
    except ValueError:
        result["conflicts"] = ["invalid README coverage date"]
        return result
    observed = source.get("observed_span")
    if len(spans) > 1 or len(end_days) > 1 or any(a > b for a, b in spans):
        result["conflicts"] = ["contradictory README coverage declarations"]
        return result
    if source.get("parse_status") != "complete":
        return result
    if prepared:
        table = context.source_table(prepared.summary["source_id"])
        result["source_refs"].append(table.source_ref)
    if end_days:
        end = next(iter(end_days))
        if observed and observed["end"] > end:
            result["conflicts"] = ["source dates exceed README end"]
            return result
        result["confirmed_end"] = end
        result["availability_basis"].append("README end validated against fully parsed order dates")
    if spans:
        start, end = next(iter(spans))
        if observed and (observed["start"] < start or observed["end"] > end):
            result["conflicts"] = ["source dates contradict README complete span"]
            result["confirmed_end"] = None
            return result
        if source.get("available_span") == {"start": start, "end": end}:
            result["available_span"] = {"start": start, "end": end}
            result["availability_basis"].append("README explicitly declares complete start/end")
            result["uncertainties"] = []
    elif observed:
        # Project policy: answer from the released export, using its fully parsed
        # order-date extent. This is not an external completeness certification.
        result["available_span"] = dict(observed)
        result["availability_basis"].append("observed_order_dates")
        result["uncertainties"] = [
            "Export extent accepted as coverage; missing interior records cannot be detected."
        ]
    return result


def prepare_semantics(context: BusinessContext) -> dict:
    """Attach one small metadata revision; raw catalog/cache is not rewritten."""
    rules = context.rules
    if rules is None:
        raise SemanticError("rule_unresolved", "business has no runtime rule profile")
    for name, expected in context.catalog["dependencies"]["authority"].items():
        if digest(context.root / name) != expected:
            raise SemanticError("stale_context", "authority changed since business preparation")
    readme_path = context.root / "data" / context.catalog["business_id"] / "README.md"
    if digest(readme_path) != context.catalog["readme_hash"]:
        raise SemanticError("stale_context", "README changed since business preparation")
    if rules.sha256 != context.catalog["rulebook_hash"] or rules.split != context.catalog["split"]:
        raise SemanticError("stale_context", "rule profile belongs to another revision/split")
    prepared = context.prepared
    facts_schema = {}
    if prepared and prepared.path is not None:
        summary = prepared.summary
        if any(summary.get(key) != context.catalog[key] for key in ["business_id", "split"]):
            raise SemanticError("stale_context", "prepared facts belong to another namespace")
        authority = summary.get("mapping", {}).get("authority", {})
        if any(
            authority.get(key) != context.catalog[key] for key in ["readme_hash", "rulebook_hash"]
        ):
            raise SemanticError("stale_context", "prepared facts use different mapping authority")
        with closing(prepared.connect()) as connection:
            facts_schema = {
                table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                for table in (
                    ["inventory_snapshot"]
                    if summary.get("shape") == "inventory_snapshot"
                    else ["orders"]
                    if summary.get("shape") == "csv_order_headers"
                    else ["orders", "order_lines"]
                )
            }
    contracts = {}
    for metric_id in METRICS:
        try:
            contracts[metric_id] = {
                "status": "resolved",
                "contract": metric_contract(rules, metric_id).summary(),
            }
        except SemanticError as exc:
            contracts[metric_id] = {"status": "unsupported", "reason": str(exc), "code": exc.code}
    metadata = {
        "version": VERSION,
        "split": context.catalog["split"],
        "business_id": context.catalog["business_id"],
        "metric_version": METRIC_VERSION,
        "temporal_version": TEMPORAL_VERSION,
        "rules": rules.summary(),
        "coverage": _coverage(context),
        "snapshot": prepared.summary.get("snapshot") if prepared else None,
        "metrics": contracts,
        "capabilities": [
            {
                "metric_id": key,
                "status": "unsupported",
                "reason": "validated S1 plan and successful preflight required before output",
            }
            for key in METRICS
        ],
        "metric_execution": "separate_validated_s1_executor",
        "prepared_revision": fingerprint(prepared.summary) if prepared else None,
        "facts_schema": facts_schema,
    }
    metadata["revision"] = fingerprint(metadata)
    context.semantics = metadata
    return metadata


def preflight(
    context: BusinessContext,
    metric_id: str,
    expression: dict,
    *,
    as_of_date: str,
    filters: dict | None = None,
) -> dict:
    """Check a single aggregate/window. Nonempty entity/filter scopes are unsupported.

    A future multi-window plan calls this for each dependency independently.
    This API never reads labels, executes numeric aggregates or emits predictions.
    """
    result = {
        "decision": "abstain",
        "metric_id": metric_id,
        "reason": None,
        "window": None,
        "effective_window": None,
        "contract": None,
        "evidence_refs": [],
        "issue_refs": [],
    }

    def stop(decision: str, code: str) -> dict:
        return {**result, "decision": decision, "reason": code}

    try:
        if not context.semantics:
            prepare_semantics(context)
        prepared = context.prepared
        if context.semantics["prepared_revision"] != (
            fingerprint(prepared.summary) if prepared else None
        ):
            raise SemanticError("stale_context", "prepared revision changed after semantics")
        if (
            filters is not None
            and (not isinstance(filters, dict) or filters)
            and metric_id != "inventory_on_hand"
        ):
            return stop("abstain", "filter_unsupported")
        if context.rules is None:
            return stop("abstain", "rule_unresolved")
        if any(context.semantics[key] != context.catalog[key] for key in ["split", "business_id"]):
            return stop("abstain", "stale_context")
        if context.rules.summary() != context.semantics["rules"]:
            return stop("abstain", "stale_context")
        contract = metric_contract(context.rules, metric_id)
        result["contract"] = contract.summary()
        window = resolve_window(expression, as_of_date, context.rules, context.calendar)
        result["window"] = {
            **window.summary(),
            "expression": expression,
            "time_axis": contract.time_axis,
        }
        coverage = context.semantics["coverage"]
        result["evidence_refs"] = coverage["source_refs"] + [
            ref
            for key in dict.fromkeys(contract.rule_refs + list(window.rule_refs))
            for ref in context.rules.require(key).source_refs
        ]
        if prepared is None or prepared.path is None or prepared.summary["status"] == "unsupported":
            return stop("abstain", "preparation_unsupported")
        schema = context.semantics["facts_schema"]
        if any(
            not set(fields) <= set(schema.get(table, []))
            for table, fields in contract.required_fields.items()
        ):
            return stop("abstain", "required_facts_unsupported")
        if not context.catalog["read_complete"] or coverage["conflicts"]:
            return stop("abstain", "coverage_conflict_or_incomplete_read")
        if metric_id in prepared.summary.get("blocking_metrics", []):
            result["issue_refs"] = [
                row["issue_id"]
                for row in prepared.iter_records("issues")
                if metric_id in row["detail"]["affected_metrics"]
                and row["detail"]["severity"] == "blocking"
            ]
            return stop("abstain", "dependent_quality_issue")
        if metric_id == "inventory_on_hand":
            snapshot = context.semantics.get("snapshot")
            if not snapshot or prepared.summary.get("shape") != "inventory_snapshot":
                return stop("abstain", "snapshot_authority_missing")
            result["evidence_refs"] += snapshot["source_refs"]
            if window.start != window.end or window.start.isoformat() != snapshot["snapshot_at"]:
                return stop("abstain", "snapshot_date_unsupported")
            if not isinstance(filters, dict) or set(filters) != {"entity", "unit", "question"}:
                return stop("abstain", "snapshot_product_scope_required")
            from src.data.normalization.stock import resolve_stock_entity

            try:
                record = resolve_stock_entity(
                    context, filters["question"], filters["entity"], filters["unit"]
                )
            except (ValueError, TypeError) as exc:
                return {
                    **stop("abstain", "snapshot_entity_or_unit_unsupported"),
                    "diagnostic": str(exc),
                }
            result["entity"] = {k: record[k] for k in ["product_code", "product_name", "base_unit"]}
            result["evidence_refs"].append(record["source_ref"])
            result["effective_window"] = {
                "start": snapshot["snapshot_at"],
                "end": snapshot["snapshot_at"],
                "inclusive": True,
            }
            return stop("eligible_to_execute", "snapshot_date_entity_unit_verified")
        end = min(window.end, window.cutoff or window.end)
        if end < window.start:
            return stop("abstain", "empty_after_cutoff")
        span = coverage["available_span"]
        if span is None:
            confirmed_end = coverage["confirmed_end"]
            if confirmed_end and window.start > iso_day(confirmed_end):
                context.rules.require("outside_coverage")
                result["evidence_refs"] += context.rules.require("outside_coverage").source_refs
                return stop("unanswerable", "outside_confirmed_end")
            return stop("abstain", "coverage_unknown")
        start = max(window.start, iso_day(span["start"]))
        end = min(end, iso_day(span["end"]))
        if start > end:
            context.rules.require("outside_coverage")
            result["evidence_refs"] += context.rules.require("outside_coverage").source_refs
            return stop("unanswerable", "outside_available_span")
        if start != window.start or end != min(window.end, window.cutoff or window.end):
            context.rules.require("coverage")
            result["evidence_refs"] += context.rules.require("coverage").source_refs
        result["effective_window"] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "inclusive": True,
        }
        return stop("eligible_to_execute", "dependencies_and_coverage_verified")
    except SemanticError as exc:
        return {**stop("abstain", exc.code), "diagnostic": str(exc)}
    except (OSError, sqlite3.Error) as exc:
        return {**stop("abstain", "prepared_read_failed"), "diagnostic": str(exc)}
