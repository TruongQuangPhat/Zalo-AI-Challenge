"""Behavioral coverage for native-grain header, ledger and relational sales sources."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.agent.execution import execute_plan
from src.data.catalog import prepare_business
from src.data.normalization import prepare_facts
from src.data.split import load_split
from src.semantics.answerability import prepare_semantics
from tests.test_planner_execution import draft, period_draft, question


def csv_file(folder: Path, name: str, header: list[str], rows: list[list]):
    with (folder / name).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def prepare(root, tmp_path, rules, readme):
    (root / "rulebook.md").write_text(rules)
    (root / "data/shop_a/README.md").write_text(readme)
    c = prepare_business(load_split(root), "shop_a")
    prepare_facts(c, tmp_path / "facts")
    prepare_semantics(c)
    return c


def test_header_merchandise_revenue_is_not_payable_or_a_fake_line(
    synthetic_split, tmp_path, rulebook_text
):
    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    csv_file(
        folder,
        "invoices.csv",
        ["id", "date", "status", "net_amount", "payable"],
        [["a", "01/01/2026", "Xong", "101", "900"], ["b", "02/01/2026", "Huỷ", "50", "1000"]],
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        """Mỗi dòng một đơn hàng, không có chi tiết mặt hàng.
- **id**: order id
- **date**: order date
- **status**: order status
- **net_amount**: order net merchandise amount
- **payable**: tổng khách phải trả
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
""",
    )
    assert c.prepared.summary["shape"] == "order_headers"
    assert list(c.prepared.iter_records("order_lines")) == []
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 101
    assert (
        execute_plan(
            period_draft("average_order_value", ["revenue", "order_count"]),
            question(question="Giá trị trung bình một đơn tháng này?"),
            c,
        )["answer"]["answer"]
        == 101
    )


def joined_source(root, tmp_path, rules, *, orphan=False, conflict=False):
    folder = root / "data/shop_a"
    (folder / "sales.csv").unlink()
    csv_file(
        folder,
        "orders.csv",
        ["order_id", "order_date", "order_status", "customer_id"],
        [["a", "01/01/2026", "Xong", "c1"], ["b", "02/01/2026", "Đang giao", ""]],
    )
    csv_file(
        folder,
        "lines.csv",
        ["order_id", "product_id", "net_amount", "line_number"],
        [
            ["a", "p1", "20", "1"],
            ["a", "p1", "20", "1"],
            ["a", "p2", "30", "2"],
            ["b", "p2", "50", "1"],
        ]
        + ([["orphan", "p1", "999", "1"]] if orphan else []),
    )
    csv_file(
        folder,
        "products.csv",
        ["product_id", "product_name", "product_group"],
        [["p1", "Tea", "Tea"], ["p2", "Cake", "Cake"]]
        + ([["p1", "Other", "Cake"]] if conflict else []),
    )
    csv_file(
        folder,
        "customers.csv",
        ["customer_id", "product_name", "product_group"],
        [["c1", "Lan", "VIP"]],
    )
    csv_file(
        folder, "payments.csv", ["order_id", "paid_amount"], [["a", "10"], ["a", "40"], ["b", "50"]]
    )
    return prepare(
        root,
        tmp_path,
        rules,
        """Dữ liệu nối qua mã. Mỗi bảng một file.
## Cột trong `orders.csv`
- **order_id**: order id
- **order_date**: order date
- **order_status**: order status
- **customer_id**: customer id
## Cột trong `lines.csv`
- **order_id**: order id
- **product_id**: product id
- **net_amount**: line net amount
## Cột trong `products.csv`
- **product_id**: product id
- **product_name**: product name
- **product_group**: product group
## Cột trong `customers.csv`
- **customer_id**: customer id
- **product_name**: customer name
- **product_group**: customer group
## Cột trong `payments.csv`
- **order_id**: order id
- **paid_amount**: số tiền thu
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
""",
    )


def test_header_detail_master_joins_do_not_multiply_money_by_payments(
    synthetic_split, tmp_path, rulebook_text
):
    c = joined_source(synthetic_split, tmp_path, rulebook_text)
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 100
    assert (
        execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), c)["answer"][
            "answer"
        ]
        == 2
    )
    for field, label, target in [
        ("item_group", "nhóm tea", 20),
        ("customer_group", "khách nhóm vip", 50),
    ]:
        result = execute_plan(
            period_draft(
                "share",
                ["revenue", "revenue"],
                filters=[field + "=" + ("tea" if field == "item_group" else "vip")],
            ),
            question(
                question=f"Tỉ trọng doanh thu của {label} trong tổng doanh thu "
                "tháng này bao nhiêu %?"
            ),
            c,
        )
        assert result["answer"]["answer"] == target
        assert result["execution"]["denominator_value"] == 100


def test_orphan_detail_blocks_complete_aggregate(synthetic_split, tmp_path, rulebook_text):
    c = joined_source(synthetic_split, tmp_path, rulebook_text, orphan=True)
    assert execute_plan(draft(), question(), c)["answer"]["type"] == "abstain"
    assert "orphan_detail" in c.prepared.summary["issue_counts"]


def test_conflicting_master_never_selects_an_arbitrary_group(
    synthetic_split, tmp_path, rulebook_text
):
    c = joined_source(synthetic_split, tmp_path, rulebook_text, conflict=True)
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 100
    result = execute_plan(
        period_draft("share", ["revenue", "revenue"], filters=["item_group=tea"]),
        question(question="Tỉ trọng của nhóm Tea trong doanh thu tháng này bao nhiêu %?"),
        c,
    )
    assert result["answer"]["type"] == "abstain"
    assert "item_group_share" in c.prepared.summary["blocking_metrics"]


def test_ledger_duplicates_are_events_not_orders(synthetic_split, tmp_path, rulebook_text):
    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    # XLSX grain is represented by CSV here to isolate semantics from the reader;
    # the identical mapper also consumes native XLSX cells in reader regressions.
    csv_file(
        folder,
        "ledger.csv",
        ["date", "order_status", "amount", "item"],
        [
            ["01/01/2026", "Xong", "20", "A"],
            ["01/01/2026", "Xong", "20", "A"],
            ["02/01/2026", "Huỷ", "1000", "B"],
        ],
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        """Mỗi dòng một mặt hàng khách mua, không ghi mã đơn, không ghi tên khách.
## Cột trong `ledger.csv`
- **date**: order date
- **order_status**: order status
- **amount**: line net amount
- **item**: product name
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
""",
    )
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 40
    assert list(c.prepared.iter_records("orders")) == []
    assert (
        execute_plan(draft("order_count"), question(question="Số đơn tháng này?"), c)["answer"][
            "type"
        ]
        == "unanswerable"
    )


@pytest.mark.parametrize(
    "comparison",
    [
        "Doanh thu ca chiều tháng này hơn ca sáng bao nhiêu phần trăm?",
        "Doanh thu ca chiều tháng này tăng hay giảm bao nhiêu % so với ca sáng?",
        "Tháng này, dthu ca chiều tăng hay giảm bao nhiu % so vs ca sáng?",
    ],
)
def test_sqlite_clock_filters_use_half_open_shift_boundaries(
    synthetic_split, tmp_path, rulebook_text, comparison
):
    import sqlite3

    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    with sqlite3.connect(folder / "pos.sqlite") as conn:
        conn.execute("CREATE TABLE don_hang (MaHoaDon TEXT,ThoiGianLap TEXT,TinhTrang TEXT)")
        conn.execute("CREATE TABLE chi_tiet_don (MaHoaDon TEXT,TenHang TEXT,TriGia INTEGER)")
        for i, (clock, money) in enumerate(
            [("13:59:59.999999", 20), ("14:00:00", 30), ("21:59:59.999999", 50), ("22:00:00", 100)]
        ):
            conn.execute(
                "INSERT INTO don_hang VALUES (?,?,?)", (str(i), "2026-01-01 " + clock, "completed")
            )
            conn.execute("INSERT INTO chi_tiet_don VALUES (?,?,?)", (str(i), "Tea", money))
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        "File máy POS xuất ra.\nDữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.\n",
    )
    p = draft()
    p["aggregations"][0]["filters"] = ["shift=afternoon"]
    r = execute_plan(p, question(question="Doanh thu ca chiều tháng này?"), c)
    assert r["answer"]["answer"] == 80
    p = period_draft("percent_change", ["revenue", "revenue"])
    p["aggregations"][0]["filters"] = ["shift=afternoon"]
    p["aggregations"][1]["filters"] = ["shift=morning"]
    p["output"]["expected_type"] = "percent"
    r = execute_plan(p, question(question=comparison), c)
    assert r["answer"]["answer"] == 300
    assert r["execution"]["baseline_value"] == 20


def test_csv_serial_epoch_requires_date_role_and_one_source_bound_solution():
    from src.data.normalization.values import ParseError, serial_system

    assert serial_system(["46145"], "19/08/2026") == "1900"
    with pytest.raises(ParseError):
        serial_system(["46145"], None)
    with pytest.raises(ParseError):
        serial_system(["30000"], "19/08/2026")


def test_declared_continuations_read_all_sheets_and_dedup_export_copies(
    synthetic_split, tmp_path, rulebook_text
):
    from tests.test_normalization import xlsx_order_source

    c = xlsx_order_source(
        synthetic_split,
        rulebook_text,
        extra_sheet=True,
        readme_change=lambda s: s + "\nFile chia mỗi tháng một sheet.\n",
    )
    prepare_facts(c, tmp_path / "multi-facts")
    prepare_semantics(c)
    result = execute_plan(draft(), question(), c)
    assert result["answer"]["answer"] == 1050
    assert len(c.prepared.summary["sources"]) == 2
    assert c.prepared.summary["counts"]["lineage"] == 12
    assert c.prepared.summary["counts"]["orders"] == 2


def test_sales_preparation_preserves_existing_exact_snapshot_lookup(
    synthetic_split, tmp_path, rulebook_text
):
    from tests.test_ingestion import text_cell, workbook
    from tests.test_stock_snapshot import draft as stock_draft
    from tests.test_stock_snapshot import question as stock_question

    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    csv_file(
        folder,
        "orders.csv",
        ["order_id", "date", "status", "amount", "item"],
        [["a", "01/01/2026", "Xong", "100", "Tea"]],
    )
    body = (
        '<sheetData><row r="1">'
        + "".join(
            text_cell(f"{c}1", v)
            for c, v in zip("ABCD", ["Mã hàng", "Tên hàng", "ĐVT", "Tồn cuối kỳ"], strict=True)
        )
        + '</row><row r="2">'
        + "".join(
            text_cell(f"{c}2", v)
            for c, v in zip("ABC", ["P01", "Sản phẩm Á 100g", "hộp"], strict=True)
        )
        + '<c r="D2"><v>7.125</v></c></row></sheetData>'
    )
    workbook(folder / "snapshot.xlsx", [("BaoCaoTonKho", "visible", body)])
    readme = """Dữ liệu nối qua mã.
## Cột trong `orders.csv`
- **order_id**: order id
- **date**: order date
- **status**: order status
- **amount**: line net amount
- **item**: product name
## Quy ước
- Dữ liệu có đến hết 03/01/2026.
- **Tồn kho**: **Tồn cuối kỳ** là tồn cuối ngày cuối có dữ liệu, theo đơn vị cơ bản.
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
"""
    c = prepare(synthetic_split, tmp_path, rulebook_text, readme)
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 100
    assert execute_plan(stock_draft(), stock_question(), c)["answer"]["answer"] == __import__(
        "decimal"
    ).Decimal("7.125")


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["45,000", "100,500"], "comma_grouped"),
        (["45.000", "100.500"], "dot_grouped"),
        (["45,000 VND", "100,500 đ"], "comma_grouped"),
        (["45,000", "100.500"], None),
        (["45,000", "bad"], None),
    ],
)
def test_approved_money_grouping_requires_consistent_complete_column(tokens, expected):
    from src.data.normalization.values import consistent_grouping

    assert consistent_grouping(tokens) == expected


def test_native_excel_clock_uses_declared_whole_second_display_precision(
    synthetic_split, tmp_path, rulebook_text
):
    import zipfile

    from tests.test_ingestion import text_cell, workbook

    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    header = (
        '<row r="1">'
        + "".join(
            text_cell(f"{col}1", name)
            for col, name in zip(
                "ABCD", ["Mã hóa đơn", "Thời gian", "Trạng thái", "Thời gian tạo"], strict=True
            )
        )
        + "</row>"
    )
    row = (
        '<row r="2">'
        + text_cell("A2", "a")
        + text_cell("B2", "01/01/2026")
        + text_cell("C2", "Hoàn thành")
        + '<c r="D2" s="1"><v>46023.58333333333</v></c></row>'
    )
    workbook(
        folder / "orders.xlsx",
        [("Orders", "visible", "<sheetData>" + header + row + "</sheetData>")],
    )
    path = folder / "orders.xlsx"
    with zipfile.ZipFile(path) as z:
        members = {name: z.read(name) for name in z.namelist()}
    members["xl/styles.xml"] = (
        b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<numFmts><numFmt numFmtId="164" formatCode="yyyy-mm-dd h:mm:ss"/></numFmts>'
        b'<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>'
    )
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    dheader = (
        '<row r="1">'
        + "".join(
            text_cell(f"{col}1", name)
            for col, name in zip("ABC", ["Mã hóa đơn", "Tên hàng", "Thành tiền"], strict=True)
        )
        + "</row>"
    )
    drow = (
        '<row r="2">'
        + text_cell("A2", "a")
        + text_cell("B2", "Tea")
        + '<c r="C2"><v>100</v></c></row>'
    )
    workbook(
        folder / "details.xlsx",
        [("Details", "visible", "<sheetData>" + dheader + drow + "</sheetData>")],
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        "File máy POS xuất ra.\nDữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.\n",
    )
    p = draft()
    p["aggregations"][0]["filters"] = ["shift=afternoon"]
    assert (
        execute_plan(p, question(question="Doanh thu ca chiều tháng này?"), c)["answer"]["answer"]
        == 100
    )


def test_shift_comparison_preserves_explicit_direction_and_schema_types():
    from src.agent.plan import _schema, generation_schema

    p = period_draft("percent_change", ["revenue", "revenue"])
    p["time_windows"][0]["id"] = "period"
    for a in p["aggregations"]:
        a["window_ref"] = "period"
    p["aggregations"][0].update(id="total", filters=["shift=morning"])
    p["aggregations"][1].update(id="baseline_total", filters=["shift=afternoon"])
    p["derived_measures"][0].update(id="delta", current_ref="total", baseline_ref="baseline_total")
    p["output"] = {"expected_type": "percent", "result_refs": ["delta"]}
    q = question(question="Doanh thu ca sáng tháng này so với ca chiều tăng bao nhiêu phần trăm?")
    _schema(p, generation_schema(q))

    from src.agent.plan import _period_measure

    for intro in ["", "Giúp tôi xem ", "Tháng này "]:
        q = question(
            question=intro + "so với ca chiều, doanh thu ca sáng tháng này chênh bao nhiêu %?"
        )
        assert _period_measure(q)["operand_filters"] == [["shift=morning"], ["shift=afternoon"]]


@pytest.mark.parametrize(
    "text",
    [
        "Tháng này có bao nhiêu đơn có doanh thu từ 50.000 đồng trở lên?",
        "Tháng này có mấy ngày doanh thu đạt 1.000.000 đồng?",
        "Doanh thu cuar trà sữa tháng này?",
        "Đếm số đơn đã huỷ và trả hàng tháng này?",
    ],
)
def test_scalar_total_guard_rejects_dropped_conditions(text):
    from src.agent.plan import _check_question_scope
    from src.jsonio import ContractError

    with pytest.raises(ContractError):
        _check_question_scope(question(question=text))


def test_relational_contiguous_headers_use_runtime_rule_and_reset_at_new_order(
    synthetic_split, tmp_path, rulebook_text
):
    folder = synthetic_split / "data/shop_a"
    csv_file(
        folder,
        "sales.csv",
        ["order_id", "date", "status", "amount", "item"],
        [
            ["a", "01/01/2026", "Xong", "20", "A"],
            ["", "", "", "30", "B"],
            ["b", "02/01/2026", "Huỷ", "900", "B"],
        ],
    )
    csv_file(
        folder,
        "products.csv",
        ["product_id", "product_name", "product_group"],
        [["a", "A", "Tea"], ["b", "B", "Other"]],
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        """Mỗi dòng một sản phẩm.
## Cột trong `sales.csv`
- **order_id**: order id
- **date**: order date
- **status**: order status
- **amount**: line net amount
- **item**: product name
## Cột trong `products.csv`
- **product_id**: product id
- **product_name**: product name
- **product_group**: product group
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
""",
    )
    assert c.prepared.summary["status"] == "ready"
    assert c.prepared.summary["counts"]["orders"] == 2
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 50


@pytest.mark.parametrize(
    ("name", "ambiguous", "expected"),
    [
        ("CA-PHÊ", False, 100),
        ("Ca phe", False, 100),
        ("Ca phe", True, None),
        ("Cả phê", False, None),
    ],
)
def test_name_only_group_join_requires_one_code_and_records_approved_policy(
    synthetic_split, tmp_path, rulebook_text, name, ambiguous, expected
):
    folder = synthetic_split / "data/shop_a"
    csv_file(
        folder,
        "sales.csv",
        ["order_id", "date", "status", "amount", "item"],
        [["a", "01/01/2026", "Xong", "45,000 VND", name]],
    )
    csv_file(
        folder,
        "products.csv",
        ["product_id", "product_name", "product_group"],
        [["p1", "Ca-phê", "Tea"]] + ([["p2", "Cá phe", "Tea"]] if ambiguous else []),
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        """Mỗi dòng một sản phẩm.
## Cột trong `sales.csv`
- **order_id**: order id
- **date**: order date
- **status**: order status
- **amount**: line net amount
- **item**: product name
## Cột trong `products.csv`
- **product_id**: product id
- **product_name**: product name
- **product_group**: product group
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
""",
    )
    p = period_draft("share", ["revenue", "revenue"], filters=["item_group=tea"])
    r = execute_plan(
        p, question(question="Tỉ trọng nhóm Tea trong doanh thu tháng này bao nhiêu %?"), c
    )
    assert execute_plan(draft(), question(), c)["answer"]["answer"] == 45000
    if expected is None:
        assert r["answer"]["type"] == "abstain"
    else:
        assert r["answer"]["answer"] == expected
    mapping = c.prepared.summary["mapping"]
    assert mapping["money_basis"] == "user_2026_11_04_consistent_three_digit_groups"
    assert mapping["money_warning"]
    assert mapping["name_matching_basis"] == "user_2026_11_04_unaccented_unique_catalog_code"
    assert mapping["name_matching_warning"]


def test_explicit_cancelled_count_uses_existing_metric_and_rejects_successful_count(
    synthetic_split, tmp_path, rulebook_text
):
    from src.agent.plan import _schema, generation_schema
    from src.jsonio import ContractError

    c = joined_source(synthetic_split, tmp_path, rulebook_text)
    q = question(question="Số đơn đã huỷ tháng này là bao nhiêu?")
    p = draft("cancelled_order_count")
    _schema(p, generation_schema(q))
    r = execute_plan(p, q, c)
    assert r["answer"]["answer"] == 0
    assert r["execution"]["orders_selected"] == 0
    with pytest.raises(ContractError):
        execute_plan(draft("order_count"), q, c)


def test_handwritten_late_negative_return_keeps_original_order_date_and_clock(
    synthetic_split, tmp_path, rulebook_text
):
    folder = synthetic_split / "data/shop_a"
    csv_file(
        folder,
        "sales.csv",
        ["id", "date", "status", "item", "amount", "quantity", "hour"],
        [
            ["a", "03/02/2026", "Trả hàng", "A", "-20", "-1", "18:00"],
            ["a", "01/01/2026", "Xong", "A", "100", "5", "07:00"],
            ["d", "02/01/2026", "Xong", "Cộng", "7", "1", "13:00"],
            ["b", "02/01/2026", "Xong", "B", "30", "2", "14:00"],
            ["", "", "", "Cộng", "37", "", ""],
            ["", "", "", "Ghi chú: tiền tính bằng đồng", "", "", ""],
            ["c", "02/02/2026", "Xong", "A", "40", "2", "13:00"],
        ],
    )
    csv_file(
        folder,
        "BangGia.csv",
        ["product_name", "date", "unit_price"],
        [["A", "01/01/2026", "100"], ["B", "01/01/2026", "15"]],
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        """Sổ bán hàng ghi tay.
## Cột trong `sales.csv`
- **id**: mã đơn
- **date**: ngày bán
- **status**: trạng thái đơn
- **item**: tên hàng
- **amount**: thành tiền sau giảm
- **quantity**: số lượng
- **hour**: giờ đặt đơn (dòng trả hàng: lúc trả)
## Cột trong `BangGia.csv`
- **product_name**: tên hàng
- **date**: ngày bắt đầu áp dụng giá
- **unit_price**: giá bán áp dụng từ ngày đó (đơn vị cơ bản)
## Quy ước
- Dữ liệu có đến hết 03/02/2026.
- Trả hàng: hàng trả ghi số lượng âm, cùng mã đơn với đơn đã bán:
đơn có dòng số lượng âm là đơn có trả hàng.
""",
    )
    assert c.prepared.summary["status"] == "ready"
    orders = {r["raw_order_id"]: r for r in c.prepared.iter_records("orders")}
    assert orders["a"]["ordered_at"].startswith("2026-01-01")
    assert orders["a"]["status"] == "returned"
    assert c.prepared.summary["coverage"]["observed_span"]["end"] == "2026-02-02"
    p = draft(expression={"kind": "month", "year": 2026, "month": 1})
    r = execute_plan(p, question(question="Doanh thu tháng 1/2026?", as_of_date="2026-02-03"), c)
    assert r["answer"]["answer"] == 37
    p["aggregations"][0]["filters"] = ["shift=afternoon"]
    r = execute_plan(
        p, question(question="Doanh thu ca chiều tháng 1/2026?", as_of_date="2026-02-03"), c
    )
    assert r["answer"]["answer"] == 30


def test_expired_question_budget_prevents_numeric_execution(
    synthetic_split, tmp_path, rulebook_text
):
    import time

    c = joined_source(synthetic_split, tmp_path, rulebook_text)
    r = execute_plan(draft(), question(), c, deadline=time.monotonic() - 1)
    assert r["answer"]["type"] == "abstain"
    assert r["execution"]["status"] == "failed"
    assert r["execution"]["reason"] == "question_timeout_before_execution"
    assert r["execution"]["error_stage"] == "execution"
    assert r["execution"]["sqlite_errorcode"] is None


@pytest.mark.parametrize(
    ("metric", "text", "expected", "phone_channel"),
    [
        ("order_count", "Số đơn bán qua điện thoại tháng này?", 1, "Điện thoại"),
        ("revenue", "Doanh thu bán qua điện thoại tháng này?", 100, "Điện thoại"),
        (
            "share",
            "Tỉ trọng các đơn bán qua điện thoại trong doanh thu tháng này bao nhiêu %?",
            25,
            "Điện thoại",
        ),
        ("order_count", "Số đơn bán qua điện thoại tháng này?", None, ""),
        ("revenue", "Doanh thu bán qua điện thoại tháng này?", None, ""),
        (
            "share",
            "Tỉ trọng các đơn bán qua điện thoại trong doanh thu tháng này bao nhiêu %?",
            None,
            "",
        ),
    ],
)
def test_header_channel_scope_is_applied_before_reduction(
    synthetic_split, tmp_path, rulebook_text, metric, text, expected, phone_channel
):
    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    csv_file(
        folder,
        "invoices.csv",
        ["id", "date", "status", "net_amount", "channel"],
        [
            ["a", "01/01/2026", "Xong", "100", phone_channel],
            ["b", "01/01/2026", "Xong", "300", "Tại quầy"],
            ["c", "01/01/2026", "Huỷ", "999", "Điện thoại"],
        ],
    )
    c = prepare(
        synthetic_split,
        tmp_path,
        rulebook_text,
        """Mỗi dòng một đơn hàng, không có chi tiết mặt hàng.
- **id**: order id
- **date**: order date
- **status**: order status
- **net_amount**: order net merchandise amount
- **channel**: sales channel
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
""",
    )
    if metric == "share":
        p = period_draft("share", ["revenue", "revenue"], filters=["channel=phone"])
    else:
        p = draft(metric)
        p["aggregations"][0]["filters"] = ["channel=phone"]
    r = execute_plan(p, question(question=text), c)
    if expected is None:
        assert r["answer"]["type"] == "abstain"
    else:
        assert r["answer"]["answer"] == expected


@pytest.mark.parametrize("variant", ["declared", "financial", "negated", "qualifier"])
@pytest.mark.parametrize("filename", ["journeys.csv", "phieu_giao.csv"])
def test_declared_nonfinancial_delivery_family_isolation_keeps_unknown_money_blocked(
    synthetic_split, tmp_path, rulebook_text, variant, filename
):
    financial = variant == "financial"
    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    csv_file(
        folder,
        "receipts.csv",
        ["id", "day", "state", "net", "item"],
        [["one", "2026-01-01", "completed", "42", "Synthetic product"]],
    )
    header = ["order", "batch", "day", "qty"]
    values = ["one", "a", "2026-01-02", "2"]
    if financial:
        header.append("money")
        values.append("9999")
    csv_file(folder, filename, header, [values])
    readme = """Mỗi dòng một dòng hàng của đơn bán.
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
## Cột trong `receipts.csv`
- **id**: order id
- **day**: order date
- **state**: order status
- **net**: line net amount
- **item**: product name
## Cột trong `journeys.csv`
Mỗi dòng một đợt giao hàng, không phải một đơn bán mới.
- **order**: order id
- **batch**: delivery batch id
- **day**: delivery date
- **qty**: delivered quantity
"""
    if financial:
        readme += "- **money**: unclassified financial amount\n"
    if variant == "negated":
        readme = readme.replace(
            "Mỗi dòng một đợt giao hàng", "Không phải mỗi dòng một đợt giao hàng"
        )
    elif variant == "qualifier":
        readme += "Các dòng này cũng là một bản sao doanh thu cần đối soát.\n"
    readme = readme.replace("journeys.csv", filename)
    c = prepare(synthetic_split, tmp_path, rulebook_text, readme)
    if variant != "declared":
        assert c.prepared.summary["status"] == "unsupported"
        assert "delivery_family_mapping_or_authority_unsupported" in c.prepared.summary["reason"]
    else:
        assert c.prepared.summary["status"] == "ready"
        family = c.prepared.summary["isolated_source_families"]
        assert len(family) == 1 and family[0]["family"] == "delivery_batches"
        assert family[0]["status"] == "unsupported_operator"
        assert execute_plan(draft(), question(), c)["answer"]["answer"] == 42
