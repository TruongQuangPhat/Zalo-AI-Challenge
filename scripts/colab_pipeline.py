"""Colab setup, label-free inference and post-run scoring using the existing entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from collections import Counter
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from scripts.annotate_questions import propose
from scripts.input_data import input_files
from src.contracts import validate_answer
from src.data.cache import fingerprint
from src.data.catalog import digest, require_output
from src.data.normalization.store import _cached
from src.data.split import Question, load_split
from src.environment import require_project_environment
from src.evaluation.scorer import compare, evaluate
from src.jsonio import ContractError, dumps, loads, read_jsonl, write_json

ROOT = Path(__file__).resolve().parents[1]
MODEL_SHA = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/"
    "91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf"
)
WHEEL_NAME = "llama_cpp_python-0.3.16-cp311-cp311-linux_x86_64.whl"
WHEEL_URL = (
    "https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.16-cu124/" + WHEEL_NAME
)
WHEEL_SHA = "a455263e7608a51c26079e82fe524f9b7f1f73ee9fc563221a297de8da2a8c51"
REQUIRED_MODULES = (
    "src/agent/business_execution.py",
    "src/agent/entities.py",
    "src/data/normalization/entities.py",
    "src/semantics/business_rules.py",
)


def download(url: str, destination: Path, expected_sha: str) -> None:
    """Download setup artifacts only; unchecked bytes never replace a verified artifact."""
    if destination.is_symlink():
        raise ContractError("download destination cannot be a symlink")
    if destination.exists():
        if digest(destination) != expected_sha:
            raise ContractError(f"existing artifact checksum mismatch: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".pending")
    if pending.is_symlink():
        raise ContractError("download temporary file cannot be a symlink")
    request = urllib.request.Request(url, headers={"User-Agent": "DataMate-setup"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, pending.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        if digest(pending) != expected_sha:
            raise ContractError(f"download checksum mismatch: {destination.name}")
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def setup(directory: Path) -> None:
    require_project_environment()
    model, wheel = directory / "model.gguf", directory / WHEEL_NAME
    download(MODEL_URL, model, MODEL_SHA)
    download(WHEEL_URL, wheel, WHEEL_SHA)
    subprocess.run(
        ["uv", "pip", "install", "--python", sys.executable, "--no-index", "--no-deps", str(wheel)],
        check=True,
    )
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [
        p
        for p in lock["package"]
        if p["name"] in {"nvidia-cublas-cu12", "nvidia-cuda-runtime-cu12"}
    ]
    registries = {p["source"]["registry"] for p in packages}
    if len(packages) != 2 or len(registries) != 1:
        raise ContractError("CUDA packages must have one locked registry")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--index-url",
            registries.pop(),
            *[f"{p['name']}=={p['version']}" for p in packages],
        ],
        check=True,
    )
    write_json(
        directory / "setup.json",
        {
            "model": {"url": MODEL_URL, "sha256": MODEL_SHA},
            "wheel": {"url": WHEEL_URL, "sha256": WHEEL_SHA},
            "cuda_packages": {p["name"]: p["version"] for p in packages},
            "license_sha256": digest(ROOT / "docs/model-license.txt"),
            "uv_lock_sha256": digest(ROOT / "uv.lock"),
        },
    )


def extract_train(archive: Path, destination: Path) -> Path:
    """Extract the official train ZIP locally; labels stay outside staged inference inputs."""
    if destination.exists():
        raise ContractError("dataset extraction requires a fresh directory")
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            target = destination / entry.filename
            if (
                not target.resolve().is_relative_to(destination.resolve())
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ContractError("unsafe dataset ZIP member")
            if entry.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    candidates = [p.parent for p in destination.rglob("questions.jsonl")]
    if len(candidates) != 1:
        raise ContractError("ZIP must contain exactly one train split")
    load_split(candidates[0])
    return candidates[0]


def stage_inputs(source: Path, destination: Path, *, expected_count: int = 3000) -> dict:
    split = load_split(source)
    if len(split.questions) != expected_count:
        raise ContractError(f"expected {expected_count} questions, found {len(split.questions)}")
    if destination.resolve().is_relative_to(source.resolve()):
        raise ContractError("stage inputs outside the raw train directory")
    files = input_files(source)
    before = {name: digest(path) for name, path in files.items()}
    if destination.exists():
        actual = {name: digest(path) for name, path in input_files(destination).items()}
        if actual != before or (destination / "answers.jsonl").exists():
            raise ContractError("existing staged inputs differ; use a fresh directory")
    else:
        destination.mkdir(parents=True)
        for name, path in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o444)
    after = {name: digest(path) for name, path in input_files(destination).items()}
    if before != after:
        raise ContractError("staged input checksum mismatch")
    return {"questions": expected_count, "sha256": after, "labels_staged": False}


def source_hashes() -> dict[str, str]:
    if any(not (ROOT / name).is_file() for name in REQUIRED_MODULES):
        raise ContractError(
            "checkout lacks the S3/S4 baseline modules; publish/pull current source"
        )
    paths = sorted((ROOT / "src").rglob("*.py")) + [
        ROOT / "predict.sh",
        ROOT / "uv.lock",
        Path(__file__),
    ]
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}


def reporting_stage(question: Question) -> str:
    """Freeze reporting buckets from existing lexical proposals before gold; not execution."""
    annotation = propose(question)
    metrics = set(annotation["metrics"])
    business = {
        "receivable_balance",
        "debt_collection",
        "new_debt",
        "inventory_quantity",
        "inventory_value",
        "stockout",
        "dead_stock",
        "purchase_value",
        "purchase_quantity",
        "purchase_price",
        "supplier",
        "gross_profit",
        "net_profit",
        "cost_of_goods",
        "expiry",
        "lot_quantity",
    }
    if metrics & business:
        return "S4"
    if "ranking" in annotation["intents"] or metrics & {
        "quantity_sold",
        "customer_spend",
        "customer_count",
        "entity_attribute",
        "product_count",
    }:
        return "S3"
    if "comparison" in annotation["intents"] or metrics & {"average_order_value", "cancellation"}:
        return "S2"
    if metrics and metrics <= {"revenue", "order_count", "returns"}:
        return "S1"
    return "S5"


def cuda_environment() -> dict[str, str]:
    site = Path(sysconfig.get_path("purelib"))
    env = os.environ.copy()
    env.update(PYTHONPATH=str(ROOT), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    env["LD_LIBRARY_PATH"] = ":".join(
        [
            str(site / "nvidia/cublas/lib"),
            str(site / "nvidia/cuda_runtime/lib"),
            env.get("LD_LIBRARY_PATH", ""),
        ]
    )
    return env


def validate_part(folder: Path, ids: list[str], identity: str) -> dict:
    rows = [row for _, row in read_jsonl(folder / "submission.jsonl")]
    if [row["id"] for row in rows] != ids:
        raise ContractError("missing, duplicate, unknown or reordered prediction IDs")
    for row in rows:
        validate_answer(row)
    manifest = loads((folder / "run_manifest.json").read_text())
    model = manifest["planner_metadata"]["model"]
    if (
        manifest["adapter"] != "local-planner"
        or manifest["question_errors"]
        or manifest["questions_written"] != len(ids)
        or model["layers_offloaded"] != 29
        or model["sha256"] != MODEL_SHA
        or model["n_ctx"] != 4096
        or model["n_batch"] != 128
        or model["threads"] != 4
        or model["seed"] != 0
        or model["temperature"] != 0
        or manifest["planner_metadata"]["question_timeout_seconds"] != 120
    ):
        raise ContractError("part failed real-model/CUDA/configuration/runtime verification")
    evidence_path = folder / "verification.json"
    if evidence_path.exists():
        evidence = loads(evidence_path.read_text())
        if evidence["identity"] != identity or evidence["submission_sha256"] != digest(
            folder / "submission.jsonl"
        ):
            raise ContractError("checkpoint source/configuration/predictions changed")
    errors = Counter()
    logs = sorted((folder / "logs").glob("*.json"))
    if len(logs) != len(ids):
        raise ContractError("per-question logs are missing")
    for path, qid in zip(logs, ids, strict=True):
        record = loads(path.read_text())
        if record["id"] != qid:
            raise ContractError("log IDs differ from predictions")
        planning = record.get("plan", {})
        planning = planning.get("planning", planning)
        errors.update(a["error"] for a in planning.get("attempts", []) if a.get("error"))
    return {"questions": len(rows), "layers_offloaded": 29, "attempt_errors": dict(errors)}


def restore_checkpoint(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as source:
        for member in source:
            target = destination / member.name
            if (
                not target.resolve().is_relative_to(destination.resolve())
                or not member.isfile()
                or Path(member.name).parts[0]
                not in {
                    "submission.jsonl",
                    "run_manifest.json",
                    "verification.json",
                    "console.log",
                    "logs",
                    "catalogs",
                }
            ):
                raise ContractError("unsafe checkpoint archive member")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)


def save_checkpoint(folder: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent) as temporary:
        archive = Path(temporary) / "part.tar.gz"
        with tarfile.open(archive, "w:gz", compresslevel=3) as target:
            for path in sorted(folder.rglob("*")):
                relative = path.relative_to(folder)
                if path.is_file() and relative.parts[0] != "prepared":
                    target.add(path, arcname=relative.as_posix(), recursive=False)
        pending = destination.with_name(destination.name + ".pending")
        shutil.copyfile(archive, pending)
        pending.replace(destination)


def remove_prepared_copies(folder: Path, cache: Path) -> dict:
    """Only finished-process copies with exact, validated canonical cache matches."""
    removed = {}
    for facts in sorted((folder / "prepared").glob("*/facts.sqlite")):
        with closing(sqlite3.connect(facts.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            dep = loads(
                conn.execute("SELECT value FROM metadata WHERE key='dependencies'").fetchone()[0]
            )
        entry = cache / "prepared" / fingerprint(dep)
        canonical = _cached(entry, dep)
        if canonical is None or digest(facts) != digest(canonical[0]):
            raise ContractError(
                "refusing to remove a prepared copy without matching canonical cache"
            )
        removed[facts.parent.name] = {"sha256": digest(facts), "bytes": facts.stat().st_size}
        facts.unlink()
        if not any(facts.parent.iterdir()):
            facts.parent.rmdir()
    return removed


def run_pipeline(
    data: Path, output: Path, cache: Path, model: Path, checkpoint: Path, *, chunk_size: int = 100
) -> None:
    require_project_environment()
    for path in (output, cache, checkpoint):
        require_output(path, data)
    if digest(model) != MODEL_SHA or model.is_symlink():
        raise ContractError("run requires the verified local baseline GGUF")
    if not 1 <= chunk_size <= 3000 or (data / "answers.jsonl").exists():
        raise ContractError("use label-free staged inputs and chunk_size1..3000")
    split = load_split(data)
    if len(split.questions) != 3000:
        raise ContractError("full train requires all3000 questions")
    output.mkdir(parents=True, exist_ok=True)
    before = {name: digest(path) for name, path in input_files(data).items()}
    source = source_hashes()
    selection = {
        "inputs": before,
        "source": source,
        "model_sha256": MODEL_SHA,
        "chunk_size": chunk_size,
        "questions": [asdict(q) | {"stage": reporting_stage(q)} for q in split.questions],
    }
    identity = hashlib.sha256(dumps(selection).encode()).hexdigest()
    write_json(output / "selection.json", selection)
    ids = [q.id for q in split.questions]
    parts = []
    started = time.monotonic()
    for index, start in enumerate(range(0, len(ids), chunk_size)):
        selected = ids[start : start + chunk_size]
        folder = output / "parts" / f"{index:03d}"
        archived = checkpoint / f"{index:03d}.tar.gz"
        if archived.exists():
            restore_checkpoint(archived, folder)
            if not (folder / "verification.json").is_file():
                raise ContractError("incomplete checkpoint")
            validate_part(folder, selected, identity)
            print(f"resumed {index}: {len(selected)} verified outputs", flush=True)
        else:
            if folder.exists():
                folder.rename(folder.with_name(folder.name + f".incomplete-{time.time_ns()}"))
            folder.mkdir(parents=True)
            cmd = [
                "bash",
                str(ROOT / "predict.sh"),
                "--data-dir",
                str(data),
                "--output-dir",
                str(folder),
                "--cache-dir",
                str(cache),
                "--model",
                str(model),
                "--gpu-layers",
                "-1",
                "--model-threads",
                "4",
                "--question-timeout",
                "120",
            ]
            for qid in selected:
                cmd += ["--question-id", qid]
            part_started = time.monotonic()
            gpu_peak = 0
            with (folder / "console.log").open("w") as log:
                process = subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    env=cuda_environment(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    while process.poll() is None:
                        memory = subprocess.check_output(
                            [
                                "nvidia-smi",
                                "--query-gpu=memory.used",
                                "--format=csv,noheader,nounits",
                            ],
                            text=True,
                            timeout=10,
                        )
                        gpu_peak = max(gpu_peak, max(int(v) for v in memory.splitlines()))
                        path = folder / "submission.jsonl"
                        count = len(path.read_bytes().splitlines()) if path.exists() else 0
                        print(f"part {index}: {count}/{len(selected)}", flush=True)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass
                    if process.returncode:
                        raise ContractError(f"predict failed; inspect {folder / 'console.log'}")
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=15)
            details = validate_part(folder, selected, identity)
            if source_hashes() != source or before != {
                name: digest(path) for name, path in input_files(data).items()
            }:
                raise ContractError("source or inputs changed during inference")
            write_json(
                folder / "verification.json",
                details
                | {
                    "identity": identity,
                    "submission_sha256": digest(folder / "submission.jsonl"),
                    "wall_seconds": time.monotonic() - part_started,
                    "gpu_peak_mib_sampled": gpu_peak,
                    "prepared_copies_removed": remove_prepared_copies(folder, cache),
                },
            )
            save_checkpoint(folder, archived)
        parts.append(folder)
    submission = output / "submission.jsonl"
    with submission.open("wb") as merged:
        for folder in parts:
            with (folder / "submission.jsonl").open("rb") as incoming:
                shutil.copyfileobj(incoming, merged)
    if [r["id"] for _, r in read_jsonl(submission)] != ids:
        raise ContractError("full train merge does not contain exactly all3000 IDs")
    if source_hashes() != source or before != {
        name: digest(path) for name, path in input_files(data).items()
    }:
        raise ContractError("source or input immutability check failed")
    write_json(
        output / "verification.json",
        {
            "complete": True,
            "identity": identity,
            "questions": 3000,
            "submission_sha256": digest(submission),
            "selection_sha256": digest(output / "selection.json"),
            "source": source,
            "inputs_unchanged": True,
            "source_unchanged": True,
            "execution": "sequential local CUDA processes; no inference API; Colab is not Docker",
            "chunk_size": chunk_size,
            "this_session_wall_seconds": time.monotonic() - started,
            "process_seconds_including_loading_preparation": sum(
                loads((p / "verification.json").read_text())["wall_seconds"] for p in parts
            ),
        },
    )
    shutil.copyfile(submission, checkpoint / "submission.jsonl")
    shutil.copyfile(output / "verification.json", checkpoint / "verification.json")


def summary(records: list[dict], evaluation_id: str) -> dict:
    counts = Counter(q["results"][evaluation_id]["verdict"] for q in records)
    return {
        "native": {
            "total": len(records),
            "correct": counts["correct"],
            "wrong": counts["wrong"],
            "abstain": counts["abstain"],
            "points": counts["correct"] - counts["wrong"],
        }
    }


def evaluate_pipeline(
    answers: Path, run: Path, report_path: Path, evaluation_id: str = "colab_train_3000"
) -> dict:
    """Gold is opened only after exact-count, checksum and completion verification."""
    if (run / "RUNNING").exists():
        raise ContractError("inference is active; do not read answers yet")
    evidence = loads((run / "verification.json").read_text())
    if (
        not evidence.get("complete")
        or evidence["questions"] != 3000
        or evidence["submission_sha256"] != digest(run / "submission.jsonl")
        or evidence["selection_sha256"] != digest(run / "selection.json")
        or evidence["source"] != source_hashes()
    ):
        raise ContractError("finish verified fulltrain3000 inference before reading answers")
    selection = loads((run / "selection.json").read_text())
    predictions = [row for _, row in read_jsonl(run / "submission.jsonl")]
    if len(predictions) != 3000 or [r["id"] for r in predictions] != [
        q["id"] for q in selection["questions"]
    ]:
        raise ContractError("full train IDs differ from frozen selection")
    for row in predictions:
        validate_answer(row)
    if report_path.resolve().is_relative_to(answers.parent.resolve()):
        raise ContractError("report must be outside the label directory")
    # First label-reading operation. All3000 scoring is explicitly requested by the user.
    answer_sha = digest(answers)
    score = evaluate(answers, run / "submission.jsonl")
    if score["total_questions"] != 3000 or score["missing"] or score["malformed"]:
        raise ContractError("evaluation requires exact3000 gold/predictions, no malformed/missing")
    gold = {row["id"]: row for _, row in read_jsonl(answers)}
    if report_path.resolve() in {answers.resolve(), (run / "submission.jsonl").resolve()}:
        raise ContractError("report cannot overwrite labels or predictions")
    report = (
        loads(report_path.read_text())
        if report_path.exists()
        else {
            "version": "stage-results-v1",
            "evaluations": {},
            "stages": {},
            "notes": [],
        }
    )
    if evaluation_id in report["evaluations"]:
        raise ContractError("evaluation ID already exists; use a new ID for an independent run")
    report["evaluations"][evaluation_id] = {
        "method": "llm_cuda",
        "repetitions": 1,
        "description": "All3000 train scored after local GPU inference; sequential Colab chunks. "
        "Stage buckets are lexical proposals frozen before gold, not acceptance "
        "of every capability. Previously heldout1224 are now scored by user request.",
    }
    records = []
    for question, actual in zip(selection["questions"], predictions, strict=True):
        expected = gold[question["id"]]
        verdict = (
            "abstain"
            if actual["type"] == "abstain"
            else ("correct" if compare(actual, expected) else "wrong")
        )
        records.append(
            {
                **{k: v for k, v in question.items() if k != "stage"},
                "case_type": "native",
                "expected_answer": {k: v for k, v in expected.items() if k not in {"id", "alias"}},
                "results": {
                    evaluation_id: {
                        "actual_answer": {k: v for k, v in actual.items() if k != "id"},
                        "verdict": verdict,
                        "points": {"correct": 1, "wrong": -1, "abstain": 0}[verdict],
                    }
                },
            }
        )
    # Existing historical records and summaries remain intact, including replay/fixture evidence.
    for stage in ("S1", "S2", "S3", "S4", "S5", "S6"):
        data = report["stages"].setdefault(
            stage,
            {
                "primary_evaluation": evaluation_id,
                "summaries": {},
                "questions": [],
            },
        )
        data["primary_evaluation"] = evaluation_id
        indexed = {q["id"]: q for q in data["questions"]}
        selected = [
            r
            for r, q in zip(records, selection["questions"], strict=True)
            if stage == "S6" or q["stage"] == stage
        ]
        for record in selected:
            existing = indexed.get(record["id"])
            if existing:
                if existing["expected_answer"] != record["expected_answer"]:
                    raise ContractError(
                        "historical expected answer differs; do not rewrite history"
                    )
                existing["results"][evaluation_id] = deepcopy(record["results"][evaluation_id])
            else:
                data["questions"].append(deepcopy(record))
        data["summaries"][evaluation_id] = summary(selected, evaluation_id)
    aggregate = report["stages"]["S6"]["summaries"][evaluation_id]["native"]
    if any(aggregate[k] != score[k] for k in ("correct", "wrong", "abstain")):
        raise ContractError("canonical report and scorer disagree")
    report["notes"].append(
        "Colab fulltrain3000: user authorized scoring all labels. Dev/validation/final memberships "
        "unchanged; none remain unscored in this evaluation. S6 repeats S1–S5, not extra samples. "
        "No claim of Docker/Blackwell/SLA acceptance from Colab alone."
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if digest(answers) != answer_sha:
        raise ContractError("answers changed during evaluation")
    write_json(report_path, report)
    write_json(
        run / "evaluation-verification.json",
        {
            "answers_sha256": answer_sha,
            "report_sha256": digest(report_path),
            "evaluation_id": evaluation_id,
            "scored": 3000,
            "prior_holdout_now_scored_by_user_request": True,
        },
    )
    return {
        k: score[k]
        for k in (
            "total_questions",
            "correct",
            "wrong",
            "abstain",
            "missing",
            "malformed",
            "pa_full",
            "coverage",
            "accuracy_answered",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("setup")
    prepare.add_argument("--directory", type=Path, required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--train-dir", type=Path, required=True)
    stage.add_argument("--data-dir", type=Path, required=True)
    run = commands.add_parser("run")
    for name in ("data-dir", "output-dir", "cache-dir", "model", "checkpoint-dir"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--chunk-size", type=int, default=100)
    scoring = commands.add_parser("evaluate")
    scoring.add_argument("--answers", type=Path, required=True)
    scoring.add_argument("--run-dir", type=Path, required=True)
    scoring.add_argument("--report", type=Path, default=ROOT / "reports/stage_results.json")
    scoring.add_argument("--evaluation-id", default="colab_train_3000")
    args = parser.parse_args()
    if args.command == "setup":
        setup(args.directory)
    elif args.command == "extract":
        print(extract_train(args.archive, args.destination))
    elif args.command == "stage":
        print(dumps(stage_inputs(args.train_dir, args.data_dir)))
    elif args.command == "run":

        def interrupted(_signal, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        require_output(args.output_dir, args.data_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        marker = args.output_dir / "RUNNING"
        with marker.open("x") as handle:
            handle.write(str(os.getpid()))
        try:
            run_pipeline(
                args.data_dir,
                args.output_dir,
                args.cache_dir,
                args.model,
                args.checkpoint_dir,
                chunk_size=args.chunk_size,
            )
        finally:
            marker.unlink(missing_ok=True)
    else:
        print(dumps(evaluate_pipeline(args.answers, args.run_dir, args.report, args.evaluation_id)))


if __name__ == "__main__":
    main()
