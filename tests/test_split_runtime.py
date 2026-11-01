from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src.agent.mock import MockPlanner
from src.cli import main
from src.data.split import load_split
from src.jsonio import ContractError, loads, read_jsonl
from src.runtime import run_inference

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def edit_question(root, change):
    path = root / "questions.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    change(records)
    path.write_text("".join(json.dumps(q) + "\n" for q in records))


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows.append(rows[0]),
        lambda rows: rows[0].update(as_of_date="2026-02-30"),
        lambda rows: rows[0].update(as_of_date="2026-03-01"),
        lambda rows: rows[0].update(business_id="../answers"),
        lambda rows: rows[0].update(business_id="missing"),
        lambda rows: rows[0].update(question=""),
    ],
)
def test_bad_questions_are_rejected_before_outputs(synthetic_split, tmp_path, change):
    edit_question(synthetic_split, change)
    output = tmp_path / "result"
    with pytest.raises(ContractError):
        run_inference(synthetic_split, output)
    assert not output.exists()


def test_calendar_and_required_sources_are_validated(synthetic_split):
    path = synthetic_split / "calendar.csv"
    path.write_text(path.read_text().replace("2026-01-02,5", "2026-01-02,1"))
    with pytest.raises(ContractError, match="weekday"):
        load_split(synthetic_split)
    path.unlink()
    with pytest.raises(ContractError, match="calendar"):
        load_split(synthetic_split)


def test_no_labels_no_network_inputs_immutable_and_deterministic(
    synthetic_split, tmp_path, monkeypatch
):
    before = snapshot(synthetic_split)
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name == "answers.jsonl":
            raise AssertionError("inference opened labels")
        return real_open(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("inference attempted network")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    for name in ["first", "second"]:
        manifest = run_inference(synthetic_split, tmp_path / name)
        assert manifest["is_mock"] is True
        assert manifest["status"] == "completed"
        assert manifest["question_errors"] == 0
        assert manifest["questions_written"] == 2
        assert len(list((tmp_path / name / "logs").glob("*.json"))) == 2
        assert "answers.jsonl" not in manifest["inputs"]["source_hashes"]
    monkeypatch.setattr(Path, "open", real_open)
    assert snapshot(synthetic_split) == before
    assert (tmp_path / "first/submission.jsonl").read_bytes() == (
        tmp_path / "second/submission.jsonl"
    ).read_bytes()


def test_output_cannot_write_inside_input_or_replace_existing_run(synthetic_split, tmp_path):
    with pytest.raises(ContractError, match="outside"):
        run_inference(synthetic_split, synthetic_split / "out")
    output = tmp_path / "out"
    run_inference(synthetic_split, output)
    with pytest.raises(ContractError, match="already exists"):
        run_inference(synthetic_split, output)


def test_failed_question_abstains_and_next_question_still_runs(synthetic_split, tmp_path):
    class FailingOnce(MockPlanner):
        def decide(self, question):
            if question.id == "q_one":
                raise RuntimeError("synthetic executor failure")
            return super().decide(question)

    manifest = run_inference(synthetic_split, tmp_path / "out", planner=FailingOnce())
    assert manifest["status"] == "completed_with_errors"
    assert manifest["question_errors"] == 1
    assert len(list(read_jsonl(tmp_path / "out/submission.jsonl"))) == 2
    log = loads(next((tmp_path / "out/logs").glob("000000*.json")).read_text())
    assert log["error"]["class"] == "RuntimeError"


def test_cli_from_different_directory_and_separate_evaluation(synthetic_split, tmp_path):
    output = tmp_path / "out"
    completed = subprocess.run(
        [
            "bash",
            str(ROOT / "predict.sh"),
            "--data-dir",
            str(synthetic_split),
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert loads(completed.stdout)["questions_written"] == 2
    assert (
        main(
            [
                "evaluate",
                "--answers",
                str(synthetic_split / "answers.jsonl"),
                "--predictions",
                str(output / "submission.jsonl"),
            ]
        )
        == 2
    )
    assert main(["validate", "--data-dir", str(synthetic_split)]) == 0


def test_inference_module_does_not_import_evaluator_in_fresh_process():
    code = "import sys,src.runtime; assert 'src.evaluation.scorer' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
