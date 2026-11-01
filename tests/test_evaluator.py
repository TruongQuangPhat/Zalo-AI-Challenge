from __future__ import annotations

from pathlib import Path

import pytest

from src.evaluation.scorer import compare, evaluate, same_value
from src.jsonio import ContractError, dumps


def score(tmp_path: Path, gold: list[dict], predictions: list[dict]):
    labels, outputs = tmp_path / "labels.jsonl", tmp_path / "predictions.jsonl"
    labels.write_text("".join(dumps(r) + "\n" for r in gold))
    outputs.write_text("".join(dumps(r) + "\n" for r in predictions))
    return evaluate(labels, outputs)


def test_full_denominator_90_correct_10_wrong_2900_abstain(tmp_path):
    gold = [{"id": f"q{i}", "type": "number", "answer": i} for i in range(3000)]
    predictions = [
        {**row, "answer": row["answer"] + int(i >= 90)} for i, row in enumerate(gold[:100])
    ]
    report = score(tmp_path, gold, predictions)
    assert report["correct"] == 90
    assert report["wrong"] == 10
    assert report["abstain"] == 2900
    assert report["missing"] == 2900
    assert report["pa_full"] == pytest.approx(80 / 3000)
    assert report["coverage"] == pytest.approx(100 / 3000)
    assert report["accuracy_answered"] == pytest.approx(0.9)


def test_all_abstain_and_missing_have_zero_pa_and_null_answered_accuracy(tmp_path):
    gold = [
        {"id": "a", "type": "number", "answer": 0},
        {"id": "b", "type": "unanswerable", "answer": None},
    ]
    report = score(tmp_path, gold, [{"id": "a", "type": "abstain"}])
    assert (report["pa_full"], report["coverage"], report["accuracy_answered"]) == (0, 0, None)
    assert report["abstain"] == 2


def test_one_dong_difference_is_wrong_even_for_large_amounts():
    assert not same_value(10_000_000_001, 10_000_000_000)
    assert same_value(0.1 + 0.2, 0.3)
    assert not same_value(0.124, 12.4)
    assert not same_value(True, 1)
    assert not same_value(1, True)


def test_aliases_case_spacing_punctuation_but_not_accent_errors():
    assert same_value(" KH-001 ", "Chị Lan", ["KH001"])
    assert same_value("cÀ  PHÊ, sữa", "Cà phê sữa")
    assert not same_value("Sting dầu", "Sting dâu")
    assert not same_value("Chi Lan", "Chị Lan")


def test_ordered_list_aliases():
    expected = {"id": "q", "type": "list", "answer": ["A", "B"], "alias": [["SP001"], ["SP002"]]}
    assert compare({"id": "q", "type": "list", "answer": ["SP001", "SP002"]}, expected)
    assert not compare({"id": "q", "type": "list", "answer": ["B", "A"]}, expected)


def test_table_permutation_columns_aliases_and_non_greedy_matching():
    expected = {
        "id": "q",
        "type": "table",
        "answer": {"columns": ["khach_hang", "so_tien"], "rows": [["A", 100], ["B", 100]]},
        "alias": [["X", "Y"], ["X"]],
    }
    predicted = {
        "id": "q",
        "type": "table",
        "answer": {"columns": ["so_tien", "khach_hang"], "rows": [[100, "X"], [100, "Y"]]},
    }
    assert compare(predicted, expected)
    predicted["answer"]["rows"][1][0] = 101
    assert not compare(predicted, expected)


def test_table_positional_column_fallback_and_duplicate_rows_count():
    expected = {
        "id": "q",
        "type": "table",
        "answer": {"columns": ["khach_hang", "no"], "rows": [["Lan", 10], ["Lan", 10]]},
    }
    predicted = {
        "id": "q",
        "type": "table",
        "answer": {"columns": ["customer", "debt"], "rows": [["Lan", 10], ["Lan", 10]]},
    }
    assert compare(predicted, expected)
    predicted["answer"]["rows"].pop()
    assert not compare(predicted, expected)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("number", 100),
        ("percent", 12.4),
        ("string", "Cà phê"),
        ("date", "2026-01"),
        ("boolean", True),
        ("list", [3, 10]),
        ("table", {"columns": ["khach", "tien"], "rows": [["Lan", 100]]}),
        ("unanswerable", None),
    ],
)
def test_every_gold_type_can_be_scored(tmp_path, kind, value):
    gold = {"id": "q", "type": kind, "answer": value, "alias": []}
    prediction = {"id": "q", "type": kind, "answer": value}
    assert score(tmp_path, [gold], [prediction])["pa_full"] == 1


def test_wrong_unanswerable_is_penalized_and_bad_reason_abstains(tmp_path):
    gold = [{"id": "q", "type": "number", "answer": 0}]
    assert score(tmp_path, gold, [{"id": "q", "type": "unanswerable"}])["pa_full"] == -1
    report = score(tmp_path, gold, [{"id": "q", "type": "unanswerable", "reason": 5}])
    assert report["pa_full"] == 0
    assert report["malformed"] == 1


def test_readable_invalid_numeric_answer_counts_wrong(tmp_path):
    report = score(
        tmp_path,
        [{"id": "q", "type": "number", "answer": 1}],
        [{"id": "q", "type": "number", "answer": True}],
    )
    assert report["wrong"] == 1
    assert report["malformed"] == 1


@pytest.mark.parametrize(
    "predictions",
    [
        [{"id": "q", "type": "abstain"}, {"id": "q", "type": "abstain"}],
        [{"id": "unknown", "type": "abstain"}],
    ],
)
def test_duplicate_or_unknown_prediction_ids_are_fatal(tmp_path, predictions):
    with pytest.raises(ContractError):
        score(tmp_path, [{"id": "q", "type": "number", "answer": 1}], predictions)
