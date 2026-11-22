"""Business preparation and streaming label-free inference with evidence logs."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import __version__
from src.contracts import validate_answer
from src.data.catalog import prepare_business, require_output
from src.data.normalization import prepare_facts
from src.data.split import load_split, question_dict
from src.jsonio import ContractError, dumps
from src.runtime_checkpoint import (
    atomic_json,
    log_name,
    read_prefix,
    record_chain,
    run_identity,
    run_lock,
)
from src.semantics.answerability import prepare_semantics

HEARTBEAT_SECONDS = 10


@contextmanager
def _progress(manifest, path, started, lock):
    stop = threading.Event()

    def checkpoint():
        with lock:
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            manifest["elapsed_seconds"] = time.perf_counter() - started
            atomic_json(path, manifest)

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            checkpoint()
            print(
                dumps(
                    {
                        "progress": manifest["questions_written"],
                        "total": manifest["questions_total"],
                        "phase": manifest.get("phase"),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )

    class ProcessSignal(BaseException):
        pass

    def interrupted(signum, frame):
        raise ProcessSignal(f"signal {signum}")

    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, interrupted)
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        checkpoint()
        yield checkpoint
    except BaseException as exc:
        with lock:
            manifest["status"] = "interrupted"
            manifest["interruption"] = {"class": type(exc).__name__, "message": str(exc)}
        checkpoint()
        raise
    finally:
        stop.set()
        worker.join()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def run_inference(
    data_dir: Path,
    output_dir: Path,
    *,
    planner: Any,
    cache_dir: Path | None = None,
    question_ids: list[str] | None = None,
    resume_from: Path | None = None,
    resume_tail: str = "reject",
    propose: Any = None,
) -> dict[str, Any]:
    split = load_split(data_dir)
    questions = split.questions
    if question_ids is not None:
        selected = set(question_ids)
        known = {q.id for q in questions}
        if not selected or len(selected) != len(question_ids) or not selected <= known:
            raise ContractError("question selection must contain distinct known IDs")
        questions = tuple(q for q in questions if q.id in selected)
    output = output_dir.resolve()
    require_output(output_dir, split.root)
    if cache_dir is not None:
        require_output(cache_dir, split.root)
    for name in [
        "submission.jsonl",
        "run_manifest.json",
        "logs",
        "catalogs",
        "prepared",
        "run.lock",
    ]:
        if (output / name).exists() or (output / name).is_symlink():
            raise ContractError(f"output already exists: {name}; use a fresh output directory")
    adapter = planner
    started = time.perf_counter()
    identity = run_identity(split, questions, adapter)
    prefix, lineage = [], None
    if resume_from is not None:
        require_output(resume_from, split.root)
        prefix, lineage = read_prefix(resume_from, identity, questions, tail_policy=resume_tail)
    elif resume_tail != "reject":
        raise ContractError("--resume-tail requires --resume-from")
    output.mkdir(parents=True, exist_ok=True)
    logs, catalogs = output / "logs", output / "catalogs"
    logs.mkdir()
    catalogs.mkdir()
    manifest: dict[str, Any] = {
        "version": __version__,
        "diagnostics_version": "runtime-diagnostics-v2",
        "status": "running",
        "adapter": adapter.name,
        "identity": identity,
        "resume": lineage,
        "planner_metadata": getattr(adapter, "metadata", None),
        "started_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "inputs": split.summary(),
        "question_selection": question_ids,
        "questions_total": len(questions),
        "questions_written": 0,
        "question_errors": 0,
        "committed_record_chain": "",
        "preparation": {},
        "phase": "starting",
        "current_question": None,
        "durability": "log then JSONL fsync then atomic manifest; directory fsync on checkpoint; "
        "hardware/filesystem durability applies; uncommitted tail may require discard",
    }
    manifest_path = output / "run_manifest.json"
    lock = threading.RLock()
    contexts = {}
    with run_lock(output), _progress(manifest, manifest_path, started, lock) as checkpoint:
        with (output / "submission.jsonl").open("x", encoding="utf-8") as predictions:
            for answer, record, name in prefix:
                atomic_json(logs / name, record)
                predictions.write(dumps(answer) + "\n")
                catalog = catalogs / f"{record['business_id']}.json"
                if not catalog.exists():
                    shutil.copyfile(resume_from / record["catalog"], catalog)
                predictions.flush()
                os.fsync(predictions.fileno())
                with lock:
                    manifest["questions_written"] += 1
                    manifest["question_errors"] += int(record.get("error") is not None)
                    manifest["committed_record_chain"] = record_chain(
                        manifest["committed_record_chain"], answer, record
                    )
            predictions.flush()
            os.fsync(predictions.fileno())
            checkpoint()
            if hasattr(adapter, "plan_ahead"):
                adapter.plan_ahead(questions[len(prefix) :])
            for index, question in enumerate(questions[len(prefix) :], len(prefix)):
                question_start = time.perf_counter()
                error, execution, decision = None, "not_performed", None
                called_planner = False
                timings = {}
                with lock:
                    manifest.update(current_question=question.id, phase="preparation")
                phase_start = time.perf_counter()
                if question.business_id not in contexts:
                    try:
                        context = prepare_business(split, question.business_id, cache_dir=cache_dir)
                        facts = prepare_facts(
                            context,
                            output / "prepared" / question.business_id,
                            cache_dir=cache_dir,
                            propose=propose,
                        )
                        semantics = prepare_semantics(context)
                        contexts[question.business_id] = context
                        atomic_json(
                            catalogs / f"{question.business_id}.json",
                            {**context.catalog, "prepared": facts.summary, "semantics": semantics},
                        )
                        with lock:
                            manifest["preparation"][question.business_id] = {
                                "read_complete": context.catalog["read_complete"],
                                "cache_hit": context.cache_hit,
                                "elapsed_seconds": context.elapsed_seconds,
                                "normalization": facts.summary,
                                "facts": str(facts.path.relative_to(output))
                                if facts.path
                                else None,
                                "facts_cache_hit": facts.cache_hit,
                                "normalization_seconds": facts.elapsed_seconds,
                            }
                    except Exception as exc:
                        contexts[question.business_id] = None
                        diagnostic = {"class": type(exc).__name__, "message": str(exc)}
                        atomic_json(
                            catalogs / f"{question.business_id}.json", {"error": diagnostic}
                        )
                        with lock:
                            manifest["preparation"][question.business_id] = {"error": diagnostic}
                timings["preparation"] = time.perf_counter() - phase_start
                with lock:
                    manifest["phase"] = "planner"
                phase_start = time.perf_counter()
                try:
                    context = contexts[question.business_id]
                    if context is None and not getattr(
                        adapter, "accepts_missing_context", False
                    ):
                        raise ContractError("business preparation failed; see catalog diagnostic")
                    called_planner = True
                    decision = adapter.decide(question, context=context)
                    answer = decision["answer"]
                    validate_answer(answer)
                    if answer["id"] != question.id:
                        raise ContractError("planner changed question id")
                    plan = decision["plan"]
                    execution = decision.get("execution", "not_performed")
                    dumps(plan)
                    dumps(decision)
                except Exception as exc:
                    decision = None
                    answer = {"id": question.id, "type": "abstain"}
                    plan = {"status": "failed"}
                    error = {"class": type(exc).__name__, "message": str(exc)}
                timings["planner_and_execution"] = time.perf_counter() - phase_start
                record = {
                    "id": question.id,
                    "business_id": question.business_id,
                    "as_of_date": question.as_of_date,
                    "adapter": adapter.name,
                    "identity": identity["sha256"],
                    "plan": plan,
                    "answer": answer,
                    "error": error,
                    "elapsed_seconds": time.perf_counter() - question_start,
                    "timings_seconds": timings,
                    "catalog": f"catalogs/{question.business_id}.json",
                    "execution": execution,
                }
                # DataMate Docker guide §3.3: Qwen3 runs with thinking disabled and emits
                # structured plans, not a reasoning trace. Preserve its raw calls in plan.
                record.update(
                    elapsed_ms=max(0, int(record["elapsed_seconds"] * 1000)),
                    thinking=None,
                    code=(
                        f"{type(adapter).__module__.replace('.', '/')}.py::"
                        f"{type(adapter).__qualname__}.decide"
                    )
                    if called_planner
                    else None,
                    inputs={
                        "question": question_dict(question),
                        "context": {
                            "catalog": record["catalog"],
                            "prepared_facts": manifest["preparation"]
                            .get(question.business_id, {})
                            .get("facts"),
                            "rulebook": str(split.root / "rulebook.md"),
                            "calendar": str(split.root / "calendar.csv"),
                        },
                    },
                    output=decision,
                )
                with lock:
                    manifest["phase"] = "output_checkpoint"
                phase_start = time.perf_counter()
                atomic_json(logs / log_name(index, question.id), record)
                predictions.write(dumps(answer) + "\n")
                predictions.flush()
                os.fsync(predictions.fileno())
                with lock:
                    manifest["questions_written"] += 1
                    manifest["question_errors"] += int(error is not None)
                    manifest["committed_record_chain"] = record_chain(
                        manifest["committed_record_chain"], answer, record
                    )
                    manifest["last_output_checkpoint_seconds"] = time.perf_counter() - phase_start
                checkpoint()
        if run_identity(split, questions, adapter) != identity:
            raise ContractError("source/input/config changed during inference")
        with lock:
            manifest["status"] = (
                "completed" if manifest["question_errors"] == 0 else "completed_with_errors"
            )
            manifest.update(phase="finished", current_question=None)
        checkpoint()
    return manifest
