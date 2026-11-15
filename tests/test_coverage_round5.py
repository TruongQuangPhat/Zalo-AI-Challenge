"""Coverage round 5: guards on rule answers and status-column wording."""

from __future__ import annotations

import pytest

from src.agent.planner import shape_mismatch
from src.agent.rule_ops import answer_by_rule
from src.data.normalization.values import ParseError, normalize_status, note_status
from src.semantics.rulebook import question_plain
from src.semantics.vocabulary import unknown_words
from tests.test_planner_execution import question
from tests.test_rule_ops import ROWS


def test_customer_return_is_a_status_only_in_the_status_column():
    assert normalize_status("Khách trả") == "returned"
    # In a free-text note the same words may say how the customer paid.
    with pytest.raises(ParseError):
        note_status("khách trả")


@pytest.mark.parametrize(
    ("text", "names", "unknown"),
    [
        ("Doanh thu tháng 5/2025 là bao nhiêu?", (), []),
        ("dthu th5/2025 la bnhiu z", (), []),
        ("Doanh thu hoá đơn tháng này", (), []),  # "hoá" is the vocabulary's "hóa"
        ("Doanh thu tháng này sau thuế là bao nhiêu?", (), ["thuế"]),
        ("Doanh thu sau thuế VAT", (), ["thuế", "vat"]),
        ("Tháng này bán được bao nhiêu hộp Xyzal?", (), ["xyzal"]),
        ("Tháng này bán được bao nhiêu hộp Xyzal?", {"xyzal"}, []),
        ("Doanh thu 152k từ 28/5/2025", (), []),  # tokens with digits are not words
    ],
)
def test_only_measured_wording_and_shop_names_are_known(text, names, unknown):
    assert unknown_words(text, names) == unknown


def test_rule_operators_leave_unmeasured_wording_to_the_model(semantic_source):
    context = semantic_source(rows=ROWS)
    known = "Tháng 1/2026 có bao nhiêu đơn có doanh thu từ 60 trở lên?"
    assert answer_by_rule(question(question=known), context)["answer"]["answer"] == 2
    trapped = "Tháng 1/2026 có bao nhiêu đơn có doanh thu từ 60 trở lên, sau khi trừ thuế?"
    assert answer_by_rule(question(question=trapped), context) is None


def test_rule_answers_of_the_wrong_shape_are_dropped(semantic_source):
    context = semantic_source(rows=ROWS)
    text = "Tháng 1/2026 có bao nhiêu đơn có doanh thu từ 60 trở lên, tính theo từng bên?"
    assert answer_by_rule(question(question=text), context) is None


@pytest.mark.parametrize(
    ("text", "answer_type", "expected"),
    [
        ("Sữa bột số 2 của hãng nào bán được nhiều lon nhất?", "number", "string"),
        ("Nhóm nào doanh thu cao nhất tháng này?", "number", "string"),
        ("Đơn bán trên sàn với đơn bán tại tiệm, từng bên doanh thu bao nhiêu?", "number", "list"),
        ("Mỗi kênh doanh thu bao nhiêu?", "number", "list"),
        ("Tuần này có mấy mặt hàng ế, bán không được món nào?", "number", None),
        ("Mặt hàng nào bán chạy nhất?", "string", None),
    ],
)
def test_brand_group_and_per_side_questions_fix_the_answer_shape(text, answer_type, expected):
    assert shape_mismatch(question(question=text), answer_type) == expected


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        (
            "Dthu bq mỗi đơn tuần trc là bnhiu?",
            "doanh thu binh quan moi don tuan truoc la bao nhieu?",
        ),
        ("đếm dúp các khách còn thiếu tiền", "dem giup cac khach con thieu tien"),
    ],
)
def test_chat_spellings_of_average_and_help(text, plain):
    assert question_plain(text) == plain


def test_counting_debtors_is_a_customer_count():
    from src.agent.entities import business_spec

    text = "Đếm giúp các khách còn thiếu tiền đến hết 16/11/2025 được bao nhiêu người nhé?"
    spec = business_spec(question(question=text, as_of_date="2025-11-16"))
    assert spec["metric"] == "receivable_customer_count"


@pytest.mark.parametrize(("quantity_cell", "errors"), [("3", 0), ("7", 1)])
def test_model_mapped_lines_must_multiply_out(quantity_cell, errors):
    import sqlite3

    from src.data.normalization.llm_mapping import _price_arithmetic
    from src.jsonio import dumps

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE order_lines (line_key, net_amount, row_role, source_values)")
    conn.execute("CREATE TABLE line_items (line_key, sold_quantity)")
    for i in range(30):
        # 3 x 12.000đ less 1.000đ = 35.000đ; the wrongly labelled quantity column says 7.
        cells = [[1, "text", "12.000đ", None], [2, "numeric", "1000", None]]
        conn.execute(
            "INSERT INTO order_lines VALUES (?, '35000', 'sale_item', ?)", (i, dumps(cells))
        )
        conn.execute("INSERT INTO line_items VALUES (?, ?)", (i, quantity_cell))
    columns = {"SL": "quantity", "ĐG": "unit_price", "TT": "line_net_amount"}
    summary = {"proposal": {"tables": {"t": {"role": "order_lines", "columns": columns}}}}
    assert len(_price_arithmetic(conn, summary)) == errors
    # Without a labelled unit price there is nothing to multiply.
    assert _price_arithmetic(conn, {"proposal": {"tables": {}}}) == []


@pytest.mark.parametrize(
    ("text", "months"),
    [
        ("Doanh thu tháng 10, 11, 12 năm 2025", ["10/2025", "11/2025", "12/2025"]),
        ("dthu th9, th10, th11/2025 lan luot bnhiu", ["9/2025", "10/2025", "11/2025"]),
        ("Dthu tháng 5, 6, 7/2025 lần lượt bnhiu?", ["5/2025", "6/2025", "7/2025"]),
    ],
)
def test_listed_months_share_the_written_year(text, months):
    from src.agent.plan import _literal_windows

    windows = _literal_windows(question(question=text))
    assert [f"{w['month']}/{w['year']}" for w in windows] == months


def test_month_over_month_claims_are_read_in_order():
    from src.agent.rule_ops import _month_claims

    text = "Dthu T7/2025 cao hơn T6/2025, rồi T8/2025 lại cao hơn T7/2025, đúng vậy không?"
    assert _month_claims(question(question=text)) == [(7, 2025, 6, 2025), (8, 2025, 7, 2025)]


@pytest.mark.parametrize(
    ("written", "value"),
    [("từ 10 củ trở lên", 10_000_000), ("từ 3tr7 trở lên", 3_700_000), ("trên 190k", 190_000)],
)
def test_spoken_money_amounts(written, value):
    import re

    from src.agent.rule_ops import AMOUNT, _amount

    match = re.search(AMOUNT, question_plain(written))
    assert _amount(match) == value


def test_listed_order_level_discount_makes_the_gross_line_amount_revenue():
    from src.data.normalization.llm_mapping import _order_level

    readme = (
        "- **Đơn**: các dòng cùng **STT** là một đơn; khách, bớt, khách trả, ghi chú của đơn "
        "lặp lại ở mọi dòng của đơn.\n"
        "- **Tiền khách trả**: **Khách trả** là số tiền khách trả cho cả đơn.\n"
        "- **Giảm**: giảm giá của dòng."
    )
    assert _order_level(readme, "Bớt")
    assert _order_level(readme, "Khách trả")
    assert not _order_level(readme, "Giảm")


@pytest.mark.parametrize(
    ("name", "model", "brand"),
    [
        ("iPhone 16 Pro Max 256GB Titan Đen", "iPhone 16 Pro Max", "Apple"),
        ("Samsung Galaxy A56 5G 128GB Đen", "Samsung Galaxy A56 5G", "Samsung"),
        ("Nokia 105 4G", "Nokia 105 4G", "Nokia"),  # 4G is the network, not a capacity
        ("iPhone 13 128GB cũ 98%", "iPhone 13", "Apple"),
    ],
)
def test_phone_model_and_brand_follow_the_rulebook(name, model, brand):
    from src.agent.phone import brand_of, model_of

    assert (model_of(name), brand_of(name)) == (model, brand)


@pytest.mark.parametrize(
    ("raw", "value"),
    [("19.990.000đ", 19990000), ("19tr990", 19990000), ("20,1tr", 20100000), ("1200k", 1200000)],
)
def test_phone_book_money_tokens(raw, value):
    from src.agent.phone import money

    assert money(raw) == value


def test_installment_due_days_and_overdue_amount():
    from decimal import Decimal

    from src.agent.phone import _overdue, _schedule

    due = _schedule("2025-07-03", 3, 5)
    assert due == ["2025-08-05", "2025-09-05", "2025-10-05"]
    contract = {"due": due, "each": Decimal(900000), "payments": [("2025-08-05", Decimal(900000))]}
    # A period due on the asked day is not overdue yet; one due before it is.
    assert _overdue(contract, "2025-09-05") == 0
    assert _overdue(contract, "2025-09-06") == 900000
    from src.jsonio import ContractError

    with pytest.raises(ContractError):
        _schedule("2025-01-15", 1, 31)  # no 31 February: the due day is not written


@pytest.mark.parametrize(
    ("text", "installment"),
    [
        ("Khách Lan còn thiếu bao nhiêu tiền góp?", True),
        ("Tháng 6 bán mấy chiếc điện thoại, gộp cả máy mới với máy cũ?", False),
        ("khach con thieu bao nhieu tien gop", True),
    ],
)
def test_installment_wording_is_told_from_combining(text, installment):
    from src.agent.phone import installment_question

    assert installment_question(question(question=text), question_plain(text)) is installment


@pytest.mark.parametrize(
    ("text", "windows"),
    [
        (
            "Dịp Tết âm lịch 2026 có doanh thu hơn hay kém 17 ngày trước Tết bao nhiêu %?",
            [("tet", 2026), ("before_tet", 2026)],
        ),
        ("Doanh thu Tết năm nay so với Tết năm ngoái?", [("tet", 2026), ("tet", 2025)]),
    ],
)
def test_tet_wording(text, windows):
    from src.agent.plan import _literal_windows

    found = _literal_windows(question(question=text, as_of_date="2026-08-05"))
    assert [(w["kind"], w["year"]) for w in found] == windows
