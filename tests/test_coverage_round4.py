"""Coverage round 4: status wording, period wording and debt age groups."""

from __future__ import annotations

import pytest

from src.agent.debt import _age_groups
from src.agent.plan import _literal_windows, unparsed_time
from src.data.normalization.values import ParseError, normalize_status, note_status
from src.data.split import Question
from src.semantics.rulebook import question_plain


def _q(text: str, as_of: str = "2026-11-13") -> Question:
    return Question("q", "b", text, as_of)


@pytest.mark.parametrize(
    ("note", "status"),
    [
        ("đang ship", "shipping"),
        ("Chưa giao", "shipping"),
        ("khách mang trả", "returned"),
        ("Khách trả hàng", "returned"),
        ("bảo hành 12 tháng", "completed"),
        ("khách trả tiền mặt", "completed"),
    ],
)
def test_note_status_reads_everyday_wording(note, status):
    assert note_status(note) == status


@pytest.mark.parametrize("note", ["Khách trả", "trả lại tiền thừa"])
def test_note_status_keeps_unclear_payment_wording_ambiguous(note):
    # "Khách trả" may be paid or returned; "trả lại tiền" is change, not goods.
    with pytest.raises(ParseError):
        note_status(note)


def test_customer_return_in_a_status_column_and_shipping_wording():
    # Round 5: as a status-column value "Khách trả" is a return (the note stays ambiguous).
    assert normalize_status("Khách trả") == "returned"
    with pytest.raises(ParseError):
        normalize_status("Khách trả tiền")
    assert normalize_status("đang ship") == "shipping"


@pytest.mark.parametrize(
    "text",
    [
        "Doanh thu 7 ngày qua tính cả hôm nay?",
        "doanh thu 7 ngày qua (kể cả hôm nay) là bao nhiêu",
        "7 ngày gần đây kể cả hôm nay bán bao nhiêu tiền",
    ],
)
def test_last_days_including_today_is_one_range_not_today(text):
    assert _literal_windows(_q(text)) == [{"kind": "last_n_days", "days": 7}]


def test_n_days_without_today_clause_stays_unparsed_window():
    assert _literal_windows(_q("doanh thu 7 ngày qua")) == []


def test_period_wordings():
    assert _literal_windows(_q("tiền bán tháng 7 bao nhiêu")) == [{"kind": "month", "month": 7}]
    assert _literal_windows(_q("doanh thu tháng 12 năm ngoái")) == [
        {"kind": "month", "year": 2025, "month": 12}
    ]
    assert _literal_windows(_q("doanh thu tháng 12 năm nay")) == [
        {"kind": "month", "year": 2026, "month": 12}
    ]
    assert _literal_windows(_q("doanh thu th này")) == [{"kind": "current_month"}]
    assert _literal_windows(_q("doanh thu đầu năm tới giờ")) == [{"kind": "year_to_date"}]
    assert _literal_windows(_q("khách nợ đến hết 06/09/2026")) == [
        {"kind": "day", "date": "2026-09-06"}
    ]


def test_bare_month_next_to_a_dated_month_is_not_guessed():
    question = _q("doanh thu tháng 7 và tháng 8/2025")
    assert unparsed_time(question)


def test_money_typos_never_touch_hours():
    assert "bao nhieu tien" in question_plain("tháng này bán bao nhiêu tiềng")
    assert "bao nhieu tieng" in question_plain("mở cửa bao nhiêu tiếng")


def test_age_groups_must_cover_from_zero_without_gaps():
    text = "no con lai theo tuoi no 0–7 ngay, 8–15 ngay va tren 15 ngay"
    assert _age_groups(text) == [(0, 7), (8, 15), (16, None)]
    assert _age_groups("tuoi no 1–7 ngay, 8–15 ngay, tren 15 ngay") is None
    assert _age_groups("tuoi no 0–7 ngay, 9–15 ngay, tren 15 ngay") is None


def test_explicit_comparison_month_is_the_base():
    from src.agent.plan import _literal_windows as windows
    from src.agent.rule_ops import _explicit_base

    for text in [
        "Doanh thu tháng 3/2026 tăng trưởng bao nhiêu % so với tháng 3/2025?",
        "So với T4/2025 thì T4/2026 doanh thu tăng trưởng bao nhiêu %?",
    ]:
        question = _q(text)
        current, base = _explicit_base(question, windows(question))
        assert current["year"] == base["year"] + 1 and current["month"] == base["month"]
    # No "so (với)" before either month: which is the base is not stated.
    question = _q("Doanh thu tháng 3/2026 và tháng 3/2025 tăng bao nhiêu %?")
    assert _explicit_base(question, windows(question)) is None


def test_amount_with_attached_currency_letter():
    import re

    from src.agent.rule_ops import COMPARATORS, _amount

    text = question_plain("8/2025 đếm giùm mấy đh dthu từ 180.000đ trở lên")
    found = next(m for p, _ in COMPARATORS if (m := re.search(p, text)))
    assert _amount(found) == 180000


def test_debt_ranking_and_count_wordings():
    from src.agent.entities import business_spec

    top = business_spec(_q("Tại ngày 11/05/2026 ai đang nợ tiệm nhiều nhất, cho top 3 kèm số nợ"))
    assert (top["metric"], top["group"], top["limit"]) == ("receivable", "customer", 3)
    count = business_spec(_q("Tính tới ngày 16/06/2025, còn bnhiu khách thiếu tiền tiệm z?"))
    assert count["metric"] == "receivable_customer_count"
    # "tháng 3" is a month, never "top 3 customers".
    month = business_spec(_q("Tháng 3/2025 khách nào mua nhiều tiền nhất"))
    assert month["limit"] == 1 and month["type"] == "string"


def test_joined_words_and_trade_slang():
    assert "doanh thu" in question_plain("30 ngày gần nhất doanhthu được bao nhiêu")
    assert "nhap hang" in question_plain("tháng 4 tiệm đánh hàng hết bao nhiêu tiền")


POS_README = """# Dữ liệu shop

File máy POS xuất ra.

## Quy ước
- Dữ liệu có đến hết 14/09/2026.
- **Công nợ**: mỗi lần khách trả tiền cho một đơn là một dòng, **Giá trị** là số tiền trả lần đó; **Tổng sau giảm giá** là số khách phải trả cho cả đơn.
- **Tồn kho**: **Tồn cuối kỳ** là tồn cuối ngày cuối có dữ liệu, theo đơn vị cơ bản.
- **Nhập hàng**: **Cần trả NCC** là tổng tiền của cả phiếu nhập.
"""  # noqa: E501


def _fields(columns, readme=POS_README):
    from types import SimpleNamespace

    from src.data.normalization.entities import source_fields

    profile = {
        "source_ref": {"file": "data/shop/x.xlsx", "table": "Sheet1"},
        "columns": [{"position": i, "display_name": n} for i, n in enumerate(columns)],
    }
    found = source_fields(SimpleNamespace(catalog={"readme": readme}), profile)
    return {key: columns[position] for key, position in found.items()}


def test_pos_convention_sentences_name_payment_receipt_and_stock_columns():
    payments = _fields(["Mã phiếu", "Thời gian", "Mã hóa đơn", "Phương thức thanh toán", "Giá trị"])
    assert payments["paid_amount"] == "Giá trị" and payments["payment_id"] == "Mã phiếu"
    assert payments["paid_at"] == "Thời gian" and payments["order_id"] == "Mã hóa đơn"
    receipts = _fields(["Mã nhập hàng", "Thời gian", "Mã NCC", "Cần trả NCC", "Trạng thái"])
    assert receipts["purchase_amount"] == "Cần trả NCC"
    assert receipts["purchase_id"] == "Mã nhập hàng" and receipts["purchased_at"] == "Thời gian"
    stock = _fields(["Mã hàng", "Tên hàng", "ĐVT", "Tồn cuối kỳ"])
    assert stock["snapshot_balance"] == "Tồn cuối kỳ" and stock["base_unit"] == "ĐVT"


def test_pos_keys_are_named_only_next_to_a_defined_amount():
    # Without the README's amount column a "Mã phiếu" is not a payment key.
    other = _fields(["Mã phiếu", "Thời gian", "Mã hóa đơn", "Số tiền khác"])
    assert "payment_id" not in other and "paid_at" not in other


def test_pos_payment_methods_are_canonical():
    from src.agent.debt import method_code

    assert [method_code(m) for m in ["Tiền mặt", "Ghi nợ", "Thu hộ (COD)", "debt"]] == [
        "cash",
        "debt",
        "cod",
        "debt",
    ]
    assert method_code(None) is None


def test_stock_answer_needs_the_base_unit_question():
    from src.agent.rule_ops import _base_unit_asked

    assert _base_unit_asked(_q("hết hôm nay bánh croissant còn lại mấy cái trong kho"), "cái")
    assert not _base_unit_asked(
        _q("Cuối ngày hôm nay Bánh croissant còn mấy hộp chẵn, mấy cái lẻ?"), "cái"
    )
    assert not _base_unit_asked(_q("trong kho còn mấy thùng bia tiger lon"), "lon")


def test_receipt_numbers_read_exactly():
    from decimal import Decimal

    from src.agent.purchasing import _number

    assert _number("56,356,700") == Decimal(56356700)
    assert _number("1.105.000đ") == Decimal(1105000)
    assert _number(12.0) == Decimal(12)
    from src.jsonio import ContractError

    with pytest.raises(ContractError):
        _number(12.5)


@pytest.mark.parametrize(
    ("text", "plain_text"),
    [
        ("tháng 7/2025 cửa hàng bán được total bn tiền", "ban duoc tong cong bao nhieu tien"),
        ("revenue ngày nào cao nhất", "doanh thu ngay nao cao nhat"),
        ("5 best seller tháng 2/2026", "5 mat hang ban chay nhat"),
        ("doanh thu đơn trả bằng tiền tươi", "tien mat"),
        ("bán hàng không cần đơn bác sĩ được bao nhiêu", "khong ke don"),
        ("có bnhiu bill bị huỷ", "bao nhieu don bi huy"),
    ],
)
def test_english_and_chat_shop_words(text, plain_text):
    assert plain_text in question_plain(text)


def _conversions(readme: str):
    from types import SimpleNamespace

    from src.data.normalization.entities import conversion_table

    return conversion_table(SimpleNamespace(catalog={"readme": readme, "tables": []}))


def test_readme_unit_list_converts_large_units():
    readme = (
        "## Quy ước\n- **Quy đổi đơn vị**: số lượng ghi theo đơn vị của dòng:\n"
        "  - Coca Cola 330ml: 1 lốc = 6 lon\n  - Coca Cola 330ml: 1 thùng = 24 lon\n"
    )
    from src.data.normalization.stock import identity_text as key

    table, only_large = _conversions(readme)
    base, larges = table[key("Coca Cola 330ml")]
    assert base == "lon" and only_large
    assert larges == {key("lốc"): 6, key("thùng"): 24}


def test_conflicting_unit_list_is_not_used():
    readme = (
        "- **Quy đổi đơn vị**: số lượng ghi theo đơn vị của dòng:\n"
        "  - Bia: 1 thùng = 24 lon\n  - Bia: 1 thùng = 20 lon\n"
    )
    assert _conversions(readme) is None
    # Without the README's conversion clause no list is read.
    assert _conversions("- Bia: 1 thùng = 24 lon\n") is None
