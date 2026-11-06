"""Discriminating entity, grain, cutoff, units, precision and output behaviors."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from src.agent.entities import business_spec, entity_candidates
from src.agent.execution import execute_plan
from src.data.catalog import prepare_business
from src.data.normalization import prepare_facts
from src.data.split import load_split
from src.jsonio import ContractError
from src.semantics.answerability import prepare_semantics
from src.semantics.business_rules import CLAUSES
from tests.test_planner_execution import question


@pytest.fixture
def business_source(synthetic_split, tmp_path, rulebook_text):
    folder = synthetic_split / "data/shop_a"
    (folder / "sales.csv").unlink()
    clauses = "\n".join(
        f"| {label} | {value} |" for label, value in CLAUSES.values() if not label.startswith("-")
    )
    clauses += "\n- Bằng nhau: " + CLAUSES["ranking_ties"][1] + ".\n"
    (synthetic_split / "rulebook.md").write_text(rulebook_text + "\n## Business rules\n" + clauses)
    (folder / "README.md").write_text("""File máy POS xuất ra.
Mã đơn nối header và detail. Tiền dùng dấu chấm thập phân, không nhóm nghìn.
Dữ liệu đầy đủ từ 01/01/2026 đến hết 31/01/2026.
Dữ liệu có đến hết 31/01/2026.
Công nợ: mỗi lần khách trả tiền cho một đơn là một dòng,
SoTienThu là số tiền trả lần đó; TongThanhToan là số khách phải trả cho cả đơn.
Giá vốn: GiaVonLucBan là giá vốn một đơn vị bán.
Tồn kho: tồn tại một thời điểm là tồn ở dòng cuối của sản phẩm tính đến thời điểm đó.
""")
    db = folder / "pos.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        CREATE TABLE don_hang (MaHoaDon TEXT, NgayLap TEXT, TinhTrang TEXT,
          MaKhachHang TEXT, TongThanhToan INTEGER, PhuongThucTT TEXT);
        INSERT INTO don_hang VALUES
          ('a','2026-01-01','completed','c1',100,'debt'),
          ('b','2026-01-02','returned','c2',70,'debt'),
          ('c','2026-01-02','completed',NULL,40,'cash'),
          ('d','2026-01-02','completed','c1',999,'cod'),
          ('e','2026-01-02','cancelled','c2',999,'debt');
        CREATE TABLE chi_tiet_don (MaHoaDon TEXT, SoThuTu INTEGER, MaHangHoa TEXT,
          SL INTEGER, SLQuyDoi INTEGER, TriGia INTEGER, GiaVonLucBan INTEGER);
        INSERT INTO chi_tiet_don VALUES
          ('a',1,'p2',1,12,80,30), ('a',2,'p1',1,2,20,10),
          ('a',3,'fee',1,999,5,0), ('b',1,'p2',1,1000,70,30),
          ('c',1,'p1',5,10,40,1), ('d',1,'p3',1,1,50,5),
          ('e',1,'p3',1,1000,1000,5);
        CREATE TABLE san_pham (MaHangHoa TEXT, TenGoi TEXT, DonViCoSo TEXT);
        INSERT INTO san_pham VALUES
          ('p1','Trà','cái'), ('p2','Cà phê','cái'), ('p3','Bánh','cái'),
          ('fee','Phí phục vụ','lần');
        CREATE TABLE khach_hang (MaKhachHang TEXT, TenKhachHang TEXT);
        INSERT INTO khach_hang VALUES ('c1','Lan'),('c2','Lan');
        CREATE TABLE thanh_toan (MaPhieuThu TEXT, MaHoaDon TEXT, NgayGioThu TEXT,
          SoTienThu INTEGER);
        INSERT INTO thanh_toan VALUES
          ('pa1','a','2026-01-01',20),('pa2','a','2026-01-03',30),
          ('pa3','a','2026-01-05',50),('pb1','b','2026-01-03',10),
          ('pc1','c','2026-01-02',10),('pd1','d','2026-01-03',999),
          ('pe1','e','2026-01-03',1);
        CREATE TABLE phieu_nhap (MaPhieuNhap TEXT, NgayNhapKho TEXT, TinhTrang TEXT,
          TongGiaTriNhap INTEGER);
        INSERT INTO phieu_nhap VALUES
          ('in1','2026-01-01','received',9007199254740993),
          ('in2','2026-01-03','cancelled',1000),
          ('in3','2026-01-05','received',7);
        CREATE TABLE the_kho (MaGiaoDichKho TEXT, MaHangHoa TEXT,
          NgayGioPhatSinh TEXT, TonCuoi INTEGER);
        INSERT INTO the_kho VALUES
          ('m1','p3','2026-01-01T10:00:00',100),
          ('m2','p3','2026-01-03T10:00:00',7),
          ('m3','p3','2026-01-04T10:00:00',9);
        """)
    context = prepare_business(load_split(synthetic_split), "shop_a")
    prepare_facts(context, tmp_path / "prepared")
    prepare_semantics(context)
    return context


def business_draft(q, context):
    spec = business_spec(q)
    count = len(spec["expressions"])
    needs_entity = (
        spec["group"] is None
        and not spec.get("trend")
        and spec["metric"] in {"quantity", "revenue", "stock_balance", "lot_expiry"}
    )
    return {
        "entities": [entity_candidates(context, q.question)[0]["entity_key"]]
        if needs_entity
        else [],
        "time_windows": [
            {"id": f"p{i}", "expression": e} for i, e in enumerate(spec["expressions"])
        ],
        "aggregations": [
            {
                "id": f"a{i}",
                "metric_id": spec["metric"],
                "window_ref": f"p{i}",
                "scope": "whole_business",
                "filters": [],
                "group_by": [spec["group"]] if spec["group"] else [],
                **({"limit": spec["limit"]} if spec["limit"] else {}),
            }
            for i in range(count)
        ],
        "derived_measures": [],
        "output": {"expected_type": spec["type"], "result_refs": [f"a{i}" for i in range(count)]},
        "uncertainties": [],
    }


def run(context, text):
    q = question(question=text)
    return execute_plan(business_draft(q, context), q, context)


def test_ranking_keeps_distinct_codes_uses_base_quantity_and_excludes_fee(business_source):
    result = run(business_source, "Top 3 mặt hàng bán chạy nhất tháng này?")
    assert result["answer"] == {
        "id": question().id,
        "type": "list",
        "answer": ["Trà", "Cà phê", "Bánh"],
    }
    assert result["execution"]["status"] == "verified"
    records = list(business_source.prepared.iter_records("entities"))
    assert len([r for r in records if r["kind"] == "product"]) == 4


def test_gross_profit_uses_sold_unit_cost_not_base_quantity_or_master_cost(business_source):
    # p1: 20 - 1*10 + 40 - 5*1 = 45; p2: 80 - 1*30 = 50.
    assert (
        run(business_source, "Hàng nào có tổng lãi gộp cao nhất tháng này?")["answer"]["answer"]
        == "Cà phê"
    )
    q = question(question="Hàng nào có tổng lãi gộp cao nhất tháng này?")
    d = business_draft(q, business_source)
    d["output"] = {"expected_type": "list", "result_refs": ["a0"]}
    with pytest.raises(ContractError, match="output"):
        execute_plan(d, q, business_source)


def test_debt_uses_payable_cutoff_returned_cod_positive_per_order_and_walkins(business_source):
    # a 50 + returned b 60 + anonymous c 30; COD/cancelled excluded.
    assert (
        run(business_source, "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")["answer"][
            "answer"
        ]
        == 140
    )
    assert run(business_source, "Có bao nhiêu khách còn nợ cửa hàng?")["answer"]["answer"] == 2
    table = run(
        business_source, "Liệt kê 2 khách nợ nhiều nhất tính đến hôm nay, kèm số tiền còn nợ."
    )["answer"]
    assert table["type"] == "table"
    assert table["answer"]["rows"] == [["Lan", Decimal(60)], ["Lan", Decimal(50)]]


def test_collection_uses_payment_day_excludes_same_day_cod_cancelled(business_source):
    assert (
        run(business_source, "Tháng này thu hồi công nợ được bao nhiêu?")["answer"]["answer"] == 40
    )


def test_purchase_precision_status_and_initial_purchase(business_source):
    result = run(business_source, "Tổng tiền nhập hàng tháng này là bao nhiêu?")
    assert result["answer"]["answer"] == 9007199254740993


def test_stock_takes_latest_balance_with_cutoff_instead_of_sum(business_source):
    assert (
        run(business_source, "Cuối ngày hôm nay Bánh còn bao nhiêu cái?")["answer"]["answer"] == 7
    )


def test_ambiguous_product_cannot_fall_back_to_shop_total(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("UPDATE san_pham SET TenGoi='Trà' WHERE MaHangHoa='p2'")
    business_source = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(business_source, tmp_path / "ambiguous")
    prepare_semantics(business_source)
    assert len(entity_candidates(business_source, "Trà")) == 2
    q = question(question="Tháng này bán Trà được bao nhiêu cái?")
    draft = business_draft(q, business_source)
    with pytest.raises(ContractError, match="entity"):
        execute_plan(draft, q, business_source)


def test_order_count_customer_ranking_includes_fee_once_and_excludes_walkins(business_source):
    assert (
        run(business_source, "Khách mua nhiều tiền nhất tháng này là ai?")["answer"]["answer"]
        == "Lan"
    )


def test_lot_expiry_uses_remaining_nonexpired_lots_and_cancelled_allocations(
    business_source, tmp_path
):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.executescript("""
        CREATE TABLE lo_hang (MaLo TEXT, MaHangHoa TEXT, MaPhieuNhap TEXT,
          HanSuDung TEXT, SLNhapQuyDoi INTEGER);
        INSERT INTO lo_hang VALUES ('L1','p3','in1','2026-02-01',1),
          ('L2','p3','in1','2026-01-20',10),('L3','p3','in1','2026-02-10',5);
        CREATE TABLE chi_tiet_don_lo (MaHoaDon TEXT, SoThuTu INTEGER, MaLo TEXT,
          SLQuyDoi INTEGER, SLTraQuyDoi INTEGER);
        INSERT INTO chi_tiet_don_lo VALUES ('d',1,'L1',1,0);
        """)
    readme = business_source.root / "data/shop_a/README.md"
    readme.write_text(
        readme.read_text()
        + "\nLô, hạn dùng: Một dòng bán có thể lấy từ nhiều lô. Hàng khách trả về lại lô đã bán.\n"
    )
    c = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(c, tmp_path / "lots")
    prepare_semantics(c)
    q = question(question="Lô Bánh gần hết hạn nhất có hạn dùng ngày nào?", as_of_date="2026-01-31")
    result = execute_plan(business_draft(q, c), q, c)
    assert result["answer"] == {"id": q.id, "type": "date", "answer": "2026-02-10"}


@pytest.mark.parametrize("mixed", [False, True])
def test_payable_money_column_grouping_and_conflicts_preserve_customer_metric(
    business_source, tmp_path, mixed
):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        rows = conn.execute("SELECT * FROM don_hang").fetchall()
        conn.execute("DROP TABLE don_hang")
        conn.execute(
            "CREATE TABLE don_hang (MaHoaDon TEXT, NgayLap TEXT, TinhTrang TEXT, "
            "MaKhachHang TEXT, TongThanhToan TEXT, PhuongThucTT TEXT)"
        )
        for i, row in enumerate(rows):
            amount = f"{row[4]}.000".replace(".", ",") if mixed and i == 1 else f"{row[4]}.000"
            conn.execute("INSERT INTO don_hang VALUES (?,?,?,?,?,?)", (*row[:4], amount, row[5]))
    readme = business_source.root / "data/shop_a/README.md"
    readme.write_text(
        readme.read_text().replace("Tiền dùng dấu chấm thập phân, không nhóm nghìn.", "")
    )
    c = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(c, tmp_path / "grouped")
    prepare_semantics(c)
    result = run(c, "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")
    if mixed:
        assert result["answer"]["type"] == "abstain"
    else:
        assert result["answer"]["answer"] == 209930
        assert any(
            column["basis"] == "user_2026_11_04_consistent_three_digit_groups"
            for source in c.prepared.summary["extensions"]["money_columns"].values()
            for column in source.values()
        )
    assert run(c, "Khách mua nhiều tiền nhất tháng này là ai?")["answer"]["answer"] == "Lan"


def test_boolean_trend_compares_exact_totals_in_question_order(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.executescript("""
        INSERT INTO don_hang VALUES
          ('nov','2025-11-01','completed',NULL,10,'cash'),
          ('dec','2025-12-01','completed',NULL,20,'cash');
        INSERT INTO chi_tiet_don VALUES
          ('nov',1,'p3',1,1,10,0),('dec',1,'p3',1,1,20,0);
        """)
    readme = business_source.root / "data/shop_a/README.md"
    readme.write_text(readme.read_text().replace("đủ từ 01/01/2026", "đủ từ 01/11/2025"))
    c = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(c, tmp_path / "trend")
    prepare_semantics(c)
    first = run(
        c, "Từ tháng 11/2025 qua tháng 12/2025 rồi tháng 1/2026, doanh thu có tăng liên tục không?"
    )
    assert first["answer"]["answer"] is True
    second = run(
        c, "Từ tháng 1/2026 qua tháng 11/2025 rồi tháng 12/2025, doanh thu có tăng liên tục không?"
    )
    assert second["answer"]["answer"] is False


def test_customer_ranking_preserves_digits_beyond_decimal_context(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        rows = conn.execute("SELECT * FROM chi_tiet_don").fetchall()
        conn.execute("DROP TABLE chi_tiet_don")
        conn.execute(
            "CREATE TABLE chi_tiet_don (MaHoaDon TEXT, SoThuTu INTEGER, MaHangHoa TEXT, "
            "SL INTEGER, SLQuyDoi INTEGER, TriGia TEXT, GiaVonLucBan INTEGER)"
        )
        for row in rows:
            value = row[5]
            if (row[0], row[1]) == ("a", 2):
                value = "10000000000000000000000000000000000000001"
            elif row[0] == "b":
                value = "10000000000000000000000000000000000000170"
            conn.execute(
                "INSERT INTO chi_tiet_don VALUES (?,?,?,?,?,?,?)", (*row[:5], str(value), row[6])
            )
        conn.execute("UPDATE don_hang SET TinhTrang='completed' WHERE MaHoaDon='b'")
        conn.execute("UPDATE khach_hang SET TenKhachHang='Hoa' WHERE MaKhachHang='c2'")
    c = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(c, tmp_path / "exact-ranking")
    prepare_semantics(c)
    assert run(c, "Khách mua nhiều tiền nhất tháng này là ai?")["answer"]["answer"] == "Hoa"


def test_entity_filter_in_ranking_and_expired_deadline_fail_closed(business_source):
    q = question(question="Top 3 mặt hàng Bánh bán chạy nhất tháng này?")
    with pytest.raises(ContractError, match="entity_filter"):
        execute_plan(business_draft(q, business_source), q, business_source)
    q = question(question="Top 3 mặt hàng bán chạy nhất tháng này?")
    decision = execute_plan(business_draft(q, business_source), q, business_source, deadline=0)
    assert decision["answer"]["type"] == "abstain"
    assert decision["execution"]["reason"] == "question_timeout_before_execution"


def test_named_anonymous_order_is_not_customer_when_shop_has_codes(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("ALTER TABLE don_hang ADD COLUMN TenKhachHang TEXT")
        conn.execute("UPDATE don_hang SET TenKhachHang='Giàu' WHERE MaHoaDon='c'")
        conn.execute("UPDATE chi_tiet_don SET TriGia=4000 WHERE MaHoaDon='c'")
    c = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(c, tmp_path / "anonymous")
    prepare_semantics(c)
    assert run(c, "Khách mua nhiều tiền nhất tháng này là ai?")["answer"]["answer"] == "Lan"
    assert run(c, "Có bao nhiêu khách còn nợ cửa hàng?")["answer"]["answer"] == 2
    assert run(c, "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")["answer"]["answer"] == 140


def test_marketplace_order_is_not_customer_debt_even_with_debt_method(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("ALTER TABLE don_hang ADD COLUMN KenhBanHang TEXT DEFAULT 'store'")
        conn.execute("UPDATE don_hang SET KenhBanHang='shopee' WHERE MaHoaDon='a'")
    c = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(c, tmp_path / "marketplace")
    prepare_semantics(c)
    assert run(c, "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")["answer"]["answer"] == 90


@pytest.mark.parametrize(
    "base,qualified",
    [
        (
            "Top 3 mặt hàng bán chạy nhất tháng này?",
            "Top 3 mặt hàng bán chạy nhất của nhà cung cấp ABC tháng này?",
        ),
        (
            "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?",
            "Khách hàng ABC còn nợ cửa hàng tổng cộng bao nhiêu tính đến hôm nay?",
        ),
        (
            "Top 3 mặt hàng bán chạy nhất tháng này?",
            "Top 3 mặt hàng bán chạy nhất mà khách ABC mua tháng này?",
        ),
        (
            "Top 3 khách mua nhiều tiền nhất tháng này?",
            "Top 3 khách mua nhiều tiền nhất cho sản phẩm ABC tháng này?",
        ),
        (
            "Top 3 khách mua nhiều tiền nhất tháng này?",
            "Top 3 khách mua nhiều tiền nhất cho Trà tháng này?",
        ),
    ],
)
def test_unknown_qualifier_cannot_be_dropped(business_source, base, qualified):
    draft = business_draft(question(question=base), business_source)
    with pytest.raises(ContractError):
        execute_plan(draft, question(question=qualified), business_source)


def test_customer_frequency_ranks_orders_instead_of_money(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("UPDATE khach_hang SET TenKhachHang='Mai' WHERE MaKhachHang='c2'")
        conn.execute(
            "UPDATE don_hang SET TinhTrang='completed',TongThanhToan=1000 WHERE MaHoaDon='b'"
        )
        conn.execute("UPDATE chi_tiet_don SET TriGia=1000 WHERE MaHoaDon='b'")
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "frequency")
    prepare_semantics(context)
    q = question(question="Khách mua nhiều lần nhất tháng này là ai?")
    draft = business_draft(q, context)
    assert execute_plan(draft, q, context)["answer"]["answer"] == "Lan"


def test_payment_excel_epoch_and_declared_second_precision_apply_cutoff(business_source, tmp_path):
    import zipfile

    from tests.test_ingestion import text_cell, workbook

    root = business_source.root
    folder = root / "data/shop_a"
    with sqlite3.connect(folder / "pos.sqlite") as conn:
        conn.execute("DROP TABLE thanh_toan")
    header = (
        '<row r="1">'
        + "".join(
            text_cell(f"{col}1", label)
            for col, label in zip(
                "ABCD", ["Phiếu thu", "Mã hóa đơn", "Thời gian thu", "Số tiền thu"], strict=True
            )
        )
        + "</row>"
    )
    row = (
        '<row r="2">'
        + text_cell("A2", "pa")
        + text_cell("B2", "a")
        + '<c r="C2" s="1"><v>46024.58333333333</v></c><c r="D2"><v>50</v></c></row>'
    )
    path = folder / "phieuthu.xlsx"
    workbook(path, [("Payments", "visible", "<sheetData>" + header + row + "</sheetData>")])
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["xl/styles.xml"] = (
        b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<numFmts><numFmt numFmtId="164" formatCode="yyyy-mm-dd h:mm:ss"/></numFmts>'
        b'<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    context = prepare_business(load_split(root), "shop_a")
    prepare_facts(context, tmp_path / "excel-payment")
    prepare_semantics(context)
    q = question(question="Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")
    assert execute_plan(business_draft(q, context), q, context)["answer"] == {
        "id": q.id,
        "type": "number",
        "answer": 160,
    }
    payments = list(context.prepared.iter_records("payments"))
    assert payments[0]["paid_at"] == "2026-01-02T14:00:00"
    early = question(question=q.question, as_of_date="2026-01-01")
    assert execute_plan(business_draft(early, context), early, context)["answer"]["answer"] == 100


def test_snapshot_definition_is_not_shadowed_by_readme_heading(business_source, tmp_path):
    folder = business_source.root / "data/shop_a"
    with sqlite3.connect(folder / "pos.sqlite") as conn:
        conn.execute("DROP TABLE the_kho")
    readme = folder / "README.md"
    text = readme.read_text().replace(
        "Tồn kho: tồn tại một thời điểm là tồn ở dòng cuối của sản phẩm tính đến thời điểm đó.",
        "- **Tồn kho**: **Ton kho** là tồn cuối ngày cuối có dữ liệu, theo đơn vị cơ bản.",
    )
    readme.write_text(text)
    (folder / "ton_kho.csv").write_text("ID SP,Đơn vị,Ton kho,Hàng hóa\np3,cái,7,Bánh\n")
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "snapshot")
    prepare_semantics(context)
    q = question(question="Cuối ngày hôm nay Bánh còn bao nhiêu cái?", as_of_date="2026-01-31")
    assert execute_plan(business_draft(q, context), q, context)["answer"] == {
        "id": q.id,
        "type": "number",
        "answer": 7,
    }
    assert context.prepared.summary["extensions"]["stock_mode"] == "snapshot"


def test_customer_total_cannot_bind_same_named_product(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("UPDATE san_pham SET TenGoi='Lan' WHERE MaHangHoa='p1'")
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "cross-kind")
    prepare_semantics(context)
    q = question(question="Doanh thu khách Lan tháng này là bao nhiêu tiền?")
    draft = business_draft(q, context)
    with pytest.raises(ContractError):
        execute_plan(draft, q, context)


def test_unsupported_business_grain_does_not_claim_missing_order_data(business_source, tmp_path):
    from tests.test_planner_execution import draft as legacy_draft

    folder = business_source.root / "data/shop_a"
    (folder / "pos.sqlite").unlink()
    (folder / "ledger.csv").write_text(
        "date,order_status,amount,item,base_quantity\n"
        "01/01/2026,completed,20,Trà,2\n01/01/2026,completed,20,Trà,2\n"
    )
    (folder / "products.csv").write_text("product_id,product_name,base_unit\np1,Trà,cái\n")
    (folder / "thekho.csv").write_text(
        "Mã giao dịch kho,Mã hàng hóa,Ngày giờ phát sinh,Tồn cuối\ns1,p1,2026-01-01,7\n"
    )
    (folder / "README.md").write_text(
        """File máy POS xuất ra.
Mỗi dòng một mặt hàng khách mua, không ghi mã đơn, không ghi tên khách.
Dữ liệu đầy đủ từ 01/01/2026 đến hết 31/01/2026.
Tồn kho: tồn tại một thời điểm là tồn ở dòng cuối của sản phẩm tính đến thời điểm đó.
## Cột trong `ledger.csv`
- **date**: order date
- **order_status**: order status
- **amount**: line net amount
- **item**: product name
- **base_quantity**: base quantity
## Cột trong `products.csv`
- **product_id**: product id
- **product_name**: product name
- **base_unit**: base unit
"""
    )
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "ledger-business")
    prepare_semantics(context)
    assert execute_plan(legacy_draft(), question(), context)["answer"]["answer"] == 40
    assert (
        execute_plan(legacy_draft("order_count"), question(question="Số đơn tháng này?"), context)[
            "answer"
        ]["type"]
        == "unanswerable"
    )
    assert context.prepared.summary["extensions"]["counts"]["stock_balances"] == 1
    q = question(question="Top 3 mặt hàng bán chạy nhất tháng này?")
    assert execute_plan(business_draft(q, context), q, context)["answer"]["type"] == "abstain"
    # Independent stock has an explicit balance rule and full README coverage;
    # missing sales order identity does not invalidate that different fact grain.
    q = question(question="Cuối ngày hôm nay Trà còn bao nhiêu cái?")
    assert execute_plan(business_draft(q, context), q, context)["answer"]["answer"] == 7


def test_business_parse_failure_blocks_even_outside_sales_span(business_source, tmp_path):
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("UPDATE thanh_toan SET NgayGioThu='not-a-date' WHERE MaPhieuThu='pa1'")
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "broken-payment")
    prepare_semantics(context)
    assert "payments" in context.prepared.summary["extensions"]["issues"]
    q = question(
        question="Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?", as_of_date="2026-02-01"
    )
    assert execute_plan(business_draft(q, context), q, context)["answer"]["type"] == "abstain"


@pytest.mark.parametrize(
    "base,qualified",
    [
        (
            "Lô Bánh gần hết hạn nhất có hạn dùng ngày nào?",
            "Theo tồn kho ngày 01/01/2026, lô Bánh gần hết hạn nhất có hạn dùng ngày nào?",
        ),
        (
            "Cuối ngày hôm nay Bánh còn bao nhiêu cái?",
            "Cuối ngày hôm nay Bánh còn bao nhiêu cái trong tháng 12/2025?",
        ),
        (
            "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?",
            "Khách còn nợ tổng cộng bao nhiêu tính đến hết tháng 12/2025?",
        ),
    ],
)
def test_explicit_window_is_never_replaced_by_today(business_source, base, qualified):
    draft = business_draft(question(question=base), business_source)
    with pytest.raises(ContractError):
        execute_plan(draft, question(question=qualified), business_source)


@pytest.mark.parametrize(
    "qualified",
    [
        "Top 0 mặt hàng bán chạy nhất tháng này?",
        "Top 100 mặt hàng bán chạy nhất tháng này?",
        "Top 3 bán chạy nhất tháng này?",
    ],
)
def test_cardinality_never_falls_back_to_single_entity(business_source, qualified):
    q = question(question="Mặt hàng bán chạy nhất tháng này là gì?")
    draft = business_draft(q, business_source)
    with pytest.raises(ContractError):
        execute_plan(draft, question(question=qualified), business_source)


def test_debt_ranking_never_falls_back_to_shop_balance(business_source):
    q = question(question="Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")
    draft = business_draft(q, business_source)
    qualified = question(question="Khách nào đang nợ nhiều nhất tính đến hôm nay?")
    with pytest.raises(ContractError):
        execute_plan(draft, qualified, business_source)


def test_independent_facts_do_not_inherit_unrecognized_sales_axis(business_source, tmp_path):
    path = business_source.root / "rulebook.md"
    path.write_text(
        path.read_text().replace("theo ngày đặt, không theo", "theo ngày giao, không theo")
    )
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "independent-facts")
    prepare_semantics(context)
    assert context.semantics["metrics"]["revenue"]["status"] == "unsupported"
    assert (
        run(context, "Tổng tiền nhập hàng tháng 1/2026 là bao nhiêu?")["answer"]["answer"]
        == 9007199254741000
    )
    assert (
        run(context, "Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?")["answer"]["answer"]
        == 140
    )
    assert run(context, "Cuối ngày hôm nay Bánh còn bao nhiêu cái?")["answer"]["answer"] == 7


def test_state_after_last_sale_and_payment_period_keep_their_own_dates(business_source, tmp_path):
    from src.semantics.answerability import preflight

    context = business_source
    # Last placed order is Jan 2, but later payments and stock balances are recorded.
    for text, date, expected in [
        ("Khách còn nợ tổng cộng bao nhiêu tính đến hôm nay?", "2026-01-05", 90),
        ("Cuối ngày hôm nay Bánh còn bao nhiêu cái?", "2026-01-04", 9),
    ]:
        q = question(question=text, as_of_date=date)
        assert execute_plan(business_draft(q, context), q, context)["answer"]["answer"] == expected
    check = preflight(
        context,
        "debt_collection",
        {"kind": "day", "date": "2026-01-03"},
        as_of_date="2026-01-06",
    )
    assert check["decision"] == "eligible_to_execute"
    assert check["effective_window"]["start"] == "2026-01-03"
    assert check["coverage"]["time_axis"] == "paid_at"
    assert check["coverage"]["fact_type"] == "payments"
    # README full span includes a day with no payment; no observed-day denominator.
    check = preflight(
        context,
        "debt_collection",
        {"kind": "day", "date": "2026-01-20"},
        as_of_date="2026-01-20",
    )
    assert check["decision"] == "eligible_to_execute"
    # A state beyond the declared end does not become the last available state.
    check = preflight(context, "stock_balance", {"kind": "today"}, as_of_date="2026-02-01")
    assert check["decision"] == "abstain"


@pytest.mark.parametrize("mutation", ["missing_declaration", "conflict", "bad_payment"])
def test_independent_coverage_does_not_bypass_authority_or_quality(
    business_source, tmp_path, mutation
):
    from src.semantics.answerability import preflight

    readme = business_source.root / "data/shop_a/README.md"
    if mutation == "missing_declaration":
        readme.write_text(readme.read_text().replace("Dữ liệu đầy đủ", "Export được ghi"))
    elif mutation == "conflict":
        readme.write_text(readme.read_text() + "\nDữ liệu có đến hết 01/01/2026.")
    else:
        with sqlite3.connect(readme.parent / "pos.sqlite") as conn:
            conn.execute("UPDATE thanh_toan SET NgayGioThu='broken' WHERE MaPhieuThu='pa1'")
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "invalid-own-coverage")
    prepare_semantics(context)
    check = preflight(
        context, "debt_collection", {"kind": "current_month"}, as_of_date="2026-01-10"
    )
    assert check["decision"] == "abstain"


def test_gross_profit_executor_uses_resolved_sales_status_policy(business_source, tmp_path):
    path = business_source.root / "rulebook.md"
    path.write_text(path.read_text().replace("Đơn đang giao vẫn tính", "Đơn đang giao không tính"))
    with sqlite3.connect(business_source.root / "data/shop_a/pos.sqlite") as conn:
        conn.execute("UPDATE don_hang SET TinhTrang='shipping' WHERE MaHoaDon='d'")
        conn.execute("UPDATE chi_tiet_don SET TriGia=900 WHERE MaHoaDon='d'")
    context = prepare_business(load_split(business_source.root), "shop_a")
    prepare_facts(context, tmp_path / "profit-status")
    prepare_semantics(context)
    assert run(context, "Hàng lời nhất tháng này là gì?")["answer"]["answer"] == "Cà phê"


def test_new_namespace_business_codes_file_and_row_order_preserve_ranking(
    business_source, tmp_path
):
    import shutil

    from src.jsonio import dumps

    root = business_source.root
    folder = root / "data/shop_a"
    with sqlite3.connect(folder / "pos.sqlite") as conn:
        rows = conn.execute("SELECT * FROM chi_tiet_don").fetchall()
        conn.execute("DELETE FROM chi_tiet_don")
        conn.executemany("INSERT INTO chi_tiet_don VALUES (?,?,?,?,?,?,?)", reversed(rows))
        for before, after in [("p1", "sku-a"), ("p2", "sku-b"), ("p3", "sku-c")]:
            conn.execute("UPDATE san_pham SET MaHangHoa=? WHERE MaHangHoa=?", (after, before))
            conn.execute("UPDATE chi_tiet_don SET MaHangHoa=? WHERE MaHangHoa=?", (after, before))
            conn.execute("UPDATE the_kho SET MaHangHoa=? WHERE MaHangHoa=?", (after, before))
    conn.close()
    q = question(question="Top 3 mặt hàng bán chạy nhất tháng này?", business_id="new_merchant")
    renamed = tmp_path / "another_split"
    new_folder = renamed / "data/new_merchant"
    new_folder.mkdir(parents=True)
    shutil.copyfile(folder / "pos.sqlite", new_folder / "register.sqlite")
    shutil.copyfile(folder / "README.md", new_folder / "README.md")
    for name in ["rulebook.md", "calendar.csv"]:
        shutil.copyfile(root / name, renamed / name)
    (renamed / "questions.jsonl").write_text(dumps(q.__dict__) + "\n")
    context = prepare_business(load_split(renamed), "new_merchant")
    prepare_facts(context, tmp_path / "permuted-facts")
    prepare_semantics(context)
    assert execute_plan(business_draft(q, context), q, context)["answer"]["answer"] == [
        "Trà",
        "Cà phê",
        "Bánh",
    ]
