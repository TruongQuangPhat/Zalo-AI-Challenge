"""Business preparation and streaming label-free inference with evidence logs."""

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
from src.data.catalog import prepare_business, require_output
from src.data.split import load_split
from src.jsonio import ContractError, dumps, write_json


def run_inference(
    data_dir: Path, output_dir: Path, *, planner: Any = None, cache_dir: Path | None = None
) -> dict[str, Any]:
    split = load_split(data_dir)
    output = output_dir.resolve()
    require_output(output_dir, split.root)
    if cache_dir is not None:
        require_output(cache_dir, split.root)
    output.mkdir(parents=True, exist_ok=True)
    for name in ["submission.jsonl", "run_manifest.json", "logs", "catalogs"]:
        if (output / name).exists() or (output / name).is_symlink():
            raise ContractError(f"output already exists: {name}; use a fresh output directory")
    logs = output / "logs"
    logs.mkdir()
    catalogs = output / "catalogs"
    catalogs.mkdir()
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
        "preparation": {},
    }
    manifest_path = output / "run_manifest.json"
    write_json(manifest_path, manifest)
    contexts = {}
    with (output / "submission.jsonl").open("x", encoding="utf-8") as predictions:
        for index, question in enumerate(split.questions):
            question_start = time.perf_counter()
            error = None
            if question.business_id not in contexts:
                try:
                    context = prepare_business(split, question.business_id, cache_dir=cache_dir)
                    contexts[question.business_id] = context
                    write_json(catalogs / f"{question.business_id}.json", context.catalog)
                    manifest["preparation"][question.business_id] = {
                        "read_complete": context.catalog["read_complete"],
                        "cache_hit": context.cache_hit,
                        "elapsed_seconds": context.elapsed_seconds,
                    }
                except Exception as exc:
                    contexts[question.business_id] = None
                    diagnostic = {"class": type(exc).__name__, "message": str(exc)}
                    write_json(catalogs / f"{question.business_id}.json", {"error": diagnostic})
                    manifest["preparation"][question.business_id] = {"error": diagnostic}
            try:
                context = contexts[question.business_id]
                if context is None:
                    raise ContractError("business preparation failed; see catalog diagnostic")
                decision = adapter.decide(question, context=context)
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
                "catalog": f"catalogs/{question.business_id}.json",
                "execution": "not_performed",
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
