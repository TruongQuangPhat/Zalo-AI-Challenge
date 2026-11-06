from __future__ import annotations

import hashlib
import io
import json
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


def expanded_split(root: Path, count: int) -> None:
    record = next(pipeline.read_jsonl(root / "questions.jsonl"))[1]
    (root / "questions.jsonl").write_text(
        "".join(dumps(record | {"id": f"synthetic_{i}"}) + "\n" for i in range(count))
    )


@pytest.mark.parametrize("split_name,count", [("train", 3000), ("public_test", 1000)])
def test_datamate_existing_folder_stages_exact_split_without_opening_labels(
    synthetic_split, tmp_path, monkeypatch, split_name, count
):
    root = tmp_path / "DataMate"
    root.mkdir()
    source = root / split_name
    shutil.copytree(synthetic_split, source)
    expanded_split(source, count)
    original = Path.open

    def no_labels(path, *args, **kwargs):
        if path.name == "answers.jsonl":
            raise AssertionError("inference staging opened labels")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_labels)
    workspace = tmp_path / "ssd"
    result = pipeline.stage_from_root(root, workspace, split_name)
    assert result["source_kind"] == "folder" and result["questions"] == count
    assert Path(result["data_dir"]).name == split_name
    assert not (Path(result["data_dir"]) / "answers.jsonl").exists()
    assert pipeline.stage_from_root(root, workspace, split_name) == result
    assert (source / "answers.jsonl").exists()


def test_train_zip_is_used_once_and_folder_can_be_selected(synthetic_split, tmp_path):
    root = tmp_path / "DataMate"
    root.mkdir()
    source = root / "train"
    shutil.copytree(synthetic_split, source)
    expanded_split(source, 3000)
    archive = root / "train.zip"
    with zipfile.ZipFile(archive, "w") as target:
        for path in source.rglob("*"):
            if path.is_file():
                target.write(path, "train/" + str(path.relative_to(source)))
    workspace = tmp_path / "ssd"
    result = pipeline.stage_from_root(root, workspace, "train")
    assert result["source_kind"] == "zip" and result["questions"] == 3000
    assert result["archive_sha256"] == pipeline.digest(archive)
    assert pipeline.stage_from_root(root, workspace, "train") == result
    assert len(list((workspace / "archives").iterdir())) == 1
    folder = pipeline.stage_from_root(root, tmp_path / "folder-ssd", "train", source_kind="folder")
    assert folder["source_kind"] == "folder" and folder["sha256"] == result["sha256"]


def test_public_count_guard_rejects_partial_input_before_staging(synthetic_split, tmp_path):
    root = tmp_path / "DataMate"
    shutil.copytree(synthetic_split, root / "public_test")
    workspace = tmp_path / "ssd"
    with pytest.raises(ContractError, match="expected 1000"):
        pipeline.stage_from_root(root, workspace, "public_test")
    assert not (workspace / "inference/public_test").exists()


def test_public_evaluator_refuses_before_any_label_read(tmp_path):
    run = tmp_path / "public-run"
    run.mkdir()
    write_json(run / "verification.json", {"split": "public_test", "complete": True})
    with pytest.raises(ContractError, match="train-only"):
        pipeline.evaluate_pipeline(tmp_path / "nonexistent-labels", run, tmp_path / "report.json")


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
        pipeline.extract_split(archive, tmp_path / "extracted")


def make_part(folder: Path, ids: list[str] | None = None) -> list[str]:
    (folder / "logs").mkdir(parents=True)
    ids = ids if ids is not None else ["fixture_a", "fixture_b"]
    (folder / "submission.jsonl").write_text(
        "".join(
            dumps({"id": qid, "type": "number", "answer": Decimal("9007199254740993.123456789")})
            + "\n"
            if i == 0
            else dumps({"id": qid, "type": "abstain"}) + "\n"
            for i, qid in enumerate(ids)
        )
    )
    write_json(
        folder / "run_manifest.json",
        {
            "adapter": "local-planner",
            "status": "completed",
            "questions_total": len(ids),
            "question_errors": 0,
            "questions_written": len(ids),
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


@pytest.mark.parametrize("split_name,count", [("train", 3000), ("public_test", 1000)])
def test_split_runner_uses_predict_and_resumes_complete_chunks_without_labels(
    synthetic_split, tmp_path, monkeypatch, split_name, count
):
    expanded_split(synthetic_split, count)
    (synthetic_split / "answers.jsonl").unlink()  # Own synthetic, label-free inference fixture.
    model = tmp_path / "model.gguf"
    model.write_bytes(b"synthetic model bytes, never loaded")
    monkeypatch.setattr(pipeline, "MODEL_SHA", pipeline.digest(model))
    monkeypatch.setattr(pipeline, "require_project_environment", lambda: None)
    monkeypatch.setattr(pipeline, "source_hashes", lambda: {})

    def stage(question):
        if split_name == "public_test":
            raise AssertionError("public questions were used for train metadata")
        return "S1"

    monkeypatch.setattr(pipeline, "reporting_stage", stage)
    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            commands.append(command)
            assert command[:2] == ["bash", str(pipeline.ROOT / "predict.sh")]
            ids = [command[i + 1] for i, value in enumerate(command) if value == "--question-id"]
            make_part(Path(command[command.index("--output-dir") + 1]), ids)

        def poll(self):
            return 0

    monkeypatch.setattr(pipeline.subprocess, "Popen", Process)
    output, cache, checkpoints = (tmp_path / name for name in ["run", "cache", "drive-checkpoints"])
    kwargs = {"chunk_size": 500, "split_name": split_name}
    pipeline.run_pipeline(synthetic_split, output, cache, model, checkpoints, **kwargs)
    selection = (output / "selection.json").read_bytes()
    submission = (output / "submission.jsonl").read_bytes()
    assert len(commands) == count // 500
    assert len(submission.splitlines()) == count
    assert b"9007199254740993.123456789" in submission
    proof = loads((output / "verification.json").read_text())
    assert proof["complete"] and proof["split"] == split_name and proof["questions"] == count
    assert (checkpoints / "submission.jsonl").read_bytes() == submission
    original_calls = len(commands)
    pipeline.run_pipeline(synthetic_split, output, cache, model, checkpoints, **kwargs)
    assert len(commands) == original_calls  # All verified parts were restored, no inference rerun.
    assert (output / "submission.jsonl").read_bytes() == submission
    path = synthetic_split / "questions.jsonl"
    path.write_text(path.read_text().replace("Hôm nay", "Ngày hôm nay"))
    with pytest.raises(ContractError, match="new RUN_ID"):
        pipeline.run_pipeline(synthetic_split, output, cache, model, checkpoints, **kwargs)
    assert (output / "selection.json").read_bytes() == selection


def test_existing_notebook_has_train_public_modes_and_guarded_scoring():
    import ast

    notebook = json.loads((pipeline.ROOT / "scripts/DataMate_Colab.ipynb").read_text())
    sources = ["".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"]
    for source in sources:
        ast.parse(source)
    combined = "\n".join(sources)
    assert '"train": 3000, "public_test": 1000' in combined
    assert any(
        isinstance(node, ast.List)
        and any(
            isinstance(left, ast.Constant)
            and left.value == "--split"
            and isinstance(right, ast.Name)
            and right.id == "SPLIT"
            for left, right in zip(node.elts, node.elts[1:], strict=False)
        )
        for node in ast.walk(ast.parse(combined))
    )
    assert "datamate_runtime.zip" not in combined
    scoring = next(s for s in sources if '"scripts.colab_pipeline",' in s and '"evaluate",' in s)
    guard = ast.parse(scoring).body[0]
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "SPLIT == 'train' and EVALUATE_TRAIN"
    assert all(c.get("outputs", []) == [] for c in notebook["cells"])


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


@pytest.mark.parametrize(
    "damage", ["offload", "duplicate", "malformed", "logs", "status", "worker"]
)
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
    elif damage == "status":
        path = folder / "run_manifest.json"
        value = loads(path.read_text())
        value["status"] = "interrupted"
        write_json(path, value)
    elif damage == "worker":
        path = folder / "logs/000000.json"
        value = loads(path.read_text())
        value["plan"] = {"attempts": [{"error": "local_model_worker_unavailable"}]}
        write_json(path, value)
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
    proof = loads((run / "evaluation-verification.json").read_text())
    assert proof["submission_sha256"] == pipeline.digest(submission)
    assert proof["selection_sha256"] == pipeline.digest(selection)
