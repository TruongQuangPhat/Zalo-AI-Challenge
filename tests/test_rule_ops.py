"""Deterministic rulebook operators: recognition, rule boundaries and fallbacks."""

from __future__ import annotations

import re

import pytest

from src.agent.rule_ops import AMOUNT, _amount, answer_by_rule
from tests.test_planner_execution import question

ROWS = [
    ["001", "01/01/2026", "Xong", "100", "Item A"],
    ["002", "01/01/2026", "Xong", "20", "Item B"],
    ["003", "02/01/2026", "Đang giao", "60", "Item A"],
    ["004", "03/01/2026", "Đổi trả", "500", "Item C"],
    ["005", "03/01/2026", "Xong", "59", "Item C"],
]


def answer(context, text):
    result = answer_by_rule(question(question=text), context)
    return None if result is None else result["answer"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Tháng 1/2026 có bao nhiêu đơn có doanh thu từ 60 trở lên?", 2),
        ("Tháng 1/2026 có bao nhiêu đơn có doanh thu trên 60?", 1),
        ("Tháng 1/2026 có mấy ngày doanh thu từ 60 trở lên?", 2),
        ("Tháng 1/2026 có bao nhiêu ngày doanh thu dưới 60?", 1),
    ],
)
def test_threshold_counts_follow_inclusive_and_strict_bounds(semantic_source, text, expected):
    # Returned order 004 never counts; day 3 holds only 59 of valid revenue.
    assert answer(semantic_source(rows=ROWS), text) == {
        "id": "synthetic",
        "type": "number",
        "answer": expected,
    }


def test_best_day_takes_highest_revenue_and_earliest_tie(semantic_source):
    context = semantic_source(rows=ROWS)
    result = answer(context, "Ngày nào trong tháng 1/2026 doanh thu cao nhất?")
    assert result == {"id": "synthetic", "type": "date", "answer": "2026-01-01"}


@pytest.mark.parametrize(
    "text",
    [
        "Doanh thu tháng 1/2026?",
        "Tháng 1/2026 khách Lan có bao nhiêu đơn từ 60 trở lên doanh thu?",
        "Ngày nào trong tháng 1/2026 mặt hàng A bán nhiều nhất?",
    ],
)
def test_unrecognized_or_qualified_questions_fall_back_to_the_planner(semantic_source, text):
    assert answer_by_rule(question(question=text), semantic_source(rows=ROWS)) is None


@pytest.mark.parametrize(
    "text,expected",
    [("53.000.000", 53_000_000), ("1,5 trieu", 1_500_000), ("55k", 55_000), ("12000", 12000)],
)
def test_amounts_parse_vietnamese_grouping_and_scales(text, expected):
    assert _amount(re.search(AMOUNT, text)) == expected
