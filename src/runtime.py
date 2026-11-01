"""Streaming, label-free Phase 0 inference with per-question evidence logs."""

from __future__ import annotations

import hashlib
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import __version__
from src.agent.mock import MockPlanner
from src.contracts import validate_answer
from src.data.split import inside, load_split
from src.jsonio import ContractError, dumps, write_json


def run_inference(data_dir: Path, output_dir: Path, *, planner: Any = None) -> dict[str, Any]:
    split = load_split(data_dir)
    output = output_dir.resolve()
    if inside(output, split.root):
        raise ContractError("output-dir must be outside the read-only input split")
    output.mkdir(parents=True, exist_ok=True)
    for name in ["submission.jsonl", "run_manifest.json", "logs"]:
        if (output / name).exists() or (output / name).is_symlink():
            raise ContractError(f"output already exists: {name}; use a fresh output directory")
    logs = output / "logs"
    logs.mkdir()
    adapter = planner if planner is not None else MockPlanner()
    started = time.perf_counter()
    manifest: dict[str, Any] = {
        "version": __version__,
        "status": "running",
        "adapter": adapter.name,
        "is_mock": adapter.is_mock,
        "started_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "inputs": split.summary(),
        "questions_written": 0,
        "question_errors": 0,
    }
    manifest_path = output / "run_manifest.json"
    write_json(manifest_path, manifest)
    with (output / "submission.jsonl").open("x", encoding="utf-8") as predictions:
        for index, question in enumerate(split.questions):
            question_start = time.perf_counter()
            error = None
            try:
                decision = adapter.decide(question)
                answer = decision["answer"]
                validate_answer(answer)
                if answer["id"] != question.id:
                    raise ContractError("planner changed question id")
                plan = decision["plan"]
                dumps(plan)
            except Exception as exc:  # One question failure must not discard previous output.
                answer = {"id": question.id, "type": "abstain"}
                plan = {"status": "failed"}
                error = {"class": type(exc).__name__, "message": str(exc)}
                manifest["question_errors"] += 1
            record = {
                "id": question.id,
                "business_id": question.business_id,
                "as_of_date": question.as_of_date,
                "adapter": adapter.name,
                "is_mock": adapter.is_mock,
                "plan": plan,
                "answer": answer,
                "error": error,
                "elapsed_seconds": time.perf_counter() - question_start,
                "execution": "not_performed_in_phase0",
            }
            digest = hashlib.sha256(question.id.encode()).hexdigest()[:16]
            write_json(logs / f"{index:06d}_{digest}.json", record)
            predictions.write(dumps(answer) + "\n")
            predictions.flush()
            manifest["questions_written"] += 1
        os.fsync(predictions.fileno())
    manifest["status"] = (
        "completed" if manifest["question_errors"] == 0 else "completed_with_errors"
    )
    manifest["elapsed_seconds"] = time.perf_counter() - started
    write_json(manifest_path, manifest)
    return manifest
