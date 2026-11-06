"""Recovery must preserve a verified prefix, revision identity and runtime errors."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.runtime as runtime
from src.jsonio import ContractError, loads, write_json
from tests.helpers import StubPlanner
from tests.test_split_runtime import snapshot


class InterruptingPlanner(StubPlanner):
    def __init__(self):
        self.interrupt = True
        self.calls = []

    def decide(self, question, *, context=None):
        self.calls.append(question.id)
        if self.interrupt and question.id == "q_two":
            raise KeyboardInterrupt("synthetic stop")
        return super().decide(question, context=context)


def interrupted(root: Path, output: Path):
    planner = InterruptingPlanner()
    with pytest.raises(KeyboardInterrupt):
        runtime.run_inference(root, output, planner=planner)
    planner.interrupt = False
    return planner


def test_progress_and_resume_use_complete_records_preserve_parent_and_labels(
    synthetic_split, tmp_path, monkeypatch
):
    before = snapshot(synthetic_split)
    parent = tmp_path / "parent"
    planner = interrupted(synthetic_split, parent)
    manifest = loads((parent / "run_manifest.json").read_text())
    assert manifest["status"] == "interrupted"
    assert manifest["questions_written"] == 1
    assert manifest["elapsed_seconds"] > 0
    assert manifest["updated_at"]
    original = snapshot(parent)
    planner.calls.clear()
    result = runtime.run_inference(
        synthetic_split, tmp_path / "recovered", planner=planner, resume_from=parent
    )
    assert planner.calls == ["q_two"]
    assert result["questions_written"] == 2
    assert result["status"] == "completed"
    assert result["resume"]["records_recovered"] == 1
    assert snapshot(parent) == original
    assert snapshot(synthetic_split) == before


@pytest.mark.parametrize("boundary", ["log_only", "submission_before_checkpoint", "checkpoint"])
def test_interrupt_at_each_commit_boundary(synthetic_split, tmp_path, monkeypatch, boundary):
    parent = tmp_path / boundary
    real_write = runtime.atomic_json
    triggered = False

    def interrupt_write(path, value):
        nonlocal triggered
        if not triggered:
            if boundary == "log_only" and path.parent.name == "logs":
                triggered = True
                real_write(path, value)
                raise KeyboardInterrupt("log committed only")
            if path.name == "run_manifest.json" and value["questions_written"] == 1:
                triggered = True
                if boundary == "checkpoint":
                    real_write(path, value)
                raise KeyboardInterrupt("record committed")
        real_write(path, value)

    monkeypatch.setattr(runtime, "atomic_json", interrupt_write)
    with pytest.raises(KeyboardInterrupt):
        runtime.run_inference(synthetic_split, parent, planner=StubPlanner())
    monkeypatch.setattr(runtime, "atomic_json", real_write)
    result = runtime.run_inference(
        synthetic_split, tmp_path / "next", resume_from=parent, planner=StubPlanner()
    )
    assert result["status"] == "completed"
    assert result["questions_written"] == 2
    assert result["resume"]["records_recovered"] == (0 if boundary == "log_only" else 1)


@pytest.mark.parametrize("tail", [b'{"id":', b'{"id":"q_two","type":"abstain"}'])
def test_unterminated_tail_requires_explicit_discard_and_parent_is_immutable(
    synthetic_split, tmp_path, tail
):
    parent = tmp_path / "parent"
    planner = interrupted(synthetic_split, parent)
    with (parent / "submission.jsonl").open("ab") as f:
        f.write(tail)
    original = snapshot(parent)
    with pytest.raises(ContractError, match="unterminated"):
        runtime.run_inference(
            synthetic_split, tmp_path / "reject", planner=planner, resume_from=parent
        )
    assert not (tmp_path / "reject").exists()
    result = runtime.run_inference(
        synthetic_split,
        tmp_path / "discard",
        planner=planner,
        resume_from=parent,
        resume_tail="discard",
    )
    assert result["questions_written"] == 2
    assert result["resume"]["discarded_tail_bytes"] == len(tail)
    assert snapshot(parent) == original


@pytest.mark.parametrize(
    "corruption", ["malformed", "duplicate", "unknown", "order", "log", "null", "[]", '"text"']
)
def test_reject_corrupt_complete_prefix(synthetic_split, tmp_path, corruption):
    parent = tmp_path / "parent"
    runtime.run_inference(synthetic_split, parent, planner=StubPlanner())
    path = parent / "submission.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    if corruption == "malformed":
        lines[0] = "{bad}\n"
    elif corruption in {"null", "[]", '"text"'}:
        lines[0] = corruption + "\n"
    elif corruption == "duplicate":
        lines[1] = lines[0]
    elif corruption == "unknown":
        lines[0] = lines[0].replace("q_one", "unknown")
    elif corruption == "order":
        lines.reverse()
    else:
        log = sorted((parent / "logs").glob("*.json"))[0]
        value = loads(log.read_text())
        value["plan"]["reason"] = "corrupted metadata"
        write_json(log, value)
    path.write_text("".join(lines))
    with pytest.raises(ContractError):
        runtime.run_inference(
            synthetic_split, tmp_path / "reject", resume_from=parent, planner=StubPlanner()
        )
    assert not (tmp_path / "reject").exists()


@pytest.mark.parametrize("mismatch", ["source", "parser", "normalizer", "model", "config", "input"])
def test_resume_requires_all_identity_components(synthetic_split, tmp_path, mismatch):
    parent = tmp_path / "parent"
    runtime.run_inference(synthetic_split, parent, planner=StubPlanner())
    path = parent / "run_manifest.json"
    manifest = loads(path.read_text())
    manifest["identity"]["dependencies"][mismatch] = "another revision"
    write_json(path, manifest)
    with pytest.raises(ContractError, match="identity"):
        runtime.run_inference(
            synthetic_split, tmp_path / "reject", resume_from=parent, planner=StubPlanner()
        )


def test_actual_source_change_and_missing_legacy_proof_fail_before_writing(
    synthetic_split, tmp_path
):
    parent = tmp_path / "parent"
    runtime.run_inference(synthetic_split, parent, planner=StubPlanner())
    source = synthetic_split / "data/shop_a/sales.csv"
    source.write_text(source.read_text() + "order_2,9\n")
    with pytest.raises(ContractError, match="identity"):
        runtime.run_inference(
            synthetic_split, tmp_path / "changed", resume_from=parent, planner=StubPlanner()
        )
    manifest_path = parent / "run_manifest.json"
    manifest = loads(manifest_path.read_text())
    del manifest["identity"]
    write_json(manifest_path, manifest)
    with pytest.raises(ContractError, match="proof"):
        runtime.run_inference(
            synthetic_split, tmp_path / "legacy", resume_from=parent, planner=StubPlanner()
        )


def test_resume_preserves_errors_in_merged_output(synthetic_split, tmp_path):
    class Failure(StubPlanner):
        def decide(self, question, *, context=None):
            if question.id == "q_one":
                raise RuntimeError("synthetic error")
            return super().decide(question, context=context)

    parent = tmp_path / "parent"
    planner = Failure()
    runtime.run_inference(synthetic_split, parent, planner=planner)
    result = runtime.run_inference(
        synthetic_split, tmp_path / "merged", resume_from=parent, planner=planner
    )
    assert result["status"] == "completed_with_errors"
    assert result["question_errors"] == 1
    assert result["questions_written"] == 2


def test_live_heartbeat_updates_atomic_manifest_and_uses_stderr(
    synthetic_split, tmp_path, monkeypatch, capsys
):
    import threading

    observed = threading.Event()
    output = tmp_path / "live"
    original = runtime.atomic_json

    def observe(path, value):
        original(path, value)
        if (
            path.name == "run_manifest.json"
            and value.get("phase") == "planner"
            and value["current_question"] == "q_two"
        ):
            assert loads(path.read_text())["questions_written"] == 1
            observed.set()

    class WaitForHeartbeat(StubPlanner):
        def decide(self, question, *, context=None):
            if question.id == "q_two":
                assert observed.wait(3), "no heartbeat while planner was busy"
            return super().decide(question, context=context)

    monkeypatch.setattr(runtime, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(runtime, "atomic_json", observe)
    runtime.run_inference(synthetic_split, output, planner=WaitForHeartbeat())
    captured = capsys.readouterr()
    assert not captured.out
    assert '"progress":1' in captured.err


def test_resume_rejects_live_parent(synthetic_split, tmp_path):
    parent = tmp_path / "parent"
    runtime.run_inference(synthetic_split, parent, planner=StubPlanner())
    # Use a separate exclusive descriptor, as an active inference process does.
    import fcntl

    with (parent / "run.lock").open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ContractError, match="still running"):
            runtime.run_inference(
                synthetic_split, tmp_path / "reject", resume_from=parent, planner=StubPlanner()
            )
    assert not (tmp_path / "reject").exists()


def test_stale_checkpoint_recovers_matching_complete_records(synthetic_split, tmp_path):
    parent = tmp_path / "parent"
    planner = interrupted(synthetic_split, parent)
    path = parent / "run_manifest.json"
    manifest = loads(path.read_text())
    manifest.update(status="running", questions_written=0, committed_record_chain="")
    write_json(path, manifest)
    result = runtime.run_inference(
        synthetic_split, tmp_path / "recovered", planner=planner, resume_from=parent
    )
    assert result["resume"]["stale_checkpoint_records"] == 1
    assert result["questions_written"] == 2


@pytest.mark.parametrize("component", ["model", "timeout", "gpu_layers"])
def test_changed_actual_adapter_metadata_refuses_resume(synthetic_split, tmp_path, component):
    class ConfiguredPlanner(StubPlanner):
        def __init__(self, value):
            self.metadata = {"model": {"sha256": "0" * 64}, "timeout": 120, "gpu_layers": 0}
            if component == "model":
                self.metadata["model"]["sha256"] = value * 64
            else:
                self.metadata[component] = int(value)

    parent = tmp_path / "parent"
    runtime.run_inference(synthetic_split, parent, planner=ConfiguredPlanner("1"))
    with pytest.raises(ContractError, match="identity"):
        runtime.run_inference(
            synthetic_split, tmp_path / "reject", resume_from=parent, planner=ConfiguredPlanner("2")
        )


def test_sigterm_records_interrupted_state_without_claiming_completion(synthetic_split, tmp_path):
    import subprocess
    import sys

    output = tmp_path / "terminated"
    code = """
import signal
import sys
from pathlib import Path
from tests.helpers import StubPlanner
from src.runtime import run_inference
class Stop(StubPlanner):
    def decide(self, question, *, context=None):
        signal.raise_signal(signal.SIGTERM)
try:
    run_inference(Path(sys.argv[1]), Path(sys.argv[2]), planner=Stop())
except BaseException:
    sys.exit(130)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(synthetic_split), str(output)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 130
    manifest = loads((output / "run_manifest.json").read_text())
    assert manifest["status"] == "interrupted"
    assert manifest["questions_written"] == 0
    assert "signal" in manifest["interruption"]["message"]
