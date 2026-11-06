from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from src.cli import main
from src.data.catalog import prepare_business
from src.data.ingestion import Cell
from src.data.normalization import prepare_facts
from src.data.normalization.values import (
    ParseError,
    cell_value,
    exact_add,
    normalize_status,
    parse_date,
    parse_datetime,
    parse_money,
    parse_number,
)
from src.data.split import load_split
from src.jsonio import ContractError, dumps, loads, read_jsonl
from src.runtime import run_inference
from tests.helpers import StubPlanner

README = """One order line per row.
- **order_id**: order id
- **date**: order date
- **status**: order status
- **amount**: line net amount
- **item**: product name
- **quantity**: quantity
Tiền dùng dấu phẩy nhóm nghìn.
Thông tin đơn chỉ ghi ở dòng đầu nhóm.
Dữ liệu có đến hết 03/01/2026.
"""
RULES = """Hai dòng của **cùng một đơn** giống nhau ở mọi ô: lỗi xuất file, tính một lần.
Ô để trống ở các dòng sau của cùng một nhóm dòng: giống ô đó ở dòng đầu nhóm.
"""


def source(root: Path, rows: list[list], readme: str = README):
    path = root / "data/shop_a/sales.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "date", "status", "amount", "item", "stt", "aux", "quantity"])
        writer.writerows(rows)
    (path.parent / "README.md").write_text(readme)
    (root / "rulebook.md").write_text(RULES)
    return prepare_business(load_split(root), "shop_a")


def line(
    order="001",
    day="01/01/2026",
    status="Xong",
    amount="100,000",
    item="Item A",
    stt="1",
    aux="",
    quantity="1",
):
    return [order, day, status, amount, item, stt, aux, quantity]


@pytest.mark.parametrize(
    ("raw", "style", "expected"),
    [
        ("1.600đ", "dot_grouped", "1600"),
        ("4,32tr", None, "4320000"),
        ("1tr340", None, "1340000"),
        ("-1tr340", None, "-1340000"),
        ("10,000.0000", "comma_grouped", "10000.0000"),
        ("99999999999999.00000001", "decimal_dot", "99999999999999.00000001"),
    ],
)
def test_money_preserves_source_precision(raw, style, expected):
    assert parse_money(raw, style) == Decimal(expected)


@pytest.mark.parametrize("raw", ["1,234", "1.234", "NaN", "bad", "1,23,000", True, 1.1, None])
def test_money_ambiguity_and_invalid_values_are_not_zero(raw):
    with pytest.raises(ParseError):
        parse_money(raw)


def test_decimal_addition_does_not_depend_on_process_precision():
    with localcontext() as context:
        context.prec = 4
        assert exact_add(Decimal("99999999999999.00000001"), Decimal("0.00000002")) == Decimal(
            "99999999999999.00000003"
        )
    assert parse_number("0,125", "decimal_comma") == Decimal("0.125")


def test_dates_keep_time_source_year_and_excel_epoch():
    assert parse_date("1 thg 1 2025").isoformat() == "2025-01-01"
    assert parse_date("01/01", year=2025).isoformat() == "2025-01-01"
    assert parse_date("45658", date_system="1900").isoformat() == "2025-01-01"
    assert parse_datetime("0.5", date_system="1904").isoformat() == "1904-01-01T12:00:00"
    assert parse_datetime("2026-01-01T09:30:15+07:00").isoformat() == "2026-01-01T09:30:15+07:00"
    assert parse_datetime("01/01/2026 09:30:15").hour == 9
    for raw, options in [
        ("01/01", {}),
        ("60", {"date_system": "1900"}),
        ("45658", {}),
        ("31/02/2026", {}),
        (True, {}),
    ]:
        with pytest.raises(ParseError):
            parse_date(raw, **options)


def test_native_errors_and_formula_caches_are_not_missing_or_zero():
    for cell in [Cell("#N/A", "error"), Cell(None, "formula", {"cache_status": "missing"})]:
        with pytest.raises(ParseError):
            cell_value(cell)
    assert cell_value(Cell("123", "formula", {"cache_status": "present"})) == "123"
    assert normalize_status("Đang giao") == "shipping"
    assert normalize_status("Đổi trả") == "returned"
    with pytest.raises(ParseError):
        normalize_status("")
    with pytest.raises(ParseError) as error:
        cell_value(Cell(None, "formula", {"cache_status": "missing"}))
    assert error.value.code == "formula_cache"


def test_excel_fraction_is_exact_even_with_low_process_decimal_precision():
    with localcontext() as context:
        context.prec = 4
        assert parse_datetime("0.12345625", date_system="1904").isoformat() == (
            "1904-01-01T02:57:46.620000"
        )
    with pytest.raises(ParseError, match="microseconds"):
        parse_datetime("0.999999999999999999999999999999", date_system="1904")


def test_group_restoration_dedup_and_lineage_preserve_grain(synthetic_split, tmp_path):
    context = source(
        synthetic_split,
        [
            line(),
            line(order="", day="", status=""),  # exact copy after group restoration
            line(stt="2"),  # same item/amount, different source STT
            line(order="002"),  # different order
            ["", "", "", "300,000", "Tổng ngày", "", "", ""],
            line(order="003", day="03/01/2026", amount="50,000"),
        ],
    )
    prepared = prepare_facts(context, tmp_path / "facts")
    assert context.prepared is prepared
    assert prepared.summary["status"] == "ready"
    assert prepared.summary["counts"] == {"orders": 3, "order_lines": 4, "lineage": 7, "issues": 0}
    assert prepared.summary["row_accounting"] == {
        "duplicate": 1,
        "header": 1,
        "kept": 4,
        "summary": 1,
    }
    assert prepared.summary["diagnostic_line_totals_by_status"] == {"completed": Decimal(350000)}
    assert {o["raw_order_id"] for o in prepared.iter_records("orders")} == {"001", "002", "003"}
    lineage = list(prepared.iter_records("lineage"))
    duplicate = next(r for r in lineage if r["disposition"] == "duplicate")
    assert "inherit:order_id" in duplicate["transformations"]
    assert duplicate["line_key"] in {r["line_key"] for r in lineage if r["disposition"] == "kept"}
    assert all(r["source_ref"]["sha256"] for r in lineage)
    assert prepared.summary["coverage"]["available_span"] == {
        "start": "2026-01-01",
        "end": "2026-01-03",
    }
    assert prepared.summary["coverage"]["observed_span"] == {
        "start": "2026-01-01",
        "end": "2026-01-03",
    }
    with closing(prepared.connect()) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM orders")


def test_summary_and_blank_break_group_inheritance(synthetic_split, tmp_path):
    context = source(
        synthetic_split,
        [
            line(),
            ["", "", "", "100,000", "Tổng ngày", "", "", ""],
            line(order="", day="", status=""),
            [],
            line(order="002"),
        ],
    )
    prepared = prepare_facts(context, tmp_path / "facts")
    assert prepared.summary["row_accounting"]["quarantined"] == 1
    assert prepared.summary["counts"]["orders"] == 2
    assert prepared.summary["blocking_metrics"] == ["order_count", "revenue"]


def test_auxiliary_error_is_retained_but_amount_error_blocks_only_revenue(
    synthetic_split, tmp_path
):
    context = source(synthetic_split, [line(aux="#N/A"), line(order="002", amount="#VALUE!")])
    prepared = prepare_facts(context, tmp_path / "facts")
    assert prepared.summary["counts"]["order_lines"] == 2
    assert prepared.summary["blocking_metrics"] == ["revenue"]
    assert prepared.summary["diagnostic_totals_complete"] is False
    assert any(r["net_amount"] is None for r in prepared.iter_records("order_lines"))
    assert {r["detail"]["severity"] for r in prepared.iter_records("issues")} == {
        "warning",
        "blocking",
    }


def test_conflicting_order_fields_and_unknown_roles_block_instead_of_dropping(
    synthetic_split, tmp_path
):
    context = source(
        synthetic_split,
        [
            line(),
            line(status="Đã hủy", stt="2"),
            line(order="002", item="Voucher", amount="-10,000"),
        ],
    )
    prepared = prepare_facts(context, tmp_path / "facts")
    assert prepared.summary["status"] == "blocked"
    assert prepared.summary["issue_counts"]["order_conflict"] == 1
    assert any(o["mapping_status"] == "ambiguous" for o in prepared.iter_records("orders"))
    assert prepared.summary["counts"]["lineage"] == 4


def test_missing_grain_mapping_or_runtime_rule_is_unsupported_not_empty(synthetic_split, tmp_path):
    for index, readme in enumerate(
        [
            README.replace("One order line per row.", "Ledger."),
            README.replace("line net amount", "order payable"),
        ]
    ):
        context = source(synthetic_split, [line()], readme)
        result = prepare_facts(context, tmp_path / f"unsupported-{index}")
        assert result.summary["status"] == "unsupported"
        assert result.path is None
    context = source(synthetic_split, [line()])
    (synthetic_split / "rulebook.md").write_text("Changed rule: keep duplicate lines.")
    context = prepare_business(load_split(synthetic_split), "shop_a")
    assert prepare_facts(context, tmp_path / "rules").summary["status"] == "unsupported"


def test_source_evidence_can_resolve_money_but_mixed_conventions_block(synthetic_split, tmp_path):
    readme = README.replace("Tiền dùng dấu phẩy nhóm nghìn.", "")
    context = source(synthetic_split, [line(amount="1,200,000"), line(order="002")], readme)
    prepared = prepare_facts(context, tmp_path / "resolved")
    assert prepared.summary["mapping"]["money_convention"] == "comma_grouped"
    assert prepared.summary["status"] == "ready"
    context = source(
        synthetic_split, [line(amount="1,200,000"), line(order="002", amount="1.200.000")], readme
    )
    prepared = prepare_facts(context, tmp_path / "mixed")
    assert prepared.summary["blocking_metrics"] == ["revenue"]
    context = source(synthetic_split, [line(amount="1,234")], readme)
    grouped = prepare_facts(context, tmp_path / "approved-grouping")
    assert grouped.summary["status"] == "ready"
    assert (
        grouped.summary["mapping"]["money_basis"] == "user_2026_11_04_consistent_three_digit_groups"
    )
    assert grouped.summary["mapping"]["money_warning"]


def test_decimal_storage_roundtrip_and_reload_are_exact(synthetic_split, tmp_path):
    readme = README.replace("dấu phẩy nhóm nghìn", "dấu chấm thập phân, không nhóm nghìn")
    context = source(
        synthetic_split,
        [line(amount="99999999999999.00000001"), line(order="002", amount="0.00000002")],
        readme,
    )
    first = prepare_facts(context, tmp_path / "cold", cache_dir=tmp_path / "cache")
    second = prepare_facts(context, tmp_path / "warm", cache_dir=tmp_path / "cache")
    assert not first.cache_hit and second.cache_hit
    assert first.summary == second.summary
    assert first.summary["diagnostic_line_totals_by_status"]["completed"] == Decimal(
        "99999999999999.00000003"
    )
    assert {r["net_amount"] for r in second.iter_records("order_lines")} == {
        Decimal("99999999999999.00000001"),
        Decimal("0.00000002"),
    }
    assert first.path.read_bytes() == second.path.read_bytes()


@pytest.mark.parametrize(
    "change", ["corrupt", "incomplete", "readme", "rulebook", "calendar", "source", "software"]
)
def test_prepared_cache_detects_damage_and_all_dependencies(
    synthetic_split, tmp_path, monkeypatch, change
):
    context = source(synthetic_split, [line()])
    cache = tmp_path / "cache"
    first = prepare_facts(context, tmp_path / "first", cache_dir=cache)
    entry = next((cache / "prepared").iterdir())
    if change == "corrupt":
        (entry / "facts.sqlite").write_bytes(b"corrupt")
    elif change == "incomplete":
        (entry / "manifest.json").unlink()
    elif change == "software":
        monkeypatch.setattr("src.data.normalization.store.VERSION", "changed")
    else:
        path = {
            "readme": synthetic_split / "data/shop_a/README.md",
            "rulebook": synthetic_split / "rulebook.md",
            "calendar": synthetic_split / "calendar.csv",
            "source": synthetic_split / "data/shop_a/sales.csv",
        }[change]
        path.write_text(path.read_text() + "\n")
        with pytest.raises(ContractError, match="changed"):
            prepare_facts(context, tmp_path / "stale", cache_dir=cache)
        context = prepare_business(load_split(synthetic_split), "shop_a")
    second = prepare_facts(context, tmp_path / "second", cache_dir=cache)
    assert not second.cache_hit
    assert first.summary["counts"]["orders"] == second.summary["counts"]["orders"]


def test_facts_keep_all_statuses_and_future_dates_with_question_independent_runtime(
    synthetic_split, tmp_path, monkeypatch
):
    source(
        synthetic_split,
        [
            line(),
            line(order="002", day="01/02/2026", status="Đổi trả"),
            line(order="003", status="Đang giao"),
            line(order="004", status="Đã hủy"),
        ],
    )
    original_open = Path.open
    before = {p: p.read_bytes() for p in synthetic_split.rglob("*") if p.is_file()}

    def guarded(path, *args, **kwargs):
        if path.name in {"answers.jsonl", "annotations.jsonl", "train_qa_joined.jsonl"}:
            raise AssertionError("opened offline metadata")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    contexts = []

    class InspectingPlanner(StubPlanner):
        def decide(self, question, *, context=None):
            contexts.append(context)
            orders = list(context.prepared.iter_records("orders"))
            assert len(orders) == 4
            assert {o["status"] for o in orders} == {
                "completed",
                "cancelled",
                "returned",
                "shipping",
            }
            assert any(o["ordered_at"].startswith("2026-02-01") for o in orders)
            return super().decide(question, context=context)

    run_inference(synthetic_split, tmp_path / "run", planner=InspectingPlanner())
    assert contexts[0] is contexts[1]
    assert all(r["type"] == "abstain" for _, r in read_jsonl(tmp_path / "run/submission.jsonl"))
    monkeypatch.setattr(Path, "open", original_open)
    assert before == {p: p.read_bytes() for p in synthetic_split.rglob("*") if p.is_file()}


def test_prepare_cli_and_output_boundaries(synthetic_split, tmp_path):
    source(synthetic_split, [line()])
    output = tmp_path / "prepared"
    assert main(["prepare", "--data-dir", str(synthetic_split), "--output-dir", str(output)]) == 0
    assert loads((output / "shop_a.json").read_text())["prepared"]["status"] == "ready"
    assert (output / "shop_a/facts.sqlite").is_file()
    context = prepare_business(load_split(synthetic_split), "shop_a")
    with pytest.raises(ContractError, match="outside"):
        prepare_facts(context, synthetic_split / "facts")
    with pytest.raises(ContractError, match="fresh"):
        prepare_facts(context, output / "shop_a")


def test_partial_return_preserves_future_event_and_marks_whole_order(synthetic_split, tmp_path):
    source(
        synthetic_split,
        [line(), line(amount="-20,000", quantity="-1", stt="2")],
        README + "Số lượng âm là trả hàng.\n- **return_date**: return date\n",
    )
    (synthetic_split / "rulebook.md").write_text(RULES + "Đơn trả một phần: bỏ **cả đơn**.\n")
    path = synthetic_split / "data/shop_a/sales.csv"
    rows = list(csv.reader(path.open()))
    rows[0].append("return_date")
    rows[1].append("")
    rows[2].append("01/02/2026")
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)
    context = prepare_business(load_split(synthetic_split), "shop_a")
    facts = prepare_facts(context, tmp_path / "facts")
    assert facts.summary["status"] == "ready"
    order = next(facts.iter_records("orders"))
    assert order["status"] == "returned" and order["ordered_at"] == "2026-01-01"
    lines = list(facts.iter_records("order_lines"))
    event = next(row for row in lines if row["row_role"] == "return_item")
    assert event["net_amount"] == Decimal(-20000)
    assert event["event_at"] == "2026-02-01"
    # Independent revenue/count check: the entire returned order is excluded, not netted.
    with closing(facts.connect()) as connection:
        selected = connection.execute(
            "SELECT l.net_amount FROM order_lines l JOIN orders o USING(order_key) "
            "WHERE o.status IN ('completed','shipping') AND l.row_role='sale_item'"
        ).fetchall()
        assert selected == []
    rows[2][2] = "Đã hủy"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)
    conflict = prepare_facts(
        prepare_business(load_split(synthetic_split), "shop_a"), tmp_path / "conflict"
    )
    assert conflict.summary["blocking_metrics"] == ["order_count", "revenue"]
    assert next(conflict.iter_records("orders"))["mapping_status"] == "ambiguous"
    rows[2][2] = "Xong"
    rows[1][7] = "bad quantity"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)
    unknown = prepare_facts(
        prepare_business(load_split(synthetic_split), "shop_a"), tmp_path / "unknown"
    )
    assert unknown.summary["blocking_metrics"] == ["order_count", "revenue"]


def test_distinct_raw_amounts_are_not_deduplicated_after_parsing(synthetic_split, tmp_path):
    context = source(synthetic_split, [line(), line(amount="100000")])
    facts = prepare_facts(context, tmp_path / "facts")
    assert facts.summary["counts"]["order_lines"] == 2
    assert facts.summary["diagnostic_line_totals_by_status"]["completed"] == 200000


def test_explicit_coverage_must_agree_with_source_and_is_not_inferred_from_calendar(
    synthetic_split, tmp_path
):
    readme = README + "Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.\n"
    context = source(synthetic_split, [line()], readme)
    facts = prepare_facts(context, tmp_path / "complete")
    assert facts.summary["coverage"]["available_span"] == {
        "start": "2026-01-01",
        "end": "2026-01-03",
    }
    context = source(synthetic_split, [line(day="01/02/2026")], readme)
    facts = prepare_facts(context, tmp_path / "contradiction")
    assert facts.summary["coverage"]["available_span"] is None
    assert facts.summary["issue_counts"]["coverage_conflict"] == 1


def test_prepared_keys_and_cache_are_namespaced_by_business_and_split(synthetic_split, tmp_path):
    import shutil

    context = source(synthetic_split, [line()])
    first = prepare_facts(context, tmp_path / "first", cache_dir=tmp_path / "cache")
    other = tmp_path / "other_split"
    shutil.copytree(synthetic_split, other)
    (other / "data/shop_a").rename(other / "data/shop_b")
    questions = (other / "questions.jsonl").read_text().replace("shop_a", "shop_b")
    (other / "questions.jsonl").write_text(questions)
    second = prepare_facts(
        prepare_business(load_split(other), "shop_b"),
        tmp_path / "second",
        cache_dir=tmp_path / "cache",
    )
    assert not second.cache_hit
    assert (
        next(first.iter_records("orders"))["order_key"]
        != next(second.iter_records("orders"))["order_key"]
    )


def test_failed_build_never_publishes_complete_facts(synthetic_split, tmp_path, monkeypatch):
    context = source(synthetic_split, [line()])

    def failed(*args):
        raise RuntimeError("interrupted normalization")

    monkeypatch.setattr("src.data.normalization.store.build_sales", failed)
    with pytest.raises(RuntimeError, match="interrupted"):
        prepare_facts(context, tmp_path / "facts", cache_dir=tmp_path / "cache")
    assert list((tmp_path / "facts").iterdir()) == []
    assert not (tmp_path / "cache").exists()


def test_cache_symlinks_and_external_tables_are_rejected(synthetic_split, tmp_path):
    context = source(synthetic_split, [line()])
    cache = tmp_path / "cache"
    cache.symlink_to(synthetic_split, target_is_directory=True)
    with pytest.raises(ContractError):
        prepare_facts(context, tmp_path / "facts", cache_dir=cache)
    facts = prepare_facts(context, tmp_path / "facts")
    with pytest.raises(ContractError):
        list(facts.iter_records("sqlite_master"))


def test_reordering_questions_and_dates_reuses_identical_uncut_facts(synthetic_split, tmp_path):
    source(synthetic_split, [line(), line(order="002", day="03/01/2026")])
    questions_path = synthetic_split / "questions.jsonl"
    questions = [row for _, row in read_jsonl(questions_path)]
    for question, day in zip(questions, ["2026-01-01", "2026-01-03"], strict=True):
        question["as_of_date"] = day
    questions_path.write_text("".join(dumps(row) + "\n" for row in questions))
    cache = tmp_path / "cache"
    first = run_inference(
        synthetic_split, tmp_path / "first", cache_dir=cache, planner=StubPlanner()
    )
    questions_path.write_text("".join(dumps(row) + "\n" for row in reversed(questions)))
    second = run_inference(
        synthetic_split, tmp_path / "second", cache_dir=cache, planner=StubPlanner()
    )
    assert not first["preparation"]["shop_a"]["facts_cache_hit"]
    assert second["preparation"]["shop_a"]["facts_cache_hit"]
    assert (tmp_path / "first/prepared/shop_a/facts.sqlite").read_bytes() == (
        tmp_path / "second/prepared/shop_a/facts.sqlite"
    ).read_bytes()


def xlsx_order_source(
    root,
    rulebook_text,
    *,
    extra_sheet=False,
    readme_change=None,
    bad_amount=False,
    suspicious_summary=False,
):
    from tests.test_ingestion import text_cell, workbook

    folder = root / "data/shop_a"
    (folder / "sales.csv").unlink(missing_ok=True)
    readme = """File đơn hàng mỗi dòng một sản phẩm, thông tin đơn lặp lại ở mọi dòng của đơn.
## Cột trong `sales.xlsx`
- **id**: mã đơn
- **day**: ngày bán
- **status**: trạng thái đơn
- **item**: tên hàng
- **amount**: thành tiền sau giảm
- **note**: auxiliary
## Cột trong `units.xlsx`
- **product**: tên hàng (file chỉ có mặt hàng có đơn vị lớn)
- **large**: đơn vị lớn (hộp)
- **factor**: hệ số: số đơn vị cơ bản trong 1 đơn vị lớn
- **base**: đơn vị cơ bản
## Quy ước
Dữ liệu đầy đủ từ 01/01/2026 đến hết 03/01/2026.
"""
    if readme_change:
        readme = readme_change(readme)
    (folder / "README.md").write_text(readme)
    (root / "rulebook.md").write_text(rulebook_text)
    records = [
        ["001", "01/01/2026", "Xong", "A", "bad" if bad_amount else "1,000", ""],
        ["001", "01/01/2026", "Xong", "Topping Pudding trứng", "50", ""],
        ["001", "01/01/2026", "Xong", "Topping Pudding trứng", "50", ""],
        ["002", "03/01/2026", "Đang giao", "B", None, ""],
        [
            "003" if suspicious_summary else "",
            "03/01/2026" if suspicious_summary else "",
            "Xong" if suspicious_summary else "",
            "",
            "1,000,000",
            "Tổng ngày 03/01",
        ],
    ]
    data = (
        "<sheetData><row r='1'>"
        + "".join(
            text_cell(f"{c}1", v)
            for c, v in zip(
                "ABCDEF", ["id", "day", "status", "item", "amount", "note"], strict=True
            )
        )
        + "</row>"
    )
    for number, row in enumerate(records, 2):
        data += f"<row r='{number}'>"
        for column, value in zip("ABCDEF", row, strict=True):
            if column == "E" and value is None:
                data += f'<c r="E{number}"><v>0.125</v></c>'
            elif value:
                data += text_cell(f"{column}{number}", value)
        data += "</row>"
    data += "</sheetData>"
    sheets = [("Orders", "visible", data)]
    if extra_sheet:
        sheets.append(("AnotherMonth", "visible", data))
    workbook(folder / "sales.xlsx", sheets)
    lookup = (
        "<sheetData><row r='1'>"
        + "".join(
            text_cell(f"{c}1", v)
            for c, v in zip("ABCD", ["product", "large", "factor", "base"], strict=True)
        )
        + "</row><row r='2'>"
        + "".join(
            text_cell(f"{c}2", v) for c, v in zip("ABCD", ["A", "hộp", "6", "cái"], strict=True)
        )
        + "</row></sheetData>"
    )
    workbook(folder / "units.xlsx", [("Conversion", "visible", lookup)])
    return prepare_business(load_split(root), "shop_a")


def test_xlsx_order_lines_exact_native_money_lookup_isolation_summary_and_dedup(
    synthetic_split, tmp_path, rulebook_text
):
    context = xlsx_order_source(synthetic_split, rulebook_text)
    before = {p: p.read_bytes() for p in (synthetic_split / "data/shop_a").iterdir()}
    result = prepare_facts(context, tmp_path / "prepared")
    assert result.summary["status"] == "ready"
    assert result.summary["shape"] == "xlsx_order_lines"
    assert result.summary["counts"] == {"orders": 2, "order_lines": 3, "lineage": 6, "issues": 0}
    assert result.summary["row_accounting"]["summary"] == 1
    assert result.summary["row_accounting"]["duplicate"] == 1
    assert len(result.summary["unused_source_ids"]) == 1
    assert result.summary["mapping"]["money_convention"] == "comma_grouped"
    assert result.summary["mapping"]["numeric_evidence"][0]["row_role"] == "summary"
    assert result.summary["diagnostic_line_totals_by_status"] == {
        "completed": Decimal("1050"),
        "shipping": Decimal(".125"),
    }
    assert all(p.read_bytes() == original for p, original in before.items())
    from src.semantics.answerability import preflight, prepare_semantics

    prepare_semantics(context)
    assert (
        preflight(context, "revenue", {"kind": "current_month"}, as_of_date="2026-01-03")[
            "decision"
        ]
        == "eligible_to_execute"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.replace("thông tin đơn lặp lại ở mọi dòng", "thông tin đơn chỉ ghi ở đầu"),
        lambda s: s.replace("## Cột trong `sales.xlsx`", "## Cột trong `other.xlsx`"),
        lambda s: s.replace("thành tiền sau giảm", "tổng khách phải trả"),
        lambda s: s.replace("hệ số: số đơn vị cơ bản trong 1 đơn vị lớn", "không có mô tả hệ số"),
    ],
)
def test_xlsx_missing_or_wrong_authority_does_not_enable_facts(
    synthetic_split, tmp_path, rulebook_text, change
):
    context = xlsx_order_source(synthetic_split, rulebook_text, readme_change=change)
    result = prepare_facts(context, tmp_path / "unsupported")
    assert result.path is None and result.summary["status"] == "unsupported"


def test_xlsx_multi_sheet_is_not_silently_clipped_to_one_sheet(
    synthetic_split, tmp_path, rulebook_text
):
    context = xlsx_order_source(synthetic_split, rulebook_text, extra_sheet=True)
    result = prepare_facts(context, tmp_path / "unsupported")
    assert result.path is None and result.summary["status"] == "unsupported"


@pytest.mark.parametrize("suspicious", [False, True])
def test_xlsx_invalid_amount_or_summary_with_order_identity_blocks_dependencies(
    synthetic_split, tmp_path, rulebook_text, suspicious
):
    context = xlsx_order_source(
        synthetic_split, rulebook_text, bad_amount=not suspicious, suspicious_summary=suspicious
    )
    result = prepare_facts(context, tmp_path / "blocked")
    assert result.summary["status"] == "blocked"
    assert "revenue" in result.summary["blocking_metrics"]
    assert ("order_count" in result.summary["blocking_metrics"]) == suspicious
