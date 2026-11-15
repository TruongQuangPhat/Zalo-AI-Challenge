"""Coverage round 2: messier shop layouts and chat questions, same facts and answers."""

from __future__ import annotations

import csv
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.rule_ops import _unrecorded_attribute
from src.data.catalog import prepare_business
from src.data.ingestion import Cell, SourceRow
from src.data.normalization import prepare_facts
from src.data.normalization.sales import SalesMapping, _row_role, _sheet_year
from src.data.normalization.values import (
    STATUS_ALIASES,
    ParseError,
    annotation_label,
    normalize_status,
    note_status,
    strip_unit,
    summary_label,
)
from src.data.split import load_split
from src.semantics.rulebook import question_plain
from tests.test_llm_mapping import DECLARED, RULES, FakeModel

NOTE_RULE = (
    "| Luật | Mặc định |\n|---|---|\n"
    "| Sổ không có cột trạng thái | trạng thái đơn ghi ở cột Ghi chú. Ghi chú không nhắc trạng "
    "thái là đơn hoàn thành |\n"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cộng ngày 01/05/2026", True),
        ("Tổng 01/05", True),
        ("TỔNG CỘNG", True),
        ("Tổng tháng 5", True),
        ("Tống Thắng Khánh", False),
        ("Tổng đơn", False),
        ("Cộng Hoà", False),
    ],
)
def test_subtotal_labels_are_not_customer_names(text, expected):
    assert summary_label(text) is expected


def test_quantity_unit_is_stripped_only_for_the_lines_own_unit():
    assert strip_unit("2 ly", "Ly") == "2"
    assert strip_unit("1 gói", "gói") == "1"
    assert strip_unit("2 ly", "gói") == "2 ly"
    assert strip_unit("3", None) == "3"


def test_note_status_follows_the_rulebook_and_never_guesses():
    assert note_status(None) == "completed"
    assert note_status("khách quen") == "completed"
    assert note_status("Huỷ") == "cancelled"
    assert note_status("khách trả lại") == "returned"
    with pytest.raises(ParseError):
        note_status("khách đổi ý")  # outcome wording outside the vocabulary
    token = STATUS_ALIASES.set({"khach doi y": "returned", "giao tan nha": "not_status"})
    try:
        assert note_status("khách đổi ý") == "returned"
        assert note_status("giao tận nhà") == "completed"
        with pytest.raises(ParseError):
            normalize_status("giao tận nhà")  # "not a status" never fills a status column
    finally:
        STATUS_ALIASES.reset(token)


def test_year_less_dates_take_the_sheet_then_the_file_year():
    def profile(file, table):
        return {"source_ref": {"file": file, "table": table}}

    assert _sheet_year(profile("BanHang_T5_2025.xlsx", "DonHang")) == 2025
    assert _sheet_year(profile("Ban_2024.xlsx", "T05-2025")) == 2025
    assert _sheet_year(profile("Ban.csv", "csv")) is None


def test_repeated_xlsx_header_matches_despite_cell_addresses():
    def row(ordinal, number, values):
        return SourceRow(
            ordinal,
            number,
            {
                i: Cell(v, "text", {"address": f"{chr(64 + i)}{number}"})
                for i, v in enumerate(values, 1)
            },
        )

    mapping = SalesMapping("s", {"order_id": 1, "ordered_at": 2, "item": 3}, False, None, False)
    header = row(1, 1, ["Mã", "Ngày", "Hàng"])
    assert _row_role(row(60, 60, ["Mã", "Ngày", "Hàng"]), header, mapping) == "repeated_header"
    assert _row_role(row(61, 61, ["Tổng 01/05", None, None]), header, mapping) == "summary"
    customer = SalesMapping(
        "s", {"order_id": 1, "ordered_at": 2, "item": 3, "customer": 4}, False, None, False
    )
    header = row(1, 1, ["Mã", "Ngày", "Hàng", "Khách"])
    sale = row(2, 2, ["P1", "01/05/2026", "Trà", "Tống Thắng Khánh"])
    assert _row_role(sale, header, customer) == "data"
    # A subtotal label in the customer column of an otherwise empty row is still a subtotal.
    wide = replace(customer, shape="xlsx_order_lines")
    subtotal = row(3, 3, [None, None, None, "Cộng ngày 01/05/2026"])
    assert _row_role(subtotal, header, wide) == "summary"


def test_footer_labels_in_the_date_column_are_not_orders():
    def row(number, values):
        return SourceRow(
            number, number, {i: Cell(v, "text", {}) for i, v in enumerate(values, 1) if v}
        )

    mapping = SalesMapping(
        "s", {"ordered_at": 1, "order_id": 2, "item": 3, "amount": 4}, False, None, False
    )
    header = row(1, ["Ngày", "STT", "Hàng", "Tiền"])
    assert _row_role(row(90, ["Tổng cộng", None, None, "1.480.000"]), header, mapping) == "summary"
    assert _row_role(row(91, ["Ghi chú: số liệu ghi tay"]), header, mapping) == "annotation"
    # A note next to a real sale is the sale's note, never a footer.
    sale = row(92, ["01/05/2026", "7", "Ghi chú: trà", "20000"])
    assert _row_role(sale, header, mapping) == "data"
    assert annotation_label("Lưu ý: giá đã gồm VAT")
    assert not annotation_label("ghi chú khách quen")


def test_declared_column_names_resolve_a_header_without_generic_words(synthetic_split):
    path = synthetic_split / "data/shop_a/sales.csv"
    path.write_text("Tồn cuối,ĐV gốc,Tên SP\n5,chai,Nước suối\n")

    def status(readme: str) -> str:
        (path.parent / "README.md").write_text(readme)
        catalog = prepare_business(load_split(synthetic_split), "shop_a").catalog
        return catalog["tables"][0]["structure_status"]

    assert status("# Dữ liệu\n") == "ambiguous"
    declared = "# Dữ liệu\n\n## Cột trong `sales.csv`\n- **Tồn cuối**: tồn\n- **ĐV gốc**: đvt\n"
    assert status(declared) == "resolved"


LEDGER = ["Ngày", "Mặt hàng", "SL", "Tiền dòng", "Ghi chú"]
LEDGER_ROWS = [
    ["01/01/2026", "Cà phê", "2", "100000", ""],
    ["01/01/2026", "Bánh mì", "1", "30000", "khách quen"],
    ["02/01/2026", "Cà phê", "1", "50000", "Huỷ"],
    ["03/01/2026", "Trà đá", "2", "40000", "giao tận nhà"],
]
LEDGER_LABELS = {
    "Ngày": "ordered_at",
    "Mặt hàng": "item_name",
    "SL": "quantity",
    "Tiền dòng": "line_net_amount",
    "Ghi chú": "order_note",
}


@pytest.fixture
def ledger(synthetic_split: Path):
    path = synthetic_split / "data/shop_a/sales.csv"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows([LEDGER, *LEDGER_ROWS])
    (path.parent / "README.md").write_text(
        "# Dữ liệu shop_a\n\nSổ bán hàng.\n\n## Quy ước\n- Dữ liệu có đến hết 03/01/2026.\n"
    )
    return synthetic_split


def _ledger_proposal() -> dict:
    declared = {**DECLARED, "row_grain": "ledger_no_order_id", "numeric_convention": "unknown"}
    return {
        "tables": {"sales.csv": {"role": "order_lines", "columns": LEDGER_LABELS}},
        "declarations": declared,
    }


def test_note_column_carries_status_only_under_the_rulebook_clause(ledger, tmp_path):
    (ledger / "rulebook.md").write_text(RULES)
    model = FakeModel(_ledger_proposal(), _ledger_proposal())
    without = prepare_facts(
        prepare_business(load_split(ledger), "shop_a"), tmp_path / "a", propose=model
    )
    assert without.summary["status"] == "unsupported"

    (ledger / "rulebook.md").write_text(RULES + NOTE_RULE)
    model = FakeModel(_ledger_proposal(), {"giao tận nhà": "not_status"})
    prepared = prepare_facts(
        prepare_business(load_split(ledger), "shop_a"), tmp_path / "b", propose=model
    )
    assert prepared.summary["status"] == "ready"
    events = list(prepared.iter_records("sales_events"))
    sold = sum((e["net_amount"] for e in events if e["status"] == "completed"), Decimal(0))
    assert sold == Decimal(170000)  # the cancelled note is out


def test_order_total_written_on_every_line_is_rejected(synthetic_split, tmp_path):
    header = ["Số phiếu", "Ngày lập", "Tình trạng", "Mặt hàng", "Tiền dòng", "Khách trả"]
    rows = []
    for n in range(30):
        lines = [("Cà phê", 50000), ("Bánh mì", 20000 + n)]
        total = sum(a for _, a in lines)
        rows += [[f"P{n}", "01/01/2026", "Xong", item, a, total] for item, a in lines]
    path = synthetic_split / "data/shop_a/sales.csv"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows([header, *rows])
    (path.parent / "README.md").write_text(
        "# Dữ liệu shop_a\n\nSổ bán hàng.\n\n## Quy ước\n- Dữ liệu có đến hết 03/01/2026.\n"
    )
    (synthetic_split / "rulebook.md").write_text(RULES)
    labels = {
        "Số phiếu": "order_id",
        "Ngày lập": "ordered_at",
        "Tình trạng": "status",
        "Mặt hàng": "item_name",
        "Tiền dòng": "other",
        "Khách trả": "line_net_amount",
    }
    wrong = {"tables": {"sales.csv": {"role": "order_lines", "columns": labels}}}
    wrong["declarations"] = DECLARED
    context = prepare_business(load_split(synthetic_split), "shop_a")
    prepared = prepare_facts(context, tmp_path / "x", propose=FakeModel(wrong, wrong))
    assert prepared.summary["status"] == "unsupported"
    assert "label it order_payable" in str(prepared.summary["llm_rejections"])

    right = {**labels, "Tiền dòng": "line_net_amount", "Khách trả": "order_payable"}
    good = {"tables": {"sales.csv": {"role": "order_lines", "columns": right}}}
    good["declarations"] = DECLARED
    context = prepare_business(load_split(synthetic_split), "shop_a")
    prepared = prepare_facts(context, tmp_path / "y", propose=FakeModel(good))
    assert prepared.summary["status"] == "ready"


def _context(columns: list[str], readme: str = "", clauses: list[str] = ()) -> SimpleNamespace:
    tables = [{"columns": [{"display_name": c} for c in columns]}]
    return SimpleNamespace(
        catalog={"tables": tables, "readme": readme},
        rules=SimpleNamespace(clauses=list(clauses)),
    )


def test_unrecorded_attributes_are_unanswerable_only_when_no_column_records_them():
    shop = _context(["Mã KH", "Tên khách", "Nhóm khách"])
    assert _unrecorded_attribute(shop, question_plain("Khách nữ mua bao nhiêu tiền?"))
    assert _unrecorded_attribute(shop, question_plain("mấy khách ở đường Trần Não"))
    assert _unrecorded_attribute(shop, question_plain("nhân viên nào lành nghề nhất"))
    assert _unrecorded_attribute(shop, question_plain("khách năm 2025 mua bao nhiêu")) is None
    assert _unrecorded_attribute(shop, question_plain("trong kho tính theo giá vốn")) is None
    recorded = _context(["Mã KH", "Giới tính", "Địa chỉ"])
    assert _unrecorded_attribute(recorded, question_plain("Khách nữ mua bao nhiêu?")) is None
    assert _unrecorded_attribute(recorded, question_plain("khách ở đường Trần Não")) is None


@pytest.mark.parametrize(
    ("chat", "standard"),
    [
        ("tháng ni đh bnhiu z", "thang nay don hang bao nhieu vay"),
        ("Dthu th8/2025 hơn hay kém", "doanh thu thang 8/2025 hon hay kem"),
        ("hàng mô bán nhứt", "hang nao ban nhat"),
        ("hết nhiêu tiền", "het bao nhieu tien"),
        ("xe mô tô", "xe mo to"),
        ("SP0012 bán đc mấy cái", "sp0012 ban duoc may cai"),
        ("mã sp-01", "ma sp-01"),
    ],
)
def test_chat_spellings_become_standard_words_and_codes_stay(chat, standard):
    assert question_plain(chat) == standard


RANKING_RULES = (
    "## 3. Khách\n| Luật | Mặc định |\n|---|---|\n"
    "| Khách trung thành nhất | khách (xác định như dòng Xác định khách) có **nhiều ngày có "
    "mua nhất** trong kỳ |\n"
    "| Khách sộp nhất | khách có **giá trị trung bình một đơn** cao nhất trong kỳ, chỉ xét "
    "khách có **từ 3 đơn trở lên** trong kỳ |\n"
    "| Khách hay đổi ý nhất | khách có **nhiều đơn huỷ nhất** trong kỳ |\n"
    "| Mặt hàng được ưa chuộng nhất | mặt hàng có mặt trong **nhiều đơn nhất** trong kỳ |\n"
)
CUSTOMER_ROWS = [
    # Lan: 3 orders on 2 days, average 100. Minh: 2 orders on 2 days, average 500.
    # Hoa: 3 orders on 3 days, average 40, plus 2 cancelled orders.
    ["1", "01/01/2026", "Xong", "100", "Trà", "Lan"],
    ["2", "01/01/2026", "Xong", "100", "Trà", "Lan"],
    ["3", "02/01/2026", "Xong", "100", "Bánh", "Lan"],
    ["4", "01/01/2026", "Xong", "500", "Bánh", "Minh"],
    ["5", "02/01/2026", "Xong", "500", "Bánh", "Minh"],
    ["6", "01/01/2026", "Xong", "40", "Trà", "Hoa"],
    ["7", "02/01/2026", "Xong", "40", "Trà", "Hoa"],
    ["8", "03/01/2026", "Xong", "40", "Kẹo", "Hoa"],
    ["9", "03/01/2026", "Huỷ", "40", "Kẹo", "Hoa"],
    ["10", "03/01/2026", "Huỷ", "40", "Kẹo", "Hoa"],
    ["11", "03/01/2026", "Huỷ", "40", "Kẹo", "Minh"],
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tháng 1/2026 khách trung thành nhất là ai?", "Hoa"),
        ("Tháng 1/2026 khách sộp nhất là ai?", "Lan"),  # Minh has only 2 orders
        ("Tháng 1/2026 khách hay đổi ý nhất là ai?", "Hoa"),
        ("Tháng 1/2026 món nào được ưa chuộng nhất?", "Trà"),
    ],
)
def test_rulebook_rankings_follow_their_definitions(semantic_source, rulebook_text, text, expected):
    from src.agent.rule_ops import answer_by_rule
    from tests.test_planner_execution import question

    context = semantic_source(
        rows=CUSTOMER_ROWS,
        rules=rulebook_text + RANKING_RULES,
        extra_columns=["customer"],
        readme_extra="- **customer**: customer name\n",
    )
    result = answer_by_rule(question(question=text), context)
    assert result is not None and result["answer"]["answer"] == expected


def test_rulebook_rankings_need_the_rulebook_definition(semantic_source):
    from src.agent.rule_ops import answer_by_rule
    from tests.test_planner_execution import question

    context = semantic_source(
        rows=CUSTOMER_ROWS,
        extra_columns=["customer"],
        readme_extra="- **customer**: customer name\n",
    )
    text = "Tháng 1/2026 khách sộp nhất là ai?"
    assert answer_by_rule(question(question=text), context) is None


class _Rewriter:
    def __init__(self, text: str):
        self.text = text

    def generate(self, messages, schema, timeout, *, max_tokens=None):
        import json

        return {"text": json.dumps({"question": self.text}), "finish_reason": "stop"}


@pytest.mark.parametrize(
    ("rewrite", "kept"),
    [
        ("Doanh thu tháng 8/2025 của Trà Sữa Hoa là bao nhiêu?", True),
        ("Doanh thu tháng 9/2025 của Trà Sữa Hoa là bao nhiêu?", False),  # number changed
        ("Doanh thu tháng 8/2025 của Trà sữa là bao nhiêu?", False),  # name changed
        ("Doanh thu tháng 8/2025 hôm qua của Trà Sữa Hoa?", False),  # period added
    ],
)
def test_model_rewrite_is_used_only_when_numbers_names_and_periods_survive(rewrite, kept):
    from src.agent.planner import rewrite_question
    from src.data.split import Question

    original = Question("q", "b", "dthu th 8/2025 của Trà Sữa Hoa bnhiu z", "2026-01-01")
    result, outcome = rewrite_question(_Rewriter(rewrite), original)
    assert (result is not None) is kept
    assert outcome["status"] == ("rewritten" if kept else "unfaithful")


def test_planner_logs_why_a_model_rewrite_did_not_reach_a_rule_answer(semantic_source):
    from src.agent.planner import LocalPlanner
    from src.data.split import Question

    rewrite = "Doanh thu tháng này của quán qzxv là bao nhiêu?"
    original = Question("q", "b", "dthu thang nay cua quan qzxv bnhiu z", "2026-01-31")
    result = LocalPlanner(_Rewriter(rewrite)).decide(original, context=semantic_source())
    assert result["answer"]["type"] == "abstain"
    assert result["plan"]["rewrite"] == {
        "status": "not_ruled",
        "text": rewrite,
        "unknown_words": ["qzxv"],
    }


REPAIR_RULES = (
    "### Sửa xe\n| Luật | Mặc định |\n|---|---|\n"
    "| Lượt xe, phiếu | mỗi dòng sổ sửa xe là **một lượt xe** (một phiếu), kể cả phiếu bảo "
    "hành; một xe tới hai lần là hai lượt. **Số phiếu, số lượt xe** của tiệm hay của một thợ "
    "tính cả phiếu bảo hành. Tiền trung bình một phiếu chia cho số phiếu **có thu tiền** "
    "(không tính phiếu bảo hành) |\n"
)


def test_repair_book_reads_tickets_and_abstains_on_industry_measures(
    synthetic_split, tmp_path, rulebook_text
):
    from src.agent.rule_ops import answer_by_rule
    from src.semantics.answerability import prepare_semantics
    from tests.test_planner_execution import question

    path = synthetic_split / "data/shop_a/sales.csv"
    rows = [
        ["Ngày", "Xe", "Làm gì", "Thợ", "Tiền", "Ghi chú"],
        ["01/01/2026", "wave 59B1-111.11", "thay nhớt", "a Đức", "120000", ""],
        ["01/01/2026", "vario 59B1-222.22", "bugi", "Đạt", "65000", "hẹn chiều lấy"],
        ["02/01/2026", "wave 59B1-111.11", "thay nhớt", "a Đức", "0", "bảo hành"],
    ]
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)
    (path.parent / "README.md").write_text(
        "# Dữ liệu shop_a\n\nSổ sửa xe chủ tiệm tự ghi: mỗi dòng một phiếu, không ghi mã "
        "phiếu. Tiền là tổng cả phiếu.\n\n## Cột trong `sales.csv`\n"
        "- **Ngày**: ngày sửa\n- **Xe**: dòng xe và biển số ghi chung một ô\n"
        "- **Làm gì**: các việc đã làm\n- **Thợ**: thợ làm phiếu, ghi tên gọi\n"
        "- **Tiền**: tổng tiền cả phiếu\n- **Ghi chú**: ghi chú\n\n"
        "## Quy ước\n- Dữ liệu có đến hết 03/01/2026.\n"
    )
    (synthetic_split / "rulebook.md").write_text(rulebook_text + NOTE_RULE + REPAIR_RULES)
    labels = {
        "Ngày": "ordered_at",
        "Xe": "other",
        "Làm gì": "other",
        "Thợ": "other",
        "Tiền": "order_payable",
        "Ghi chú": "order_note",
    }
    declared = {**DECLARED, "row_grain": "ledger_no_order_id", "numeric_convention": "unknown"}
    proposal = {
        "tables": {"sales.csv": {"role": "order_headers", "columns": labels}},
        "declarations": declared,
    }
    model = FakeModel(proposal, {"bảo hành": "not_status"})  # only outcome-like notes ask
    context = prepare_business(load_split(synthetic_split), "shop_a")
    prepared = prepare_facts(context, tmp_path / "facts", propose=model)
    assert prepared.summary["status"] == "ready"
    context.prepared = prepared
    prepare_semantics(context)

    def ask(text):

        result = answer_by_rule(question(question=text), context)
        return None if result is None else result["answer"].get("answer")

    assert ask("Tháng 1/2026 tiệm có bao nhiêu phiếu sửa?") == 3  # warranty ticket counts
    assert ask("Doanh thu tháng 1/2026?") == 185000
    # Per-mechanic totals and the paid-ticket average follow industry rules: abstain.
    assert ask("Tháng 1/2026 tiệm có bao nhiêu phiếu bảo hành?") == 1
    assert ask("Tháng 1/2026 bình quân 1 phiếu bao nhiêu tiền?") == 92500  # paid tickets only
    assert ask("Tháng 1/2026 phiếu bảo hành chiếm bao nhiêu %?") == 50.0
    assert ask("Tháng 1/2026 có bao nhiêu chiếc xe khác nhau?") == 2
    # No mechanic list: a nickname cannot be resolved, so mechanic questions abstain.
    assert ask("Tháng 1/2026 thợ Đức làm được bao nhiêu tiền?") is None

    (path.parent / "tho.csv").write_text(
        "Họ tên,Hoa hồng (%)\nLương Chi Đức,25\nHà Loan Đạt,30\n", encoding="utf-8"
    )
    readme = (path.parent / "README.md").read_text(encoding="utf-8")
    (path.parent / "README.md").write_text(
        readme.replace(
            "## Quy ước",
            "## Cột trong `tho.csv`\n- **Họ tên**: họ tên thợ\n"
            "- **Hoa hồng (%)**: phần trăm hoa hồng của thợ\n\n## Quy ước",
        ),
        encoding="utf-8",
    )
    proposal["tables"]["tho.csv"] = {
        "role": "other",
        "columns": {"Họ tên": "other", "Hoa hồng (%)": "other"},
    }
    model = FakeModel(proposal, {"bảo hành": "not_status"})
    context = prepare_business(load_split(synthetic_split), "shop_a")
    context.prepared = prepare_facts(context, tmp_path / "facts2", propose=model)
    prepare_semantics(context)
    assert ask("Tháng 1/2026 thợ Lương Chi Đức làm được mấy lượt xe?") == 2
    assert ask("Tháng 1/2026 thợ Lương Chi Đức được bao nhiêu hoa hồng?") == 30000
    assert ask("Tháng 1/2026 thợ nào làm nhiều lượt xe nhất?") == "Lương Chi Đức"
    assert ask("Tháng 1/2026 thợ Hà Loan Đạt làm ra bao nhiêu tiền?") == 65000


def test_peak_hour_is_the_starting_hour_with_most_orders(semantic_source, rulebook_text):
    from src.agent.rule_ops import answer_by_rule
    from tests.test_planner_execution import question

    rule = (
        "| Giờ cao điểm | khung **một giờ tròn** theo giờ tạo đơn (từ h giờ đến trước h + 1 "
        "giờ) có nhiều đơn nhất trong kỳ. Đáp án là **giờ bắt đầu khung**, type `number` |\n"
    )
    rows = [
        ["1", "01/01/2026 09:59", "Xong", "10", "A"],
        ["2", "01/01/2026 10:00", "Xong", "10", "A"],
        ["3", "02/01/2026 10:59", "Xong", "10", "B"],
        ["4", "02/01/2026 11:30", "Huỷ", "10", "B"],
        ["5", "03/01/2026 11:10", "Huỷ", "10", "B"],
        ["6", "03/01/2026 09:05", "Xong", "10", "C"],
        ["7", "03/01/2026 09:40", "Xong", "10", "C"],
    ]
    context = semantic_source(rows=rows, rules=rulebook_text + RANKING_RULES + rule)
    result = answer_by_rule(question(question="Tháng 1/2026 giờ cao điểm là mấy giờ?"), context)
    # 09h holds three valid orders, 10h two; the cancelled 11h orders do not count.
    assert result is not None and result["answer"]["answer"] == 9
    tie = semantic_source(rows=rows[:-1], rules=rulebook_text + RANKING_RULES + rule)
    assert answer_by_rule(question(question="Tháng 1/2026 giờ cao điểm là mấy giờ?"), tie) is None


PHONE_RULE = (
    "### Cửa hàng điện thoại\n| Luật | Mặc định |\n|---|---|\n"
    "| Thu máy cũ | **Khách phải trả** là tiền của đơn sau bớt và sau trừ máy cũ, gồm cả phần "
    "còn nợ. Tiền thu máy cũ **không** trừ vào doanh thu. Tổng khách phải trả, tiền thu máy cũ "
    "của một kỳ chỉ tính đơn không huỷ, không trả (§1) |\n"
)


def test_trade_in_and_payable_totals_skip_cancelled_orders(semantic_source, rulebook_text):
    from src.agent.rule_ops import answer_by_rule
    from tests.test_planner_execution import question

    rows = [
        ["1", "01/01/2026", "Xong", "5000000", "Máy A", "1000000", "3900000"],
        ["1", "01/01/2026", "Xong", "100000", "Ốp", "", ""],
        ["2", "02/01/2026", "Huỷ", "4000000", "Máy B", "500000", "3500000"],
        ["3", "03/01/2026", "Xong", "200000", "Cáp", "", "200000"],
    ]
    context = semantic_source(
        rows=rows,
        rules=rulebook_text + PHONE_RULE,
        extra_columns=["Thu máy cũ", "Khách trả"],
        readme_extra=(
            "\n## Cột trong `sales.csv`\n"
            "- **Thu máy cũ**: tiền thu máy cũ của khách, trừ vào tiền đơn\n"
            "- **Khách trả**: số tiền khách trả cho cả đơn, đã trừ bớt và thu máy cũ\n"
        ),
    )

    def ask(text):
        result = answer_by_rule(question(question=text), context)
        return None if result is None else result["answer"].get("answer")

    assert ask("Tháng 1/2026 tiền thu máy cũ cộng lại là bao nhiêu?") == 1000000
    assert ask("Tháng 1/2026 tổng tiền khách phải trả là bao nhiêu?") == 4100000


BABY_RULE = (
    "### Mẹ và bé\n| Luật | Mặc định |\n|---|---|\n"
    "| Tiền giảm voucher, tiền giảm nhờ dùng điểm, phí ship | trả **số dương**; cộng theo "
    "**ngày của hoá đơn**; chỉ tính hoá đơn không huỷ, không trả |\n"
)


def test_voucher_points_and_shipping_totals_are_positive_on_valid_invoices(
    semantic_source, rulebook_text
):
    from src.agent.rule_ops import answer_by_rule
    from tests.test_planner_execution import question

    rows = [
        ["1", "01/01/2026", "Xong", "300", "Sữa"],
        ["1", "01/01/2026", "Xong", "-20", "Voucher"],
        ["1", "01/01/2026", "Xong", "15", "Phí ship"],
        ["2", "02/01/2026", "Huỷ", "200", "Bỉm"],
        ["2", "02/01/2026", "Huỷ", "-50", "Voucher"],
        ["3", "03/01/2026", "Xong", "100", "Bỉm"],
        ["3", "03/01/2026", "Xong", "-5", "Dùng 50 điểm"],
    ]
    context = semantic_source(rows=rows, rules=rulebook_text + BABY_RULE)

    def ask(text):
        result = answer_by_rule(question(question=text), context)
        return None if result is None else result["answer"].get("answer")

    assert ask("Tháng 1/2026 shop giảm voucher cho khách hết bao nhiêu tiền?") == 20
    assert ask("Tháng 1/2026 tiệm thu phí ship được bao nhiêu tiền?") == 15
    assert ask("Tháng 1/2026 tiền giảm nhờ dùng điểm là bao nhiêu?") == 5
    assert ask("Doanh thu tháng 1/2026?") == 400  # adjustments are not revenue (§1)


def test_new_members_are_counted_from_the_registration_column():
    from src.agent.industry import registrations

    def row(n, values):
        return SourceRow(n, n, {i: Cell(v, "text", {}) for i, v in enumerate(values, 1) if v})

    rows = [row(1, ["Tên KH", "Ngày đăng ký"]), row(2, ["Lan", "03/01/2026"])]
    rows += [row(3, ["Minh", "28/12/2025"]), row(4, ["Hoa", "05/01/2026"])]
    profile = {
        "source_id": "kh",
        "source_ref": {"file": "data/b/QuanLy.xlsx", "table": "KhachHang"},
        "header_rows": [1],
        "columns": [
            {"display_name": "Tên KH", "position": 1},
            {"display_name": "Ngày đăng ký", "position": 2},
        ],
    }
    table = SimpleNamespace(metadata={}, iter_rows=lambda: iter(rows))
    context = SimpleNamespace(
        catalog={"readme": "", "tables": [profile]}, source_table=lambda _id: table
    )
    assert registrations(context) == ["2026-01-03", "2025-12-28", "2026-01-05"]


def test_abbreviation_sheet_expands_names_and_supplies_item_kinds(
    synthetic_split, tmp_path, rulebook_text
):
    from src.agent.rule_ops import answer_by_rule
    from src.semantics.answerability import prepare_semantics
    from tests.test_planner_execution import question

    shop = synthetic_split / "data/shop_a"
    with (shop / "sales.csv").open("w", newline="") as handle:
        csv.writer(handle).writerows(
            [
                ["Số phiếu", "Ngày lập", "Tình trạng", "Tiền dòng", "Mặt hàng", "SL"],
                ["P1", "01/01/2026", "Xong", "3000000", "IP11 cũ", "1"],
                ["P1", "01/01/2026", "Xong", "90000", "Cáp C", "1"],
                ["P2", "02/01/2026", "Xong", "2500000", "iPhone 11 cũ 64GB", "1"],
                ["P3", "03/01/2026", "Xong", "100000", "Cáp Type-C 1m", "1"],
            ]
        )
    (shop / "ghi_tat.csv").write_text(
        "Ghi tắt,Tên hàng,Loại\nIP11 cũ,iPhone 11 cũ 64GB,máy cũ\nCáp C,Cáp Type-C 1m,phụ kiện\n",
        encoding="utf-8",
    )
    (shop / "README.md").write_text(
        "# Dữ liệu shop_a\n\nSổ bán hàng.\n\n## Quy ước\n- Dữ liệu có đến hết 03/01/2026.\n"
    )
    (synthetic_split / "rulebook.md").write_text(rulebook_text)
    sales = {
        "Số phiếu": "order_id",
        "Ngày lập": "ordered_at",
        "Tình trạng": "status",
        "Tiền dòng": "line_net_amount",
        "Mặt hàng": "item_name",
        "SL": "quantity",
    }
    # The small lookup sheet stays unresolved (as in shop exports), so no proposal entry.
    proposal = {
        "tables": {"sales.csv": {"role": "order_lines", "columns": sales}},
        "declarations": {**DECLARED, "numeric_convention": "unknown"},
    }
    context = prepare_business(load_split(synthetic_split), "shop_a")
    context.prepared = prepare_facts(context, tmp_path / "facts", propose=FakeModel(proposal))
    assert context.prepared.summary["status"] == "ready"
    prepare_semantics(context)

    def ask(text):
        result = answer_by_rule(question(question=text), context)
        return None if result is None else result["answer"].get("answer")

    assert ask("Tháng 1/2026 nhóm máy cũ bán được bao nhiêu tiền?") == 5500000
    assert ask("Tháng 1/2026 nhóm phụ kiện chiếm bao nhiêu % doanh thu?") == 3.34


PHARMACY_RULE = (
    "### Nhà thuốc\n| Luật | Mặc định |\n|---|---|\n"
    "| Thuốc kê đơn | **Thuốc kê đơn** = sản phẩm ghi kê đơn trong danh mục thuốc; mọi sản "
    "phẩm còn lại là **hàng không kê đơn**. Tỉ lệ kê đơn tính theo doanh thu |\n"
    "| Doanh thu theo hoạt chất | doanh thu (§1) các thuốc có hoạt chất ghi **đúng như vậy** "
    "trong danh mục thuốc. Thuốc phối hợp nhiều hoạt chất là **một loại riêng**, không tính "
    "vào từng hoạt chất trong đó |\n"
)


def test_prescription_and_ingredient_revenue_follow_the_drug_catalog(
    semantic_source, rulebook_text, monkeypatch
):
    from src.agent import pharmacy
    from src.agent.rule_ops import answer_by_rule
    from tests.test_planner_execution import question

    catalog = {
        "para 500": ("paracetamol", False),
        "para cam": ("paracetamol + phenylephrin", False),
        "amox 500": ("amoxicilin", True),
    }
    monkeypatch.setattr(pharmacy, "drug_catalog", lambda context: ("name", catalog))
    rows = [
        ["1", "01/01/2026", "Xong", "100", "Para 500"],
        ["1", "01/01/2026", "Xong", "40", "Para Cam"],
        ["2", "02/01/2026", "Xong", "60", "Amox 500"],
        ["3", "02/01/2026", "Huỷ", "500", "Amox 500"],
        ["4", "03/01/2026", "Xong", "200", "Khẩu trang"],
    ]
    context = semantic_source(rows=rows, rules=rulebook_text + PHARMACY_RULE)
    context.rules.rules.setdefault("prescription_share", None)
    context.rules.rules.setdefault("active_ingredient", None)

    def ask(text):
        result = answer_by_rule(question(question=text), context)
        return None if result is None else result["answer"].get("answer")

    assert ask("Tháng 1/2026 tiền bán thuốc kê đơn là bao nhiêu?") == 60
    assert ask("Tháng 1/2026 dthu hàng hok kê đơn là bn?") == 340
    assert ask("Trừ thuốc kê đơn ra, tháng 1/2026 các mặt hàng còn lại bán được bao nhiêu?") == 340
    assert ask("Tháng 1/2026 thuốc kê đơn chiếm mấy % tổng doanh thu?") == 15.0
    # A combination drug is its own kind, never counted under one of its ingredients.
    assert ask("Thuốc có hoạt chất Paracetamol tháng 1/2026 bán được bao nhiêu tiền?") == 100
    assert (
        ask("Doanh thu thuốc hoạt chất Paracetamol + Phenylephrin tháng 1/2026 là bao nhiêu?") == 40
    )
    assert ask("Thuốc có hoạt chất Ibuprofen tháng 1/2026 bán được bao nhiêu tiền?") is None


SHIFT_RULE = (
    "| Ca sáng, ca chiều | theo giờ tạo đơn: ca sáng 06:00–13:59, ca chiều 14:00–21:59. So ca "
    "chiều với ca sáng: mốc là **ca sáng** |\n"
)


def test_shift_questions_become_plans_with_the_morning_baseline(
    semantic_source, rulebook_text, monkeypatch
):
    from src.agent import execution
    from src.agent.rule_ops import _shift_plan
    from tests.test_planner_execution import question

    context = semantic_source(rules=rulebook_text.replace("## 2.", SHIFT_RULE + "## 2."))
    drafts = []

    def capture(draft, q, ctx):
        drafts.append(draft)
        return {"answer": {"type": "number"}, "plan": {}}

    monkeypatch.setattr(execution, "execute_plan", capture)

    def plan(text):
        drafts.clear()
        _shift_plan(question(question=text), context)
        return drafts[0] if drafts else None

    single = plan("Tháng 1/2026 rieng ca sang ban duoc tong cong bao nhieu tien?")
    assert [(a["metric_id"], a["filters"]) for a in single["aggregations"]] == [
        ("revenue", ["shift=morning"])
    ]
    count = plan("Tháng 1/2026 ca chiều bán được mấy đơn?")
    assert count["aggregations"][0]["metric_id"] == "order_count"
    change = plan("Tháng 1/2026 doanh thu ca chiều tăng hay giảm bao nhiêu % so với ca sáng?")
    assert [a["filters"] for a in change["aggregations"]] == [
        ["shift=afternoon"],
        ["shift=morning"],
    ]
    assert change["derived_measures"][0]["operator"] == "percent_change"
    assert change["output"]["expected_type"] == "percent"
    assert plan("Tháng 1/2026 nhân viên A ca sáng bán được bao nhiêu tiền?") is None
