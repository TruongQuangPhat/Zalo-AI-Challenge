"""Bounded S1 metadata with stable business prefix and shared authority references."""

from src.data.catalog import BusinessContext
from src.data.normalization.stock import stock_candidates
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.answerability import prepare_semantics

VERSION = "planner-context-v7"


def build_context(question: Question, context: BusinessContext) -> dict:
    if len(question.question) > 4096:
        raise ContractError("question exceeds planner context budget")
    semantics = context.semantics or prepare_semantics(context)
    metrics = {
        key: entry
        for key, entry in semantics["metrics"].items()
        if entry["status"] != "resolved"
        or all(
            set(columns) <= set(semantics["facts_schema"].get(table, []))
            for table, columns in entry["contract"]["required_fields"].items()
        )
    }
    fields = {}
    for entry in metrics.values():
        if entry["status"] == "resolved":
            for table, columns in entry["contract"]["required_fields"].items():
                fields.setdefault(table, set()).update(columns)
    # Stable metadata comes before per-question values so llama.cpp can reuse its
    # native token-prefix cache. Full quotes/hashes remain in BusinessContext/logs.
    return {
        "version": VERSION,
        "authority": {
            "split": context.catalog["split"],
            "rulebook": {"file": "rulebook.md", "sha256": context.rules.sha256},
            "readme": {
                "file": f"data/{context.catalog['business_id']}/README.md",
                "sha256": context.catalog["readme_hash"],
            },
        },
        "facts_schema": {
            table: [column for column in columns if column in fields.get(table, ())]
            for table, columns in semantics["facts_schema"].items()
            if table in fields
        },
        "metrics": {
            key: {
                field: entry["contract"][field]
                for field in (
                    "required_fields",
                    "input_grain",
                    "count_unit",
                    "status_policy",
                    "time_axis",
                    "unit",
                    "formula",
                    "rule_refs",
                )
            }
            if entry["status"] == "resolved"
            else entry
            for key, entry in metrics.items()
        },
        "coverage": {
            key: semantics["coverage"].get(key)
            for key in (
                "observed_span",
                "available_span",
                "confirmed_end",
                "parse_status",
                "availability_basis",
                "uncertainties",
                "conflicts",
            )
        },
        "rules": [
            {
                "id": key,
                "status": rule.status,
                "operator": rule.operator,
                "parameters": rule.parameters,
                "source_lines": sorted({ref["line"] for ref in rule.source_refs}),
            }
            for key, rule in context.rules.rules.items()
            if key
            in {
                "as_of",
                "week",
                "current_month",
                "missing_year",
                "tet",
                "last_n_days",
                "explicit_month",
                "year_to_date",
                "inclusive_range",
                "difference",
                "percent_change",
                "cancellation_rate",
                "average_order_value",
                "customer_share",
                "item_group",
            }
        ],
        "warnings": [
            "Finite rule clauses only; document-wide exceptions/paraphrases not certified.",
            "Plan even if coverage unknown; preflight decides execution, never infer coverage.",
            (
                "One inventory_on_hand product at snapshot/base unit; no history/conversion."
                if semantics.get("snapshot")
                else "S2 needs verified operands and a positive denominator for division."
            ),
        ],
        "preparation_status": context.prepared.summary["status"] if context.prepared else None,
        "blocking_metrics": context.prepared.summary.get("blocking_metrics", [])
        if context.prepared
        else [],
        "snapshot": semantics.get("snapshot"),
        "inventory_entities": stock_candidates(context, question.question),
        "as_of_date": question.as_of_date,
        "question": question.question,
    }
