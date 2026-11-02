from __future__ import annotations

import os
from pathlib import Path

import pytest

import src.runtime as runtime
from src.agent.mock import MockPlanner
from src.data.cache import fingerprint
from src.data.catalog import prepare_business
from src.data.profiling import ProfileOptions
from src.data.split import load_split
from src.jsonio import ContractError, dumps, loads, read_jsonl


def test_runtime_reuses_business_context_and_retains_future_raw_records(
    synthetic_split, tmp_path, monkeypatch
):
    sales = synthetic_split / "data/shop_a/sales.csv"
    sales.write_text("order_id,date,amount\nA,2026-01-01,100\nB,2026-01-03,200\n")
    calls, contexts = [], []
    prepare = runtime.prepare_business

    def traced(split, business, **kwargs):
        calls.append(business)
        return prepare(split, business, **kwargs)

    class InspectingPlanner(MockPlanner):
        def decide(self, question, *, context=None):
            contexts.append(context)
            assert context.catalog["business_id"] == question.business_id
            table = context.catalog["tables"][0]
            assert len(list(context.iter_rows(table["source_id"]))) == 3
            return super().decide(question, context=context)

    monkeypatch.setattr(runtime, "prepare_business", traced)
    result = runtime.run_inference(synthetic_split, tmp_path / "run", planner=InspectingPlanner())
    assert calls == ["shop_a"]
    assert contexts[0] is contexts[1]
    assert result["question_errors"] == 0
    assert len(list((tmp_path / "run/catalogs").glob("*.json"))) == 1


def test_discovery_excludes_offline_metadata_before_open_even_in_business(
    synthetic_split, tmp_path, monkeypatch
):
    business = synthetic_split / "data/shop_a"
    forbidden = {"answers.csv", "train_qa_joined.xlsx", "annotations.sqlite"}
    for name in forbidden:
        (business / name).write_text("MUST_NOT_OPEN")
    (business / "reports").mkdir()
    (business / "reports/hidden.csv").write_text("MUST_NOT_OPEN")
    real_open = Path.open

    def guarded(path, *args, **kwargs):
        if path.name in forbidden or path.name == "hidden.csv":
            raise AssertionError("opened offline source")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    context = prepare_business(load_split(synthetic_split), "shop_a")
    assert context.catalog["read_complete"]
    assert len(context.catalog["tables"]) == 1
    assert len(context.catalog["excluded_files"]) == 4


def test_catalog_cache_roundtrip_damage_and_dependency_invalidation(synthetic_split, tmp_path):
    split = load_split(synthetic_split)
    cache = tmp_path / "cache"
    first = prepare_business(split, "shop_a", cache_dir=cache)
    second = prepare_business(split, "shop_a", cache_dir=cache)
    assert not first.cache_hit and second.cache_hit
    assert dumps(first.catalog) == dumps(second.catalog)
    entry = cache / fingerprint(first.catalog["dependencies"])
    (entry / "catalog.json").write_text("{}")
    repaired = prepare_business(split, "shop_a", cache_dir=cache)
    assert not repaired.cache_hit
    assert dumps(first.catalog) == dumps(repaired.catalog)
    (entry / "manifest.json").unlink()
    assert not prepare_business(split, "shop_a", cache_dir=cache).cache_hit
    # Content invalidation must not rely on size/mtime.
    sales = synthetic_split / "data/shop_a/sales.csv"
    stat = sales.stat()
    sales.write_text(sales.read_text().replace("100000", "200000"))
    os.utime(sales, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = prepare_business(split, "shop_a", cache_dir=cache)
    assert not changed.cache_hit
    assert first.catalog["tables"][0]["source_id"] == changed.catalog["tables"][0]["source_id"]
    assert first.catalog["files"][0]["sha256"] != changed.catalog["files"][0]["sha256"]


@pytest.mark.parametrize(
    "dependency",
    ["README.md", "rulebook.md", "calendar.csv", "config", "software", "add", "delete"],
)
def test_cache_invalidation_covers_authority_config_software_and_membership(
    synthetic_split, tmp_path, monkeypatch, dependency
):
    split = load_split(synthetic_split)
    business = synthetic_split / "data/shop_a"
    extra = business / "extra.csv"
    if dependency == "delete":
        extra.write_text("id,amount\nA,1\n")
    cache = tmp_path / "cache"
    first = prepare_business(split, "shop_a", cache_dir=cache)
    options = {}
    if dependency in {"README.md", "rulebook.md", "calendar.csv"}:
        path = business / dependency if dependency == "README.md" else synthetic_split / dependency
        path.write_text(path.read_text() + "\n")
    elif dependency == "config":
        options["profile_options"] = ProfileOptions(max_distinct=10)
    elif dependency == "software":
        monkeypatch.setattr("src.data.catalog._software_hash", lambda: "changed")
    elif dependency == "add":
        extra.write_text("id,amount\nA,1\n")
    else:
        extra.unlink()
    second = prepare_business(split, "shop_a", cache_dir=cache, **options)
    assert not second.cache_hit
    assert first.catalog["dependencies"] != second.catalog["dependencies"]


def test_context_does_not_cross_business_or_split_namespace(synthetic_split, tmp_path):
    split = load_split(synthetic_split)
    cache = tmp_path / "cache"
    first = prepare_business(split, "shop_a", cache_dir=cache)
    new_root = tmp_path / "other_split"
    synthetic_split.rename(new_root)
    second = prepare_business(load_split(new_root), "shop_a", cache_dir=cache)
    assert not second.cache_hit
    assert first.catalog["tables"][0]["source_id"] != second.catalog["tables"][0]["source_id"]


def test_file_failure_continues_other_tables_and_reports_incomplete(synthetic_split):
    business = synthetic_split / "data/shop_a"
    (business / "broken.xlsx").write_bytes(b"not a workbook")
    context = prepare_business(load_split(synthetic_split), "shop_a")
    assert context.catalog["read_complete"] is False
    statuses = {f["file"]: f["read_status"] for f in context.catalog["files"]}
    assert statuses["data/shop_a/broken.xlsx"] == "failed"
    assert statuses["data/shop_a/sales.csv"] == "read"
    assert context.catalog["issues"][0]["code"] == "file_read_failed"


def test_failed_business_preparation_abstains_and_keeps_question_logs(
    synthetic_split, tmp_path, monkeypatch
):
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise ContractError("synthetic preparation failure")

    monkeypatch.setattr(runtime, "prepare_business", failed)
    result = runtime.run_inference(synthetic_split, tmp_path / "run")
    assert calls == [1]
    assert result["question_errors"] == 2
    assert [r["type"] for _, r in read_jsonl(tmp_path / "run/submission.jsonl")] == ["abstain"] * 2
    assert len(list((tmp_path / "run/logs").glob("*.json"))) == 2


def test_source_symlinks_and_output_boundaries_are_enforced(synthetic_split, tmp_path):
    split = load_split(synthetic_split)
    business = synthetic_split / "data/shop_a"
    (business / "alias.csv").symlink_to(business / "sales.csv")
    with pytest.raises(ContractError, match="symlink"):
        prepare_business(split, "shop_a")
    (business / "alias.csv").unlink()
    with pytest.raises(ContractError, match="outside"):
        prepare_business(split, "shop_a", cache_dir=synthetic_split / "cache")
    alias = tmp_path / "cache_alias"
    real_cache = tmp_path / "real_cache"
    real_cache.mkdir()
    alias.symlink_to(real_cache, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink"):
        prepare_business(split, "shop_a", cache_dir=alias)


def test_catalog_cli_writes_one_catalog_per_business(synthetic_split, tmp_path):
    from src.cli import main

    output = tmp_path / "catalogs"
    assert main(["catalog", "--data-dir", str(synthetic_split), "--output-dir", str(output)]) == 0
    catalog = loads((output / "shop_a.json").read_text())
    assert catalog["read_complete"]
    assert catalog["business_semantics"] == "not_implemented"
    assert catalog["coverage"]["available_span"] is None
    assert not catalog["capabilities"]


def test_question_order_and_as_of_dates_do_not_change_prepared_catalog(synthetic_split, tmp_path):
    path = synthetic_split / "questions.jsonl"
    questions = [r for _, r in read_jsonl(path)]
    questions[0]["as_of_date"], questions[1]["as_of_date"] = "2026-01-01", "2026-01-03"
    path.write_text("".join(dumps(q) + "\n" for q in questions))
    cache = tmp_path / "cache"
    first = runtime.run_inference(synthetic_split, tmp_path / "first", cache_dir=cache)
    path.write_text("".join(dumps(q) + "\n" for q in reversed(questions)))
    second = runtime.run_inference(synthetic_split, tmp_path / "second", cache_dir=cache)
    assert not first["preparation"]["shop_a"]["cache_hit"]
    assert second["preparation"]["shop_a"]["cache_hit"]
    assert (tmp_path / "first/catalogs/shop_a.json").read_bytes() == (
        tmp_path / "second/catalogs/shop_a.json"
    ).read_bytes()
    assert [r["id"] for _, r in read_jsonl(tmp_path / "second/submission.jsonl")] == [
        q["id"] for q in reversed(questions)
    ]


def test_same_entity_key_in_two_businesses_keeps_separate_contexts(synthetic_split, tmp_path):
    beta = synthetic_split / "data/shop_b"
    beta.mkdir()
    (beta / "README.md").write_text("Synthetic business B")
    (beta / "sales.csv").write_text("order_id,amount\norder_1,999\n")
    path = synthetic_split / "questions.jsonl"
    questions = [r for _, r in read_jsonl(path)]
    questions[1]["business_id"] = "shop_b"
    path.write_text("".join(dumps(q) + "\n" for q in questions))
    seen = {}

    class InspectingPlanner(MockPlanner):
        def decide(self, question, *, context=None):
            table = context.catalog["tables"][0]
            row = list(context.iter_rows(table["source_id"]))[1]
            seen[question.business_id] = (table["source_id"], row.cells[2].raw_value)
            return super().decide(question, context=context)

    runtime.run_inference(synthetic_split, tmp_path / "run", planner=InspectingPlanner())
    assert seen["shop_a"][0] != seen["shop_b"][0]
    assert (seen["shop_a"][1], seen["shop_b"][1]) == ("100000", "999")
