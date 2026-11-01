"""Typed output validation, independent of questions and ground-truth labels."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from src.jsonio import ContractError, dumps

ANSWER_TYPES = {"number", "percent", "string", "date", "boolean", "list", "table", "unanswerable"}
OUTPUT_TYPES = ANSWER_TYPES | {"abstain"}


class ReasonTypeError(ContractError):
    """Rulebook: non-string unanswerable reason makes the line unreadable."""


def number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float, Decimal))


def require_id(record: dict[str, Any]) -> str:
    value = record.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ContractError("id must be a nonempty string")
    return value


def validate_answer(record: dict[str, Any], *, gold: bool = False) -> None:
    require_id(record)
    kind = record.get("type")
    allowed = ANSWER_TYPES if gold else OUTPUT_TYPES
    if not isinstance(kind, str) or kind not in allowed:
        raise ContractError("unknown answer type")
    if kind == "unanswerable" and "reason" in record and not isinstance(record["reason"], str):
        raise ReasonTypeError("unanswerable reason must be a string")
    if not gold:
        extra = set(record) - {"id", "type", "answer", "reason"}
        if extra:
            raise ContractError(f"unexpected output fields: {sorted(extra)}")
        if "reason" in record and kind != "unanswerable":
            raise ContractError("reason only belongs to unanswerable")
    if kind in {"abstain", "unanswerable"}:
        if "answer" in record and record["answer"] is not None:
            raise ContractError(f"{kind} cannot contain a non-null answer")
        return
    if "answer" not in record:
        raise ContractError("missing answer")
    answer = record["answer"]
    dumps(answer)  # Also rejects NaN, Infinity, non-string keys and unsupported objects.
    if kind in {"number", "percent"} and not number(answer):
        raise ContractError("numeric answer must be a JSON number, not bool/string")
    if kind == "boolean" and not isinstance(answer, bool):
        raise ContractError("boolean answer must be true/false")
    if kind == "string" and not isinstance(answer, str):
        raise ContractError("string answer must be a string")
    if kind == "date":
        if not isinstance(answer, str):
            raise ContractError("date answer must be a string")
        if re.fullmatch(r"\d{4}-\d{2}", answer):
            candidate = answer + "-01"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", answer):
            candidate = answer
        else:
            raise ContractError("date must be YYYY-MM or YYYY-MM-DD")
        try:
            date.fromisoformat(candidate)
        except ValueError as exc:
            raise ContractError("invalid calendar date") from exc
    if kind == "list" and not isinstance(answer, list):
        raise ContractError("list answer must be an array")
    if kind == "table":
        if not isinstance(answer, dict) or set(answer) != {"columns", "rows"}:
            raise ContractError("table must contain exactly columns and rows")
        columns, rows = answer["columns"], answer["rows"]
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and c.strip() for c in columns)
            or len(set(columns)) != len(columns)
        ):
            raise ContractError("table requires nonempty, distinct column names")
        if not isinstance(rows, list):
            raise ContractError("table rows must be an array")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ContractError("table row width differs from columns")
