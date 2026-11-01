from __future__ import annotations

from decimal import Decimal

import pytest

from src.contracts import ReasonTypeError, validate_answer
from src.jsonio import ContractError, dumps, loads


@pytest.mark.parametrize(
    ("kind", "answer"),
    [
        ("number", 100000),
        ("number", Decimal("18.6")),
        ("percent", Decimal("12.4")),
        ("string", "Cà phê sữa"),
        ("date", "2026-02"),
        ("date", "2024-02-29"),
        ("boolean", False),
        ("list", [3, 10]),
        ("table", {"columns": ["khach_hang", "so_tien"], "rows": [["Lan", 100]]}),
    ],
)
def test_supported_typed_formats(kind, answer):
    record = {"id": "q", "type": kind, "answer": answer}
    validate_answer(record)
    assert loads(dumps(record)) == record


@pytest.mark.parametrize(
    ("kind", "answer"),
    [
        ("number", True),
        ("number", "1.000đ"),
        ("percent", "12.4%"),
        ("boolean", 1),
        ("list", "A,B"),
        ("date", "2026-02-29"),
        ("date", "2026-13"),
        ("date", "01/01/2026"),
        ("table", {"columns": ["a", "a"], "rows": []}),
        ("table", {"columns": ["a", "b"], "rows": [[1]]}),
        ("number", float("nan")),
        ("number", float("inf")),
    ],
)
def test_invalid_typed_formats(kind, answer):
    with pytest.raises(ContractError):
        validate_answer({"id": "q", "type": kind, "answer": answer})


def test_unanswerable_and_abstain_are_distinct():
    validate_answer({"id": "q", "type": "abstain"})
    validate_answer({"id": "q", "type": "unanswerable", "reason": "Thiếu dữ liệu"})
    with pytest.raises(ReasonTypeError):
        validate_answer({"id": "q", "type": "unanswerable", "reason": []})


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', "Infinity", "[}"])
def test_json_rejects_ambiguous_or_nonstandard_values(text):
    with pytest.raises(ContractError):
        loads(text)


def test_decimal_does_not_lose_precision_or_turn_into_string():
    value = Decimal("99999999999999.00000001")
    assert loads(dumps({"value": value}))["value"] == value
    assert dumps(value) == "99999999999999.00000001"
