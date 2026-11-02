from __future__ import annotations

import hashlib
import socket
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from src.agent.mock import MockPlanner
from src.data.catalog import prepare_business
from src.data.split import load_split
from src.jsonio import dumps, loads
from src.runtime import run_inference
from src.semantics.answerability import preflight, prepare_semantics
from src.semantics.metric_registry import metric_contract
from src.semantics.rulebook import SemanticError, parse_rulebook

PERIOD = {"kind": "range", "start": "2026-01-01", "end": "2026-01-03"}


def check(context, metric="revenue", expression=None, **kwargs):
    return preflight(
        context,
        metric,
        PERIOD if expression is None else expression,
        as_of_date="2026-01-02",
        **kwargs,
    )


def test_rule_evidence_matches_exact_source_bytes_including_bom(rulebook_text):
    raw = rulebook_text.encode("utf-8-sig")
    digest = hashlib.sha256(raw).hexdigest()
    rules = parse_rulebook(rulebook_text, "new_split", source_sha256=digest)
    assert rules.sha256 == digest
    assert all(rule.status == "resolved" for rule in rules.rules.values())
    for rule in rules.rules.values():
        assert rule.source_refs
        for ref in rule.source_refs:
            assert ref["sha256"] == digest and ref["split"] == "new_split"
            assert rulebook_text.splitlines()[ref["line"] - 1] == ref["quote"]


def test_metric_specific_status_change_and_conflicting_or_new_rules(rulebook_text):
    modified = rulebook_text.replace("Đơn đang giao vẫn tính", "Đơn đang giao không tính")
    rules = parse_rulebook(modified, "another_split")
    assert metric_contract(rules, "revenue").status_policy == ["completed"]
    assert metric_contract(rules, "order_count").status_policy == ["completed", "shipping"]
    unknown = modified.replace("Đơn đang giao không tính", "Đơn đang giao tính nửa số tiền")
    with pytest.raises(SemanticError):
        metric_contract(parse_rulebook(unknown, "another_split"), "revenue")
    row = next(line for line in rulebook_text.splitlines() if line.startswith("| Đơn tính"))
    conflicting = rulebook_text + "\n" + row.replace("vẫn tính", "không tính")
    profile = parse_rulebook(conflicting, "another_split")
    assert profile.rules["revenue_status"].status == "ambiguous"


def test_industry_overrides_general_only_with_verified_scope(rulebook_text):
    row = next(line for line in rulebook_text.splitlines() if line.startswith("| Đơn tính"))
    text = (
        rulebook_text
        + "\n## Luật ngành\n### Synthetic industry\n"
        + row.replace("vẫn tính", "không tính")
    )
    selected = parse_rulebook(text, "split", industry="Synthetic industry")
    assert metric_contract(selected, "revenue").status_policy == ["completed"]
    assert selected.rules["revenue_status"].source_refs[0]["section"] == "Luật ngành"
    unknown = parse_rulebook(text, "split")
    assert unknown.rules["revenue_status"].status == "ambiguous"
    unrelated = parse_rulebook(text, "split", industry="Another industry")
    assert metric_contract(unrelated, "revenue").status_policy == ["completed", "shipping"]


def test_extra_rule_columns_do_not_silently_drop_qualifications(rulebook_text):
    row = next(line for line in rulebook_text.splitlines() if line.startswith("| Đơn tính"))
    text = rulebook_text.replace(row, row + " ngoại lệ chưa được hỗ trợ |")
    profile = parse_rulebook(text, "synthetic")
    assert profile.rules["revenue_status"].status == "unsupported"
    with pytest.raises(SemanticError):
        metric_contract(profile, "revenue")


def test_preflight_partial_and_independent_missing_baseline(semantic_source):
    context = semantic_source()
    partial = check(
        context, expression={"kind": "range", "start": "2025-12-30", "end": "2026-01-02"}
    )
    assert partial["decision"] == "eligible_to_execute"
    assert partial["window"]["requested_start"] == "2025-12-30"
    assert partial["effective_window"] == {
        "start": "2026-01-01",
        "end": "2026-01-02",
        "inclusive": True,
    }
    missing = check(context, expression={"kind": "month", "month": 12, "year": 2025})
    assert missing["decision"] == "unanswerable"
    assert missing["effective_window"] is None
    current = check(context)
    assert current["decision"] == "eligible_to_execute"
    assert current["window"]["requested_end"] == "2026-01-03"  # no implicit as-of cut
    cutoff = check(context, expression={**PERIOD, "cutoff": "as_of"})
    assert cutoff["effective_window"]["end"] == "2026-01-02"
    assert current["contract"]["rounding"]["unspecified_tie"] == "abstain"


def test_unknown_start_is_not_observed_min_but_confirmed_end_can_prove_outside(semantic_source):
    context = semantic_source(complete=False)
    assert context.semantics["coverage"]["available_span"] is None
    assert context.semantics["coverage"]["confirmed_end"] == "2026-01-03"
    assert check(context)["reason"] == "coverage_unknown"
    outside = check(context, expression={"kind": "day", "date": "2026-01-04"})
    assert outside["decision"] == "unanswerable"
    assert outside["reason"] == "outside_confirmed_end"
    before = check(context, expression={"kind": "day", "date": "2025-12-01"})
    assert before["decision"] == "abstain"


def test_amount_error_blocks_only_revenue_and_keeps_issue_evidence(semantic_source):
    context = semantic_source(rows=[["001", "01/01/2026", "Xong", "bad", "Item A"]])
    failed = check(context)
    assert failed["decision"] == "abstain" and failed["reason"] == "dependent_quality_issue"
    assert failed["issue_refs"]
    assert check(context, "order_count")["decision"] == "eligible_to_execute"


def test_no_transactions_is_eligible_but_is_not_a_computed_zero(semantic_source):
    context = semantic_source(rows=[["001", "03/01/2026", "Xong", "10", "Item A"]])
    result = check(context, expression={"kind": "day", "date": "2026-01-01"})
    assert result["decision"] == "eligible_to_execute"
    assert result["contract"]["empty_policy"] == "zero_if_complete"
    assert "answer" not in result


@pytest.mark.parametrize(
    "extra",
    [
        "Dữ liệu có đến hết 04/01/2026.\n",
        "Dữ liệu đầy đủ từ 02/01/2026 đến hết 03/01/2026.\n",
    ],
)
def test_conflicting_coverage_never_becomes_zero_or_unanswerable(semantic_source, extra):
    context = semantic_source(readme_extra=extra)
    result = check(context)
    assert context.semantics["coverage"]["conflicts"]
    assert result["decision"] == "abstain"


def test_unsupported_filters_metrics_and_shapes_do_not_erase_constraints(semantic_source):
    context = semantic_source()
    assert check(context, filters={"customer": "X"})["reason"] == "filter_unsupported"
    assert check(context, "inventory")["reason"] == "metric_unsupported"
    context.prepared = replace(context.prepared, path=None, summary={"status": "unsupported"})
    prepare_semantics(context)
    assert check(context)["reason"] == "preparation_unsupported"


def test_rule_change_invalidates_preparation_and_disabled_dedup_is_unsupported(
    semantic_source, synthetic_split, tmp_path, rulebook_text
):
    first = semantic_source(cache_dir=tmp_path / "cache")
    altered = rulebook_text.replace("Đơn đang giao vẫn tính", "Đơn đang giao không tính")
    second = semantic_source(rules=altered, cache_dir=tmp_path / "cache")
    assert not second.prepared.cache_hit
    assert first.semantics["revision"] != second.semantics["revision"]
    assert check(second)["contract"]["status_policy"] == ["completed"]
    disabled = rulebook_text.replace("lỗi xuất file, tính một lần", "giữ tất cả bản sao")
    third = semantic_source(rules=disabled)
    assert third.prepared.path is None
    assert check(third)["decision"] == "abstain"
    (synthetic_split / "rulebook.md").write_text(rulebook_text)
    with pytest.raises(SemanticError, match="changed"):
        prepare_semantics(third)


def test_whole_order_return_after_asof_is_shared_contract_not_event_cutoff(semantic_source):
    context = semantic_source(rows=[["001", "01/01/2026", "Đổi trả", "100", "Item A"]])
    assert check(context)["contract"]["status_policy"] == ["completed", "shipping"]
    contract = check(context, "order_count")["contract"]
    assert "no event cutoff on returns" in contract["history_requirement"]
    # Preparation regression already covers a negative-quantity return event after as-of.
    with closing(context.prepared.connect()) as connection:
        assert connection.execute("SELECT status FROM orders").fetchone() == ("returned",)
    foreign = replace(context.rules, split="foreign")
    context.rules = foreign
    with pytest.raises(SemanticError, match="revision/split"):
        prepare_semantics(context)


def test_runtime_metadata_reuse_cache_and_isolation(
    semantic_source, synthetic_split, tmp_path, monkeypatch
):
    semantic_source()
    before = {p: p.read_bytes() for p in synthetic_split.rglob("*") if p.is_file()}
    real_open = Path.open
    seen = []

    def guarded(path, *args, **kwargs):
        if path.name in {"answers.jsonl", "annotations.jsonl", "train_qa_joined.jsonl"}:
            raise AssertionError("opened offline metadata")
        return real_open(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("network is forbidden")

    class Inspector(MockPlanner):
        def decide(self, question, *, context=None):
            seen.append(context)
            assert check(context)["decision"] == "eligible_to_execute"
            return super().decide(question, context=context)

    monkeypatch.setattr(Path, "open", guarded)
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    for name in ["first", "warm"]:
        run_inference(
            synthetic_split, tmp_path / name, planner=Inspector(), cache_dir=tmp_path / "cache"
        )
    assert seen[0] is seen[1] and seen[2] is seen[3]
    first = loads((tmp_path / "first/catalogs/shop_a.json").read_text())
    second = loads((tmp_path / "warm/catalogs/shop_a.json").read_text())
    assert dumps(first["semantics"]) == dumps(second["semantics"])
    assert first["semantics"]["metric_execution"] == "not_implemented"
    assert all(c["status"] != "supported" for c in first["semantics"]["capabilities"])
    monkeypatch.setattr(Path, "open", real_open)
    assert before == {p: p.read_bytes() for p in synthetic_split.rglob("*") if p.is_file()}


def test_calendar_and_rules_are_attached_on_raw_context(synthetic_split, rulebook_text):
    (synthetic_split / "rulebook.md").write_text(rulebook_text)
    split = load_split(synthetic_split)
    context = prepare_business(split, "shop_a")
    assert context.rules is split.rules and context.calendar is split.calendar
    assert len(context.calendar) == split.calendar_days == 3


def test_preflight_rejects_changed_profile_or_missing_fact_columns(semantic_source):
    context = semantic_source()
    original = context.rules
    context.rules = replace(original, split="another_split")
    assert check(context)["reason"] == "stale_context"
    context.rules = original
    with sqlite3.connect(context.prepared.path) as connection:
        connection.execute("ALTER TABLE order_lines DROP COLUMN net_amount")
    prepare_semantics(context)
    assert check(context)["reason"] == "required_facts_unsupported"
    assert check(context, "order_count")["decision"] == "eligible_to_execute"


def test_two_asof_dates_and_question_reordering_preserve_semantic_revision(
    semantic_source, synthetic_split, tmp_path
):
    context = semantic_source()
    first_revision = context.semantics["revision"]
    first_facts = context.prepared.path.read_bytes()
    early = preflight(context, "order_count", {"kind": "current_month"}, as_of_date="2026-01-01")
    late = preflight(context, "order_count", {"kind": "current_month"}, as_of_date="2026-01-03")
    assert early["effective_window"]["end"] == "2026-01-01"
    assert late["effective_window"]["end"] == "2026-01-03"
    assert context.semantics["revision"] == first_revision
    assert context.prepared.path.read_bytes() == first_facts
    path = synthetic_split / "questions.jsonl"
    questions = path.read_text().splitlines()
    run_inference(synthetic_split, tmp_path / "initial", cache_dir=tmp_path / "cache")
    path.write_text("\n".join(reversed(questions)) + "\n")
    manifest = run_inference(synthetic_split, tmp_path / "reordered", cache_dir=tmp_path / "cache")
    assert manifest["preparation"]["shop_a"]["facts_cache_hit"] is True
    one = loads((tmp_path / "initial/catalogs/shop_a.json").read_text())
    two = loads((tmp_path / "reordered/catalogs/shop_a.json").read_text())
    assert one["semantics"] == two["semantics"]
