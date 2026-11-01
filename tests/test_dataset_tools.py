from __future__ import annotations

import copy
import csv
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from scripts.annotate_questions import (
    annotate,
    propose,
    select_pilot,
    validate_annotations,
)
from scripts.audit_dataset import (
    compare_inventory,
    freeze_manifest,
    inventory,
    outside_inputs,
    validate_case_specs,
    validate_manifest,
)
from src.data.split import Question, Split, load_split
from src.jsonio import ContractError
from src.runtime import run_inference


def evaluation_fixture() -> tuple[Split, dict, dict]:
    questions = tuple(
        Question(f"q_{i}", f"shop_{i}", "Synthetic question", "2026-01-01") for i in range(3)
    )
    split = Split(
        Path("/synthetic/train"), questions, tuple(q.business_id for q in questions), {}, 1
    )
    current = {
        "files": {"sales.csv": {"bytes": 1, "sha256": "a" * 64}},
        "source_fingerprint": "f" * 64,
    }
    partitions = {
        name: {"businesses": [f"shop_{i}"]}
        for i, name in enumerate(["dev", "validation", "final_holdout"])
    }
    selection = {
        "version": "validation-split-v1",
        "seed": 123,
        "selection_rule": "synthetic fixed selection",
        "business_groups": {f"shop_{i}": f"group_{i}" for i in range(3)},
        "group_definitions": {f"group_{i}": "synthetic structure" for i in range(3)},
        "previous_gold_businesses": ["shop_0"],
        "previous_gold_question_ids": ["q_0"],
        "protocols": {
            name: {"partitions": copy.deepcopy(partitions)} for name in ["business", "structure"]
        },
    }
    return split, current, freeze_manifest(selection, split, current)


@pytest.mark.parametrize(
    "corruption", ["business", "question", "missing", "group", "fingerprint", "gold"]
)
def test_frozen_manifest_rejects_leakage_and_drift(corruption):
    split, current, manifest = evaluation_fixture()
    validate_manifest(manifest, split, current)
    parts = manifest["protocols"]["business"]["partitions"]
    if corruption == "business":
        parts["validation"]["businesses"] = ["shop_0"]
    elif corruption == "question":
        parts["validation"]["question_ids"].append("q_0")
    elif corruption == "missing":
        parts["final_holdout"]["question_ids"] = []
    elif corruption == "group":
        manifest["business_groups"]["shop_1"] = "group_0"
    elif corruption == "fingerprint":
        current = copy.deepcopy(current)
        current["files"]["sales.csv"]["sha256"] = "b" * 64
    else:
        manifest["previous_gold_question_ids"] = ["q_2"]
    with pytest.raises(ContractError):
        validate_manifest(manifest, split, current)


def test_frozen_manifest_cannot_be_silently_refrozen():
    split, current, manifest = evaluation_fixture()
    with pytest.raises(ContractError, match="already frozen"):
        freeze_manifest(manifest, split, current)


def test_inventory_reads_structures_and_hashes_labels_as_opaque_bytes(tmp_path):
    root = tmp_path / "train"
    root.mkdir()
    (root / "answers.jsonl").write_bytes(b"not valid JSON; hash only\x00")
    (root / "sales.csv").write_text("id,amount\nA,100\n")
    with zipfile.ZipFile(root / "book.xlsx", "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Month 1"/><sheet name="Month 2"/></sheets></workbook>',
        )
        # No worksheets needed: inventory must not claim to parse contents.
    with sqlite3.connect(root / "pos.sqlite") as connection:
        connection.execute('CREATE TABLE "order detail" (id TEXT)')
    current = inventory(root)
    assert {(r["path"], r["table"]) for r in current["tables"]} == {
        ("sales.csv", "csv"),
        ("book.xlsx", "Month 1"),
        ("book.xlsx", "Month 2"),
        ("pos.sqlite", "order detail"),
    }
    assert current == inventory(root)
    (root / "escape.csv").symlink_to(root / "sales.csv")
    with pytest.raises(ContractError, match="symlink"):
        inventory(root)


def test_inventory_diff_reports_missing_changed_and_added_sources(tmp_path):
    def write_csv(name, fields, rows):
        with (tmp_path / name).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    write_csv(
        "file_inventory.csv",
        ["split", "path", "bytes", "sha256"],
        [
            {"split": "train", "path": "train/a.csv", "bytes": 1, "sha256": "old"},
            {"split": "train", "path": "train/missing.csv", "bytes": 1, "sha256": "old"},
            {
                "split": "public_test",
                "path": "public_test/never-read.csv",
                "bytes": 1,
                "sha256": "old",
            },
        ],
    )
    write_csv(
        "table_inventory.csv",
        ["split", "path", "table"],
        [
            {"split": "train", "path": "train/missing.csv", "table": "csv"},
        ],
    )
    current = {
        "files": {"a.csv": {"bytes": 2, "sha256": "new"}, "b.csv": {"bytes": 1, "sha256": "x"}},
        "tables": [{"path": "a.csv", "table": "csv"}],
    }
    result = compare_inventory(current, tmp_path, "train")
    assert result == {
        "matches": False,
        "added_files": ["b.csv"],
        "missing_files": ["missing.csv"],
        "changed_files": ["a.csv"],
        "added_tables": [("a.csv", "csv")],
        "missing_tables": [("missing.csv", "csv")],
    }


def test_output_guards_exclude_entire_dataset_and_resolved_symlinks(tmp_path):
    dataset = tmp_path / "dataset"
    data = dataset / "train"
    data.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(dataset, target_is_directory=True)
    for output in [data / "output", dataset / "explore/new", alias / "new"]:
        with pytest.raises(ContractError, match="outside"):
            outside_inputs(output, data, dataset / "explore")
    outside_inputs(tmp_path / ".artifacts/output", data, dataset / "explore")


def question(text: str, qid: str = "synthetic_q", business: str = "shop") -> Question:
    return Question(qid, business, text, "2026-01-01")


@pytest.mark.parametrize(
    "text,metrics,excluded,intent",
    [
        ("Ê doanh thu tháng 5 là bao nhiêu?", {"revenue"}, {"dead_stock"}, "aggregate"),
        ("Doanh thu 7 ngày gần nhất", {"revenue"}, {"dead_stock"}, "aggregate"),
        (
            "Tỷ trọng nhóm giảm đau trong revenue là bao nhiêu %?",
            {"revenue"},
            {"dead_stock"},
            "aggregate",
        ),
        (
            "Giá nhập bình quân một hộp trong tháng 4?",
            {"purchase_price"},
            {"purchase_quantity"},
            "aggregate",
        ),
    ],
)
def test_lexical_proposals_preserve_dev_discovered_distinctions(text, metrics, excluded, intent):
    q = question(text)
    record = propose(q)
    assert metrics.issubset(record["metrics"])
    assert not excluded.intersection(record["metrics"])
    assert intent in record["intents"]
    if "gần nhất" in text:
        assert "ranking" not in record["intents"]
    assert record["source_verification"] == "not_checked"
    validate_annotations([record], [q])


@pytest.mark.parametrize(
    "corruption",
    ["answer", "duplicate", "missing", "business", "enum", "review", "source", "uncertainty"],
)
def test_annotation_validator_rejects_false_evidence_and_label_leaks(corruption):
    q = question("Doanh thu hôm nay bao nhiêu?")
    record = propose(q)
    records = [record]
    if corruption == "answer":
        record["answer"] = 100
    elif corruption == "duplicate":
        records.append(copy.deepcopy(record))
    elif corruption == "missing":
        records.clear()
    elif corruption == "business":
        record["business_id"] = "another_shop"
    elif corruption == "enum":
        record["metrics"] = ["invented_metric"]
    elif corruption == "review":
        record["review_status"] = "reviewed"
    elif corruption == "source":
        record["source_verification"] = "source_is_missing"
    else:
        record["dimension_status"]["expected_output_type"] = "reviewed"
    with pytest.raises(ContractError):
        validate_annotations(records, [q])


def test_pilot_uses_intersection_of_dev_and_balances_businesses():
    questions = [
        question(f"Doanh thu hôm nay {i}", f"q_{business}_{i}", business)
        for business in ["a", "b", "held_out"]
        for i in range(10)
    ]
    manifest = {
        "seed": 12,
        "protocols": {
            "business": {"partitions": {"dev": {"businesses": ["a", "b", "held_out"]}}},
            "structure": {"partitions": {"dev": {"businesses": ["a", "b"]}}},
        },
    }
    pilot = select_pilot(questions, manifest, count=6)
    assert [q.business_id for q in pilot] == ["a", "b"] * 3
    assert pilot == select_pilot(list(reversed(questions)), manifest, count=6)


def test_annotation_never_parses_labels_or_eda_and_retains_unknowns(
    synthetic_split, tmp_path, monkeypatch
):
    split = load_split(synthetic_split)
    root = tmp_path / "dataset" / "train"
    root.parent.mkdir()
    synthetic_split.rename(root)
    split = load_split(root)
    # At least three whole businesses are needed by the evaluation protocol.
    all_questions = []
    for i in range(3):
        business = root / "data" / f"shop_{i}"
        business.mkdir()
        (business / "README.md").write_text("Synthetic business")
        (business / "sales.csv").write_text("id,amount\nA,100\n")
        all_questions.extend(
            [
                {
                    "id": f"q_{i}_{j}",
                    "business_id": f"shop_{i}",
                    "question": "Ba món hot nhất?",
                    "as_of_date": "2026-01-02",
                }
                for j in range(100)
            ]
        )
    (root / "questions.jsonl").write_text("".join(json.dumps(q) + "\n" for q in all_questions))
    split = load_split(root)
    _, _, selection = evaluation_fixture()
    del selection["source_files"], selection["source_fingerprint"]
    selection["previous_gold_question_ids"] = []
    manifest = freeze_manifest(selection, split, inventory(root))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    real_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if (
            path.name in {"answers.jsonl", "train_qa_joined.jsonl", "table_profiles.json"}
            and "b" not in mode
        ):
            raise AssertionError("label/EDA content parsed")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = annotate(root, path, tmp_path / "annotations")
    assert result["records"] == 300
    assert result["review_status"] == {"proposed": 300}
    assert result["unresolved"]["metrics"] == 300
    assert result["inputs_unchanged"]
    with pytest.raises(ContractError, match="outside"):
        annotate(root, path, tmp_path / "bad-review-output", reviews=root / "answers.jsonl")


def test_inference_ignores_offline_metadata(synthetic_split, tmp_path, monkeypatch):
    forbidden = {
        "answers.jsonl",
        "annotations.jsonl",
        "train_qa_joined.jsonl",
        "pilot_reviews.jsonl",
    }
    for name in forbidden - {"answers.jsonl"}:
        (synthetic_split / name).write_text("MUST NOT OPEN")
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name in forbidden:
            raise AssertionError("inference opened offline metadata")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = run_inference(synthetic_split, tmp_path / "predictions")
    assert result["questions_written"] == 2
    assert result["question_errors"] == 0


def test_acceptance_spec_validator_does_not_certify_metric_execution():
    path = Path(__file__).parent / "fixtures/data_acceptance_cases.json"
    specification = json.loads(path.read_text())
    assert validate_case_specs(specification) == 33
    specification["cases"][0]["implementation_status"] = "passed"
    with pytest.raises(ContractError, match="cannot certify"):
        validate_case_specs(specification)
