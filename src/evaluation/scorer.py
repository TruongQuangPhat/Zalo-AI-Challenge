"""Internal rulebook comparator; official evaluator equivalence is not yet verified."""

from __future__ import annotations

import unicodedata
from collections import Counter, deque
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.contracts import ReasonTypeError, number, require_id, validate_answer
from src.jsonio import ContractError, read_jsonl

FLOAT_ABS_TOLERANCE = Decimal("0.00000001")


def normalize_name(value: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFC", value).casefold()
        if not c.isspace() and not unicodedata.category(c).startswith("P")
    )


def same_value(predicted: Any, expected: Any, aliases: Any = None) -> bool:
    if isinstance(expected, bool):
        return isinstance(predicted, bool) and predicted == expected
    if number(expected):
        if not number(predicted):
            return False
        left, right = Decimal(str(predicted)), Decimal(str(expected))
        return left.is_finite() and right.is_finite() and abs(left - right) <= FLOAT_ABS_TOLERANCE
    if isinstance(expected, str):
        options = [expected]
        if isinstance(aliases, list):
            options.extend(a for a in aliases if isinstance(a, str))
        return isinstance(predicted, str) and any(
            normalize_name(predicted) == normalize_name(option) for option in options
        )
    if isinstance(expected, list):
        if not isinstance(predicted, list) or len(predicted) != len(expected):
            return False
        alternatives = aliases if isinstance(aliases, list) else []
        return all(
            same_value(p, e, alternatives[i] if i < len(alternatives) else None)
            for i, (p, e) in enumerate(zip(predicted, expected, strict=True))
        )
    if isinstance(expected, dict):
        return (
            isinstance(predicted, dict)
            and set(predicted) == set(expected)
            and all(same_value(predicted[k], v) for k, v in expected.items())
        )
    return predicted is None if expected is None else False


def same_table(predicted: dict, expected: dict, aliases: list) -> bool:
    pcolumns = [normalize_name(c) for c in predicted["columns"]]
    ecolumns = [normalize_name(c) for c in expected["columns"]]
    if len(pcolumns) != len(ecolumns) or len(predicted["rows"]) != len(expected["rows"]):
        return False
    # Unambiguous complete name correspondence; otherwise documented positional fallback.
    if len(set(pcolumns)) == len(pcolumns) and set(pcolumns) == set(ecolumns):
        order = [pcolumns.index(c) for c in ecolumns]
    else:
        order = list(range(len(ecolumns)))
    prows = [[r[i] for i in order] for r in predicted["rows"]]
    adjacency = []
    for i, erow in enumerate(expected["rows"]):
        row_aliases = aliases[i] if i < len(aliases) else []
        adjacency.append(
            [
                j
                for j, prow in enumerate(prows)
                if all(
                    same_value(p, e, row_aliases if c == 0 else None)
                    for c, (p, e) in enumerate(zip(prow, erow, strict=True))
                )
            ]
        )
    # Perfect bipartite match: greedy matching can fail for overlapping row aliases.
    owners: dict[int, int] = {}
    for origin in range(len(adjacency)):
        queue = deque([origin])
        parents: dict[int, tuple[int, int] | None] = {origin: None}
        endpoint = None
        while queue and endpoint is None:
            current = queue.popleft()
            for candidate in adjacency[current]:
                if candidate not in owners:
                    endpoint = (current, candidate)
                    break
                owner = owners[candidate]
                if owner not in parents:
                    parents[owner] = (current, candidate)
                    queue.append(owner)
        if endpoint is None:
            return False
        current, candidate = endpoint
        while True:
            owners[candidate] = current
            previous = parents[current]
            if previous is None:
                break
            current, candidate = previous
    return True


def compare(predicted: dict, expected: dict) -> bool:
    if predicted["type"] != expected["type"]:
        return False
    if expected["type"] == "unanswerable":
        return True
    if expected["type"] == "table":
        return same_table(predicted["answer"], expected["answer"], expected.get("alias", []))
    return same_value(predicted["answer"], expected["answer"], expected.get("alias"))


def evaluate(answers_path: Path, predictions_path: Path) -> dict[str, Any]:
    gold: dict[str, dict] = {}
    for line, row in read_jsonl(answers_path):
        validate_answer(row, gold=True)
        qid = row["id"]
        if qid in gold:
            raise ContractError(f"answers:{line}: duplicate id")
        if not isinstance(row.get("alias", []), list):
            raise ContractError("gold alias must be a list")
        gold[qid] = row
    if not gold:
        raise ContractError("answers must not be empty")
    predictions: dict[str, dict] = {}
    for line, row in read_jsonl(predictions_path):
        qid = require_id(row)
        if qid not in gold:
            raise ContractError(f"predictions:{line}: unknown id")
        if qid in predictions:
            raise ContractError(f"predictions:{line}: duplicate id")
        predictions[qid] = row
    counts: Counter = Counter()
    by_type: dict[str, Counter] = {}
    details = []
    for qid, expected in gold.items():
        predicted = predictions.get(qid)
        issue = None
        if predicted is None:
            verdict = "abstain"
            counts["missing"] += 1
        else:
            try:
                validate_answer(predicted)
                verdict = (
                    "abstain"
                    if predicted["type"] == "abstain"
                    else "correct"
                    if compare(predicted, expected)
                    else "wrong"
                )
            except ReasonTypeError as exc:
                verdict, issue = "abstain", str(exc)
            except ContractError as exc:
                verdict, issue = "wrong", str(exc)
        counts[verdict] += 1
        counts["malformed"] += int(issue is not None)
        by_type.setdefault(expected["type"], Counter())[verdict] += 1
        details.append({"id": qid, "type": expected["type"], "verdict": verdict, "issue": issue})
    total = len(gold)
    answered = counts["correct"] + counts["wrong"]
    return {
        "evaluator": "internal-rulebook-v0",
        "official_equivalence": "unverified",
        "numeric_absolute_tolerance": str(FLOAT_ABS_TOLERANCE),
        "numeric_relative_tolerance": 0,
        "total_questions": total,
        "correct": counts["correct"],
        "wrong": counts["wrong"],
        "abstain": counts["abstain"],
        "missing": counts["missing"],
        "malformed": counts["malformed"],
        "pa_full": (counts["correct"] - counts["wrong"]) / total,
        "coverage": answered / total,
        "accuracy_answered": counts["correct"] / answered if answered else None,
        "by_type": {kind: dict(c) for kind, c in sorted(by_type.items())},
        "details": details,
    }
