from __future__ import annotations

import hashlib
import io
import shutil
import sqlite3
import tarfile
import zipfile
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import colab_pipeline as pipeline
from src.jsonio import ContractError, dumps, loads, write_json


def test_stage_excludes_even_invalid_labels_and_preserves_inputs(synthetic_split, tmp_path):
    staged = tmp_path / "staged/train"
    before = (synthetic_split / "data/shop_a/sales.csv").read_bytes()
    result = pipeline.stage_inputs(synthetic_split, staged, expected_count=2)
    assert result["questions"] == 2 and not result["labels_staged"]
    assert not (staged / "answers.jsonl").exists()
    assert (staged / "data/shop_a/sales.csv").read_bytes() == before
    assert pipeline.stage_inputs(synthetic_split, staged, expected_count=2) == result
    (staged / "data/shop_a/sales.csv").chmod(0o644)
    (staged / "data/shop_a/sales.csv").write_text("changed")
    with pytest.raises(ContractError, match="differ"):
        pipeline.stage_inputs(synthetic_split, staged, expected_count=2)


def test_full_train_default_refuses_a_subset_before_staging(synthetic_split, tmp_path):
    staged = tmp_path / "staged/train"
    with pytest.raises(ContractError, match="3000"):
        pipeline.stage_inputs(synthetic_split, staged)
    assert not staged.exists()


@pytest.mark.parametrize("payload", [b"verified model", b"corrupt model"])
def test_download_never_publishes_unverified_bytes(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(pipeline.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(payload))
    target = tmp_path / "model.gguf"
    expected = hashlib.sha256(b"verified model").hexdigest()
    if payload == b"verified model":
        pipeline.download("https://example.invalid/model", target, expected)
        assert target.read_bytes() == payload
    else:
        with pytest.raises(ContractError, match="checksum"):
            pipeline.download("https://example.invalid/model", target, expected)
        assert not target.exists()
    assert not target.with_name("model.gguf.pending").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute/escape"])
def test_dataset_archive_cannot_escape_destination(tmp_path, name):
    archive = tmp_path / "train.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "forbidden")
    with pytest.raises(ContractError, match="unsafe"):
        pipeline.extract_train(archive, tmp_path / "extracted")


def make_part(folder: Path) -> list[str]:
    (folder / "logs").mkdir(parents=True)
    ids = ["fixture_a", "fixture_b"]
    (folder / "submission.jsonl").write_text(
        '{"id":"fixture_a","type":"number","answer":9007199254740993.123456789}\n'
        '{"id":"fixture_b","type":"abstain"}\n'
    )
    write_json(
        folder / "run_manifest.json",
        {
            "adapter": "local-planner",
            "question_errors": 0,
            "questions_written": 2,
            "planner_metadata": {
                "question_timeout_seconds": 120,
                "model": {
                    "sha256": pipeline.MODEL_SHA,
                    "layers_offloaded": 29,
                    "n_ctx": 4096,
                    "n_batch": 128,
                    "threads": 4,
                    "seed": 0,
                    "temperature": 0,
                },
            },
        },
    )
    for i, qid in enumerate(ids):
        write_json(folder / "logs" / f"{i:06d}.json", {"id": qid, "plan": {}})
    return ids


def test_checkpoint_preserves_decimal_bytes_excludes_facts_and_rejects_tampering(tmp_path):
    part = tmp_path / "part"
    ids = make_part(part)
    assert pipeline.validate_part(part, ids, "source-a")["questions"] == 2
    write_json(
        part / "verification.json",
        {
            "identity": "source-a",
            "submission_sha256": pipeline.digest(part / "submission.jsonl"),
        },
    )
    (part / "prepared").mkdir()
    (part / "prepared/facts.sqlite").write_bytes(b"redundant prepared copy")
    archived = tmp_path / "drive/000.tar.gz"
    pipeline.save_checkpoint(part, archived)
    restored = tmp_path / "restored"
    pipeline.restore_checkpoint(archived, restored)
    assert not (restored / "prepared").exists()
    assert (restored / "submission.jsonl").read_bytes() == (part / "submission.jsonl").read_bytes()
    pipeline.validate_part(restored, ids, "source-a")
    with pytest.raises(ContractError, match="changed"):
        pipeline.validate_part(restored, ids, "source-b")
    path = restored / "submission.jsonl"
    path.write_text(path.read_text().replace("123456789", "123456780"))
    with pytest.raises(ContractError, match="changed"):
        pipeline.validate_part(restored, ids, "source-a")


@pytest.mark.parametrize("damage", ["offload", "duplicate", "malformed", "logs"])
def test_part_gate_rejects_cpu_fallback_invalid_ids_types_and_missing_logs(tmp_path, damage):
    folder = tmp_path / "part"
    ids = make_part(folder)
    if damage == "offload":
        path = folder / "run_manifest.json"
        value = loads(path.read_text())
        value["planner_metadata"]["model"]["layers_offloaded"] = 0
        write_json(path, value)
    elif damage == "logs":
        (folder / "logs/000000.json").unlink()
    else:
        path = folder / "submission.jsonl"
        replacement = '"fixture_a"' if damage == "duplicate" else "null"
        token = '"fixture_b"' if damage == "duplicate" else "9007199254740993.123456789"
        path.write_text(path.read_text().replace(token, replacement))
    with pytest.raises(ContractError):
        pipeline.validate_part(folder, ids, "source-a")


def test_checkpoint_cannot_write_outside_run(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../escape")
        member.size = 3
        output.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(ContractError, match="unsafe"):
        pipeline.restore_checkpoint(archive, tmp_path / "restored")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("changed", [False, True])
def test_copy_cleanup_requires_validated_byte_matching_canonical_cache(tmp_path, changed):
    cache, folder = tmp_path / "cache", tmp_path / "part"
    dependencies = {"source": {"business_id": "synthetic_shop"}, "version": "fixture"}
    entry = cache / "prepared" / pipeline.fingerprint(dependencies)
    entry.mkdir(parents=True)
    canonical = entry / "facts.sqlite"
    with sqlite3.connect(canonical) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
        connection.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [
                ("dependencies", dumps(dependencies)),
                ("summary", dumps({"status": "ready"})),
            ],
        )
    write_json(
        entry / "manifest.json",
        {
            "complete": True,
            "dependencies": dependencies,
            "facts_sha256": pipeline.digest(canonical),
        },
    )
    facts = folder / "prepared/synthetic_shop/facts.sqlite"
    facts.parent.mkdir(parents=True)
    shutil.copyfile(canonical, facts)
    if changed:
        with sqlite3.connect(facts) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        with pytest.raises(ContractError, match="canonical cache"):
            pipeline.remove_prepared_copies(folder, cache)
        assert facts.exists()
    else:
        removed = pipeline.remove_prepared_copies(folder, cache)
        assert removed["synthetic_shop"]["sha256"] == pipeline.digest(canonical)
        assert not facts.exists()
    assert canonical.exists()


def test_evaluation_cannot_open_answers_before_completed_inference(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    write_json(run / "verification.json", {"complete": False})
    # Missing file proves the guard fails without even attempting to open gold.
    with pytest.raises(ContractError, match="before reading answers"):
        pipeline.evaluate_pipeline(
            tmp_path / "missing-answers", run, tmp_path / "stage_results.json"
        )


def test_active_run_blocks_scoring_even_if_prior_completion_exists(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "RUNNING").write_text("123")
    write_json(run / "verification.json", {"complete": True, "questions": 3000})
    with pytest.raises(ContractError, match="inference is active"):
        pipeline.evaluate_pipeline(tmp_path / "missing-gold", run, tmp_path / "stage_results.json")


def test_all3000_scoring_preserves_history_and_decimal_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "source_hashes", lambda: {})
    run, labels = tmp_path / "run", tmp_path / "labels"
    run.mkdir()
    labels.mkdir()
    questions, expected, predicted = [], [], []
    precise = Decimal("9007199254740993.123456789")
    for index in range(3000):
        qid = f"synthetic_{index}"
        questions.append(
            {
                "id": qid,
                "business_id": "synthetic_shop",
                "question": "Synthetic fixture?",
                "as_of_date": "2026-01-01",
                "stage": f"S{index % 5 + 1}",
            }
        )
        gold = {"id": qid, "type": "number", "answer": precise if index == 0 else index}
        actual = dict(gold)
        if index == 1:
            actual["answer"] = -1
        elif index == 2:
            actual = {"id": qid, "type": "abstain"}
        expected.append(gold)
        predicted.append(actual)
    answers = labels / "answers.jsonl"
    answers.write_text("".join(dumps(row) + "\n" for row in expected))
    submission = run / "submission.jsonl"
    submission.write_text("".join(dumps(row) + "\n" for row in predicted))
    selection = run / "selection.json"
    write_json(selection, {"questions": questions})
    write_json(
        run / "verification.json",
        {
            "complete": True,
            "questions": 3000,
            "source": {},
            "submission_sha256": pipeline.digest(submission),
            "selection_sha256": pipeline.digest(selection),
        },
    )
    old_record = {k: v for k, v in questions[0].items() if k != "stage"} | {
        "case_type": "native",
        "expected_answer": {"type": "number", "answer": precise},
        "results": {
            "historical_replay": {
                "actual_answer": {"type": "number", "answer": 0},
                "verdict": "wrong",
                "points": -1,
            }
        },
    }
    old_summary = {"native": {"total": 1, "correct": 0, "wrong": 1, "abstain": 0, "points": -1}}
    report = tmp_path / "stage_results.json"
    write_json(
        report,
        {
            "version": "stage-results-v1",
            "notes": [],
            "evaluations": {"historical_replay": {}},
            "stages": {
                "S1": {
                    "primary_evaluation": "historical_replay",
                    "questions": [deepcopy(old_record)],
                    "summaries": {"historical_replay": deepcopy(old_summary)},
                }
            },
        },
    )
    score = pipeline.evaluate_pipeline(answers, run, report)
    assert (score["total_questions"], score["correct"], score["wrong"], score["abstain"]) == (
        3000,
        2998,
        1,
        1,
    )
    result = loads(report.read_text())
    assert (
        result["stages"]["S1"]["questions"][0]["results"]["historical_replay"]
        == (old_record["results"]["historical_replay"])
    )
    assert result["stages"]["S1"]["summaries"]["historical_replay"] == old_summary
    full = result["stages"]["S6"]
    assert len(full["questions"]) == 3000
    assert full["questions"][0]["expected_answer"]["answer"] == precise
    assert full["questions"][2]["results"]["colab_train_3000"]["points"] == 0
    assert (
        sum(
            result["stages"][f"S{i}"]["summaries"]["colab_train_3000"]["native"]["total"]
            for i in range(1, 6)
        )
        == 3000
    )
    assert "9007199254740993.123456789" in report.read_text()
    assert "source_sha256" not in report.read_text()
