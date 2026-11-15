"""Round 6: regional item names, note statuses, toppings in one cell, sales channels."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.agent.rule_ops import CHANNELS, NET_PROFIT, _line_item
from src.data.normalization.values import note_status
from src.semantics.dialect import align_names
from src.semantics.rulebook import question_plain

CATALOG = ["Má heo nướng nước mắm", "Mực chiên nước mắm", "Đậu hũ chiên giòn", "Cháo nghêu"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Má lợn nướng nước mắm lên giá", "Má heo nướng nước mắm lên giá"),
        ("Tiền bán Đậu phụ rán giòn tháng 10", "Tiền bán Đậu hũ chiên giòn tháng 10"),
        ("Cháo ngao bán được mấy nồi?", "Cháo nghêu bán được mấy nồi?"),
        ("muc ran nuoc mam ban bao nhieu", "Mực chiên nước mắm ban bao nhieu"),
    ],
)
def test_other_region_item_name_becomes_the_catalog_name(question, expected):
    assert align_names(question, CATALOG) == expected


def test_regional_alignment_never_overrides_a_named_catalog_item():
    # The catalog sells both forms: each is its own item, nothing is rewritten.
    names = ["Má heo nướng", "Má lợn nướng"]
    assert align_names("Má lợn nướng bán bao nhiêu?", names) == "Má lợn nướng bán bao nhiêu?"
    # A question that already names a catalog item keeps its wording.
    text = "Mực chiên nước mắm và Má lợn nướng nước mắm"
    assert align_names(text, CATALOG) == text
    assert align_names("Khoai lang nướng bán bao nhiêu?", CATALOG) == (
        "Khoai lang nướng bán bao nhiêu?"
    )


@pytest.mark.parametrize(
    ("note", "status"),
    [
        ("kiểm tra lại tuần sau", "completed"),  # a check, not "trả lại"
        ("Khách trả lại hàng", "returned"),
        ("kiểm tra lại, khách trả lại", "returned"),
        ("hủy", "cancelled"),
    ],
)
def test_note_status_reads_a_check_note_as_completed(note, status):
    assert note_status(note) == status


def test_a_drink_with_toppings_in_its_cell_is_the_drinks_line():
    context = SimpleNamespace(rules=SimpleNamespace(rules={"combined_topping": {}}))
    name = "Latte (M) + 2 Trân châu đen (14.000đ)"
    assert _line_item(context, name, "ly") == ("Latte (M)", "ly")
    plain_shop = SimpleNamespace(rules=SimpleNamespace(rules={}))
    assert _line_item(plain_shop, name, "ly") == (name, "ly")


@pytest.mark.parametrize(
    ("question", "channel"),
    [
        ("doanh thu bán tại quầy tháng 5", "store"),
        ("doanh thu tại tiệm tháng 5", "store"),
        ("đơn bán trên sàn thương mại điện tử", "marketplace"),
        ("đơn bán qua Zalo OA", "zalo_oa"),
    ],
)
def test_channel_wording_names_one_channel(question, channel):
    text = question_plain(question)
    found = [key for key, (pattern, _) in CHANNELS.items() if re.search(pattern, text)]
    assert found == [channel]


@pytest.mark.parametrize(
    ("question", "net"),
    [
        ("Tháng 7/26 trừ hết chi phí quán còn lãi ròng bnhiu?", True),
        ("1-2026 tru chi phi xong quan loi that duoc bao nhieu?", True),
        ("Lợi nhuận ròng tháng 6 năm 2025 là bao nhiêu?", True),
        ("Lãi gộp tháng 6 năm 2025 là bao nhiêu?", False),
    ],
)
def test_net_profit_wording(question, net):
    assert bool(re.search(NET_PROFIT, question_plain(question))) is net


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("nhiêuf", "nhiều"),  # Telex tone key left in the word
        ("dduowcj", "được"),
        ("tíh", "tính"),  # dropped "n" of a final "nh"
        ("từg", "từng"),
        ("kác", "các"),  # "k" cannot start "ac"
        ("nheiu", "nhieu"),  # swapped letters
        ("bánnhiều", "bán nhiều"),  # missing space
        ("đối", None),  # a real word the vocabulary lacks is never rewritten
        ("lương", None),
    ],
)
def test_typo_repair_needs_one_general_slip(token, expected):
    from src.semantics.typos import repair
    from src.semantics.vocabulary import correction_words

    assert repair(token, correction_words()) == expected


def test_typo_repair_keeps_names_and_conditions():
    from src.semantics.typos import repaired
    from src.semantics.vocabulary import is_known

    text = "Tháng 1/2026 thợ Lương Chi Đức làm được mấy lượt xe?"
    assert repaired(text, is_known) == text
    text = "Doanh thu hôm nay so với hôm qua chênh lệch tuyệt đối bao nhiêu?"
    assert repaired(text, is_known) == text
    assert repaired("tíh tới hôm nay kác khách", is_known) == "tính tới hôm nay các khách"
