from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

import pytest

from src.agent.context import build_context
from src.agent.execution import execute_plan
from src.agent.plan import generation_schema, validate_query_plan
from src.data.catalog import prepare_business
from src.data.normalization import prepare_facts
from src.data.split import Question, load_split
from src.jsonio import ContractError, dumps
from src.semantics.answerability import preflight, prepare_semantics
from tests.test_ingestion import text_cell, workbook


def question(**changes):
    return replace(
        Question(
            "snapshot_test",
            "shop_a",
            "Cuối ngày hôm nay trong kho còn bao nhiêu hộp Sản phẩm Á 100g?",
            "2026-01-03",
        ),
        **changes,
    )


def draft(**changes):
    value = {
        "entities": ["P01"],
        "time_windows": [{"id": "point", "expression": {"kind": "today"}}],
        "aggregations": [
            {
                "id": "stock",
                "metric_id": "inventory_on_hand",
                "window_ref": "point",
                "scope": "product",
                "filters": [],
                "group_by": [],
                "unit": "hộp",
            }
        ],
        "derived_measures": [],
        "output": {"expected_type": "number", "result_refs": ["stock"]},
        "uncertainties": [],
    }
    value.update(changes)
    return value


@pytest.fixture
def snapshot_source(synthetic_split, tmp_path, rulebook_text):
    index = 0

    def prepare(*, rows=None, declaration=True, extra="", cache_dir=None):
        nonlocal index
        index += 1
        folder = synthetic_split / "data/shop_a"
        (folder / "sales.csv").unlink(missing_ok=True)
        readme = "# Synthetic snapshot\n- Dữ liệu có đến hết 03/01/2026.\n"
        if declaration:
            readme += (
                "- **Tồn kho**: **Tồn cuối kỳ** là tồn cuối ngày cuối có dữ liệu, "
                "theo đơn vị cơ bản.\n"
            )
        (folder / "README.md").write_text(readme + extra)
        (synthetic_split / "rulebook.md").write_text(rulebook_text)
        records = (
            rows
            if rows is not None
            else [
                ("P01", "Sản phẩm Á 100g", "hộp", "7.125"),
                ("P02", "Mặt hàng B", "gói", "0"),
            ]
        )
        body = (
            "<sheetData><row r='1'>"
            + "".join(
                text_cell(f"{c}1", v)
                for c, v in zip("ABCD", ["Mã hàng", "Tên hàng", "ĐVT", "Tồn cuối kỳ"], strict=True)
            )
            + "</row>"
        )
        for i, (code, name, unit, qty) in enumerate(records, 2):
            body += f"<row r='{i}'>" + "".join(
                text_cell(f"{c}{i}", v) for c, v in zip("ABC", [code, name, unit], strict=True)
            )
            body += f'<c r="D{i}"><v>{qty}</v></c></row>'
        body += "</sheetData>"
        workbook(folder / "snapshot.xlsx", [("BaoCaoTonKho", "visible", body)])
        context = prepare_business(load_split(synthetic_split), "shop_a", cache_dir=cache_dir)
        prepare_facts(context, tmp_path / f"stock-{index}", cache_dir=cache_dir)
        prepare_semantics(context)
        return context

    return prepare


def test_snapshot_quantity_exact_provenance_and_history_coverage_remains_unknown(snapshot_source):
    context = snapshot_source()
    original = context.prepared.path.read_bytes()
    assert context.prepared.summary["shape"] == "inventory_snapshot"
    assert context.semantics["coverage"]["available_span"] is None
    assert context.semantics["snapshot"]["snapshot_at"] == "2026-01-03"
    result = execute_plan(draft(), question(), context)
    assert result["answer"] == {"id": "snapshot_test", "type": "number", "answer": Decimal("7.125")}
    assert result["execution"]["status"] == "verified"
    assert result["execution"]["rows_selected"] == 1
    assert result["execution"]["source_ref_samples"][0]["row"] == 2
    assert result["plan"]["time_windows"][0]["time_axis"] == "snapshot_at"
    assert result["plan"]["aggregations"][0]["unit"] == "hộp"
    assert context.prepared.path.read_bytes() == original
    for metric in ["order_count", "revenue"]:
        assert (
            preflight(context, metric, {"kind": "today"}, as_of_date="2026-01-03")["decision"]
            == "abstain"
        )


def test_wrong_snapshot_date_abstains_instead_of_extrapolating_history(snapshot_source):
    context = snapshot_source()
    result = execute_plan(draft(), question(as_of_date="2026-01-02"), context)
    assert result["answer"]["type"] == "abstain"
    assert result["execution"]["reason"] == "snapshot_date_unsupported"


def test_stock_aliases_allow_case_punctuation_spacing_but_not_accent_loss(snapshot_source):
    context = snapshot_source()
    result = execute_plan(
        draft(entities=["SẢN PHẨM Á100g"]),
        question(question="Cuối ngày hôm nay tồn kho còn bao nhiêu HỘP SẢNPHẨMÁ100g?"),
        context,
    )
    assert result["answer"]["answer"] == Decimal("7.125")


@pytest.mark.parametrize(
    "text,entities,unit",
    [
        ("Hôm nay tồn kho Sản phẩm Á 100g còn mấy thùng?", ["P01"], "thùng"),
        ("Hôm nay trong kho còn bao nhiêu hộp Sản phẩm A 100g?", ["P01"], "hộp"),
        ("Hôm nay còn bao nhiêu hộp Sản phẩm Á 100g?", ["P02"], "hộp"),
        ("Hôm nay tồn kho Sản phẩm Á 100g còn bao nhiêu?", ["P01"], "hộp"),
        ("Hôm nay tồn kho Sản phẩm Á 100g và Mặt hàng B bao nhiêu hộp?", ["P01"], "hộp"),
    ],
)
def test_entity_and_base_unit_grounding_rejects_unsupported_scope(
    snapshot_source, text, entities, unit
):
    context = snapshot_source()
    plan = draft(entities=entities)
    plan["aggregations"][0]["unit"] = unit
    with pytest.raises(ContractError):
        validate_query_plan(plan, question(question=text), context)


def test_missing_or_conflicting_snapshot_authority_does_not_enable_metric(snapshot_source):
    context = snapshot_source(declaration=False)
    assert context.prepared.path is None
    context = snapshot_source(extra="- Tồn cuối kỳ là tồn của tháng trước.\n")
    assert context.prepared.path is None


@pytest.mark.parametrize(
    "rows",
    [
        [("P01", "Sản phẩm Á 100g", "hộp", "7"), ("p01", "Mặt hàng C", "hộp", "2")],
        [("P01", "Sản phẩm Á 100g", "hộp", "7"), ("P02", "SẢN PHẨM Á 100G", "hộp", "2")],
        [("P01", "Sản phẩm Á 100g", "hộp", "NaN")],
    ],
)
def test_invalid_rows_block_snapshot_without_silent_dedup_or_zero(snapshot_source, rows):
    context = snapshot_source(rows=rows)
    assert context.prepared.summary["status"] == "blocked"
    result = preflight(
        context,
        "inventory_on_hand",
        {"kind": "today"},
        as_of_date="2026-01-03",
        filters={"entity": "P01", "unit": "hộp", "question": question().question},
    )
    assert result["reason"] == "dependent_quality_issue"


def test_snapshot_cache_and_context_do_not_expose_quantities(snapshot_source, tmp_path):
    first = snapshot_source(cache_dir=tmp_path / "cache")
    before = hashlib.sha256(first.prepared.path.read_bytes()).hexdigest()
    second = prepare_business(load_split(first.root), "shop_a", cache_dir=tmp_path / "cache")
    prepare_facts(second, tmp_path / "cached-stock", cache_dir=tmp_path / "cache")
    prepare_semantics(second)
    assert second.prepared.cache_hit
    assert hashlib.sha256(second.prepared.path.read_bytes()).hexdigest() == before
    payload = build_context(question(), second)
    assert payload["inventory_entities"] == [
        {"product_code": "P01", "product_name": "Sản phẩm Á 100g", "base_unit": "hộp"}
    ]
    assert "7.125" not in dumps(payload)
    assert set(payload["metrics"]) == {"inventory_on_hand"}
    schema = generation_schema(
        question(),
        stock=True,
        entity_codes=[row["product_code"] for row in payload["inventory_entities"]],
    )
    assert schema["properties"]["entities"]["items"] == {"enum": ["P01"]}
