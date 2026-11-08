from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.catalog import prepare_business
from src.data.normalization import prepare_facts
from src.data.normalization.llm_mapping import build_request, synthesize_readme
from src.data.split import load_split

# No grain declaration and no README vocabulary: the fixed mappers cannot read this shop.
README = """# Dữ liệu shop_a

Sổ bán hàng của tiệm.

## Quy ước
- Dữ liệu có đến hết 03/01/2026.
"""
RULES = "Hai dòng của **cùng một đơn** giống nhau ở mọi ô: lỗi xuất file, tính một lần.\n"
HEADER = ["Số phiếu", "Ngày lập", "Tình trạng", "Đơn giá", "Tiền dòng", "Mặt hàng", "SL"]
ROWS = [
    ["P1", "01/01/2026", "Xong", "50,000", "100,000", "Cà phê", "2"],
    ["P1", "01/01/2026", "Xong", "30,000", "30,000", "Bánh mì", "1"],
    ["P2", "02/01/2026", "Huỷ", "50,000", "50,000", "Cà phê", "1"],
    ["P3", "03/01/2026", "Xong", "20,000", "40,000", "Trà đá", "2"],
]
GOOD = {
    "Số phiếu": "order_id",
    "Ngày lập": "ordered_at",
    "Tình trạng": "status",
    "Đơn giá": "unit_price",
    "Tiền dòng": "line_net_amount",
    "Mặt hàng": "item_name",
    "SL": "quantity",
}
DECLARED = {
    "row_grain": "order_line",
    "order_info": "repeated_every_row",
    "numeric_convention": "comma_grouped",
    "multi_part": False,
    "negative_quantity_is_return": False,
    "exported_from_sales_software": False,
}


def proposal(columns: dict, role: str = "order_lines") -> dict:
    return {"tables": {"sales.csv": {"role": role, "columns": columns}}, "declarations": DECLARED}


class FakeModel:
    model_id = "fake"

    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict) -> dict:
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.fixture
def shop(synthetic_split: Path):
    path = synthetic_split / "data/shop_a/sales.csv"
    with path.open("w", newline="") as handle:
        csv.writer(handle).writerows([HEADER, *ROWS])
    (path.parent / "README.md").write_text(README)
    (synthetic_split / "rulebook.md").write_text(RULES)
    return lambda: prepare_business(load_split(synthetic_split), "shop_a")


def revenue(prepared) -> Decimal:
    completed = {
        o["order_key"] for o in prepared.iter_records("orders") if o["status"] == "completed"
    }
    return sum(
        (
            r["net_amount"]
            for r in prepared.iter_records("order_lines")
            if r["order_key"] in completed
        ),
        Decimal(0),
    )


def test_unreadable_shop_stays_unsupported_without_a_model(shop, tmp_path):
    prepared = prepare_facts(shop(), tmp_path / "facts")
    assert prepared.summary["status"] == "unsupported"
    assert "mapping_source" not in prepared.summary


def test_correct_proposal_prepares_the_shop_without_touching_the_source(shop, tmp_path):
    context = shop()
    readme_path = context.root / "data/shop_a/README.md"
    model = FakeModel(proposal(GOOD))
    prepared = prepare_facts(context, tmp_path / "facts", propose=model)
    assert prepared.summary["status"] == "ready"
    assert prepared.summary["mapping_source"] == "llm_proposal"
    assert revenue(prepared) == Decimal(170000)  # P2 is cancelled
    assert {o["raw_order_id"] for o in prepared.iter_records("orders")} == {"P1", "P2", "P3"}
    assert readme_path.read_text() == README
    assert "- **Tiền dòng**: thành tiền sau giảm" in context.catalog["readme"]
    assert context.catalog["readme"].startswith(README.split("\n\n")[0])
    assert len(model.prompts) == 1


def test_wrong_status_column_is_rejected_after_one_retry(shop, tmp_path):
    wrong = {**GOOD, "Tình trạng": "other", "Mặt hàng": "status", "SL": "item_name"}
    model = FakeModel(proposal(wrong), proposal(wrong))
    prepared = prepare_facts(shop(), tmp_path / "facts", propose=model)
    assert prepared.summary["status"] == "unsupported"
    assert prepared.summary["mapping_source"] == "llm_rejected"
    assert len(prepared.summary["llm_rejections"]) == 2
    assert "Đề xuất trước bị từ chối" in model.prompts[1]
    assert not (tmp_path / "facts").exists()


@pytest.mark.parametrize(
    "bad",
    [
        proposal({**GOOD, "SL": "line_net_amount"}),  # one meaning on two columns
        proposal(GOOD, role="products"),  # role contradicts columns
        proposal({k: v for k, v in GOOD.items() if k != "SL"}),  # column omitted
        {"tables": {}, "declarations": DECLARED},  # table omitted
    ],
)
def test_inconsistent_proposals_never_reach_the_builders(shop, tmp_path, bad):
    model = FakeModel(bad, bad)
    prepared = prepare_facts(shop(), tmp_path / "facts", propose=model)
    assert prepared.summary["status"] == "unsupported"
    assert all(prepared.summary["llm_rejections"])


def test_retry_with_feedback_can_recover(shop, tmp_path):
    model = FakeModel(proposal({**GOOD, "SL": "line_net_amount"}), proposal(GOOD))
    prepared = prepare_facts(shop(), tmp_path / "facts", propose=model)
    assert prepared.summary["status"] == "ready"
    assert "assigned to several columns" in model.prompts[1]


def test_cached_proposal_is_replayed_without_the_model(shop, tmp_path):
    cache = tmp_path / "cache"
    prepare_facts(shop(), tmp_path / "first", cache_dir=cache, propose=FakeModel(proposal(GOOD)))

    def offline(prompt, schema):
        raise AssertionError("model called despite a cached proposal")

    offline.model_id = "fake"
    prepared = prepare_facts(shop(), tmp_path / "second", cache_dir=cache, propose=offline)
    assert prepared.summary["status"] == "ready"
    assert revenue(prepared) == Decimal(170000)


def test_schema_closes_tables_columns_and_labels(shop):
    _, schema = build_request(shop().catalog)
    table = schema["properties"]["tables"]["properties"]["sales.csv"]
    columns = table["properties"]["columns"]
    assert columns["required"] == HEADER
    assert columns["additionalProperties"] is False
    assert "line_net_amount" in columns["properties"]["Tiền dòng"]["enum"]


def test_synthesis_rewrites_existing_bullets_and_marks_auxiliary_tables(shop):
    catalog = shop().catalog
    original = README.replace(
        "## Quy ước", "## Cột trong `sales.csv`\n- **Tiền dòng**: tiền khách trả\n\n## Quy ước"
    )
    text = synthesize_readme({**catalog, "readme": original}, proposal(GOOD, role="other"))
    assert text.count("**Tiền dòng**") == 1
    assert "- **Tiền dòng**: thành tiền sau giảm" in text
    assert "File phụ, không chứa giao dịch bán." in text
    assert "Mỗi dòng một dòng hàng." in text
    assert "- Dữ liệu có đến hết 03/01/2026." in text
