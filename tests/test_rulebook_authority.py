"""Authority documents are inputs; numeric examples and identities are synthetic."""

from dataclasses import replace
from pathlib import Path

import pytest

from src.semantics.metric_registry import metric_contract
from src.semantics.rulebook import SemanticError, parse_rulebook

PUBLIC_AXIS = (
    "theo ngày đặt, không theo ngày giao hay ngày thu tiền. Đơn trả hàng sau kỳ, "
    "kể cả trả sau hôm nay, vẫn bị loại khỏi doanh thu kỳ đặt"
)


@pytest.mark.parametrize("split", ["train", "public_test"])
def test_released_order_axis_preserves_whole_order_return_policy(split):
    # Deliberately reads authority only: no questions, facts, labels or saved runs.
    path = Path("dataset") / split / "rulebook.md"
    if not path.exists():
        pytest.skip("released authority not installed")
    rules = parse_rulebook(path.read_text(), split, industry="Nhà thuốc")
    contract = metric_contract(rules, "revenue")
    assert contract.time_axis == "ordered_at"
    assert contract.status_policy == ["completed", "shipping"]
    assert "no event cutoff on returns" in contract.history_requirement
    ref = rules.require("time_axis").source_refs[0]
    assert ref["split"] == split
    assert ref["quote"] == path.read_text().splitlines()[ref["line"] - 1]
    if split == "public_test":
        assert "kể cả trả sau hôm nay" in ref["quote"]


@pytest.mark.parametrize("metric", ["purchase_amount", "stock_balance"])
def test_independent_contract_does_not_require_sales_policy(rulebook_text, metric):
    from src.semantics.business_rules import CLAUSES

    label, value = CLAUSES["purchase_amount"]
    text = rulebook_text + f"\n| {label} | {value} |"
    rules = parse_rulebook(text, "synthetic")
    rules = replace(rules, rules={k: v for k, v in rules.rules.items() if k != "time_axis"})
    contract = metric_contract(rules, metric)
    assert "time_axis" not in contract.rule_refs
    assert "return_policy" not in contract.rule_refs
    with pytest.raises(SemanticError):
        metric_contract(rules, "revenue")


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.replace("theo ngày đặt,", "theo ngày giao,"),
        lambda s: s.replace("vẫn bị loại", "không bị loại"),
        lambda s: s + ". Trừ đơn trả một phần",
        lambda s: s + ". Ngoại lệ: đơn đang giao chỉ tính một nửa",
    ],
)
def test_order_axis_never_discards_changed_negation_or_exception(rulebook_text, change):
    original = "theo ngày đặt, không theo ngày giao hay ngày thu tiền"
    text = rulebook_text.replace(original, change(PUBLIC_AXIS))
    rules = parse_rulebook(text, "invented_split")
    with pytest.raises(SemanticError, match="time_axis"):
        metric_contract(rules, "revenue")


def test_public_axis_executes_recorded_return_after_cutoff_without_netting(
    semantic_source, rulebook_text
):
    from src.agent.execution import execute_plan
    from tests.test_planner_execution import draft, question

    text = rulebook_text.replace(
        "theo ngày đặt, không theo ngày giao hay ngày thu tiền", PUBLIC_AXIS
    )
    context = semantic_source(
        rules=text,
        rows=[
            ["r", "01/01/2026", "Đổi trả", "190", "A"],
            ["c", "01/01/2026", "Xong", "17", "B"],
            ["s", "01/01/2026", "Đang giao", "9", "C"],
            ["x", "01/01/2026", "Đã hủy", "1000", "D"],
        ],
    )
    q = question(as_of_date="2026-01-01")
    assert execute_plan(draft(), q, context)["answer"]["answer"] == 26
    q = question(question="Số đơn tháng này?", as_of_date="2026-01-01")
    assert execute_plan(draft("order_count"), q, context)["answer"]["answer"] == 2


def test_catalogue_keeps_industry_and_split_semantics_separate():
    from src.semantics.business_rules import CATALOGUE

    # Use new transaction-free identities; source wording is authority, not a label.
    text = (
        "## Theo ngành\n### Sửa xe\n| Bảo hành | "
        "Tỉ lệ bảo hành bằng số phiếu bảo hành có ngày trong kỳ, chia cho số phiếu có thu tiền "
        "trong cùng kỳ; phiếu bảo hành của xe sửa từ kỳ trước vẫn tính |\n"
    )
    unknown = parse_rulebook(text, "alpha")
    assert unknown.rules["repair_warranty"].understood
    assert unknown.rules["repair_warranty"].status == "ambiguous"
    selected = parse_rulebook(text, "beta", industry="Sửa xe")
    rule = selected.require("repair_warranty")
    assert rule.parameters["denominator"] == "charged_visits_in_period"
    assert rule.execution == "unavailable"
    assert "phone_warranty" not in selected.rules
    unrelated = parse_rulebook(text, "gamma", industry="Nhà thuốc")
    assert unrelated.rules["repair_warranty"].status == "not_applicable"
    # A catalogue definition cannot create absent authority in another split.
    assert CATALOGUE["repair_warranty"]
    with pytest.raises(SemanticError):
        parse_rulebook("## Không có luật này", "delta", industry="Sửa xe").require(
            "repair_warranty"
        )
    with pytest.raises(SemanticError, match="metric is not implemented"):
        metric_contract(selected, "repair_warranty")


@pytest.mark.parametrize("split", ["train", "public_test"])
def test_complete_authority_inventory_and_cosmetic_row_permutations(split):
    path = Path("dataset") / split / "rulebook.md"
    if not path.exists():
        pytest.skip("released authority not installed")
    text = path.read_text()
    first = parse_rulebook(text, split)
    # Reverse only contiguous data rows: preserve section/industry scope.
    result, rows = [], []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|") and "---" not in line:
            rows.append(line)
        else:
            result.extend(reversed(rows))
            rows = []
            result.append(line)
    cosmetic = "\n".join(result).replace("**", "").replace(" | ", "  |  ").upper()
    second = parse_rulebook(cosmetic, "unseen_namespace")
    assert first.sha256 != second.sha256
    assert first.clauses and all(c["understood"] for c in first.clauses)
    assert all(c["understood"] for c in second.clauses)
    assert {
        k: (v.status, v.operator, v.parameters, v.understood) for k, v in first.rules.items()
    } == {k: (v.status, v.operator, v.parameters, v.understood) for k, v in second.rules.items()}


def test_paragraph_purchase_exception_blocks_only_dependent_contract(rulebook_text):
    from src.semantics.business_rules import CLAUSES

    label, value = CLAUSES["purchase_amount"]
    text = rulebook_text + f"\n| {label} | {value} |\nNgoại lệ: phiếu nhập đầu kỳ không tính."
    rules = parse_rulebook(text, "generated")
    with pytest.raises(SemanticError, match="purchase_amount"):
        metric_contract(rules, "purchase_amount")
    assert metric_contract(rules, "revenue").time_axis == "ordered_at"


def test_audit_reads_only_authority_and_retains_unrecognized_units(synthetic_split, monkeypatch):
    from scripts.audit_rulebook import audit

    real_open = Path.open

    def guarded(path, *args, **kwargs):
        assert path.name in {
            "rulebook.md",
            "README.md",
            "calendar.csv",
            "rulebook.py",
            "business_rules.py",
        }
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    report = audit(synthetic_split)
    assert report["sources"] == {}
    assert len(report["readmes"]) == 1
    assert report["clauses"][0]["status"] == "unmapped"
    assert report["clauses"][0]["understood"] is False


def test_parser_helper_change_invalidates_prepared_cache(semantic_source, tmp_path, monkeypatch):
    first = semantic_source(cache_dir=tmp_path / "cache")
    real_read = Path.read_bytes

    def changed(path):
        raw = real_read(path)
        return raw + b"\n# changed helper revision\n" if path.name == "business_rules.py" else raw

    monkeypatch.setattr(Path, "read_bytes", changed)
    second = semantic_source(cache_dir=tmp_path / "cache")
    assert second.rules.parser_sha256 != first.rules.parser_sha256
    assert not second.prepared.cache_hit
    assert second.semantics["revision"] != first.semantics["revision"]


def test_catalogue_parameters_are_not_shared_mutable_split_defaults():
    text = (
        "| Khách sộp nhất | khách có giá trị trung bình một đơn cao nhất trong kỳ, "
        "chỉ xét khách có từ 3 đơn trở lên trong kỳ |"
    )
    first = parse_rulebook(text, "first")
    first.require("customer_aov_ranking").parameters["minimum_orders"] = 1
    assert (
        parse_rulebook(text, "second").require("customer_aov_ranking").parameters["minimum_orders"]
        == 3
    )


def test_wrapped_markdown_paragraphs_preserve_policy_and_physical_source_span(rulebook_text):
    import textwrap

    wrapped = "\n".join(
        textwrap.fill(line, width=65, subsequent_indent="  ")
        if line and not line.startswith(("#", "|"))
        else line
        for line in rulebook_text.splitlines()
    )
    first = parse_rulebook(rulebook_text, "original")
    second = parse_rulebook(wrapped, "wrapped")
    assert {k: (r.status, r.operator, r.parameters) for k, r in first.rules.items()} == {
        k: (r.status, r.operator, r.parameters) for k, r in second.rules.items()
    }
    ref = second.require("recorded_status").source_refs[0]
    assert ref["line_end"] > ref["line"]
    assert ref["quote"] == "\n".join(wrapped.splitlines()[ref["line"] - 1 : ref["line_end"]])


@pytest.mark.parametrize(
    "paragraph",
    [
        "Ngoại lệ: đơn đang giao chỉ tính một nửa doanh thu. "
        "Câu hỏi nhiều số thì ghi list theo đúng thứ tự câu hỏi nêu.",
        "Hôm nay của mỗi câu là trường as_of_date trong questions.jsonl. "
        "Mọi cách nói tương đối tính theo ngày này. Đơn đang giao chỉ tính một nửa. "
        "Riêng trạng thái huỷ, trả hàng của đơn lấy theo trạng thái ghi trên đơn, "
        "kể cả khi phiếu trả ghi ngày sau hôm nay.",
    ],
)
def test_recognized_paragraph_fragment_does_not_hide_other_qualifications(rulebook_text, paragraph):
    text = rulebook_text + "\n## Bổ sung\n" + paragraph
    rules = parse_rulebook(text, "synthetic")
    with pytest.raises(SemanticError):
        metric_contract(rules, "revenue")
    assert not rules.clauses[-1]["understood"]
