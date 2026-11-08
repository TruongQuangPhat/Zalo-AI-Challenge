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
        "Doanh thu tháng 1/2026 không tính hàng A?",
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


def test_whole_shop_revenue_order_count_and_aov_follow_the_revenue_policy(semantic_source):
    context = semantic_source(rows=ROWS)
    # Returned order 004 is excluded; the shipping order 003 counts (§1).
    assert answer(context, "Doanh thu tháng 1/2026?")["answer"] == 239
    assert answer(context, "Tháng 1/2026 có bao nhiêu đơn?")["answer"] == 4
    assert answer(context, "Tháng 1/2026 trung bình mỗi đơn bao nhiêu tiền?")["answer"] == 60


def test_cancellation_rate_divides_by_every_placed_order(semantic_source):
    rows = [*ROWS, ["006", "02/01/2026", "Huỷ", "10", "Item A"]]
    context = semantic_source(rows=rows)
    assert answer(context, "Tháng 1/2026 có bao nhiêu đơn bị huỷ?")["answer"] == 1
    # 1 cancelled of 6 placed orders, all statuses included.
    assert answer(context, "Tỉ lệ huỷ đơn tháng 1/2026 là bao nhiêu %?")["answer"] == 16.67


def test_item_group_share_uses_the_whole_shop_denominator(semantic_source):
    context = semantic_source(
        extra_columns=["group"],
        readme_extra="- **group**: product group\n",
        rows=[
            ["1", "01/01/2026", "Xong", "30", "A", "Trà sữa"],
            ["2", "02/01/2026", "Xong", "70", "B", "Khác"],
        ],
    )
    text = "Tháng 1/2026 nhóm trà sữa chiếm bao nhiêu % doanh thu?"
    assert answer(context, text) == {"id": "synthetic", "type": "percent", "answer": 30.0}
