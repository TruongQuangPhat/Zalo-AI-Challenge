"""Offline runtime verifier; temporary predictions/logs are removed after every run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from packaging.requirements import Requirement

from scripts.build_context import build_context
from scripts.probe_environment import probe
from src.contracts import validate_answer
from src.data.split import load_split
from src.environment import require_project_environment
from src.evaluation.scorer import evaluate
from src.jsonio import ContractError, dumps, loads, read_jsonl, write_json

ROOT = Path(__file__).resolve().parents[1]


def source_hashes(data_dirs: list[Path]) -> dict[str, str]:
    return {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in data_dirs
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def check_toolchain() -> dict:
    require_project_environment()
    versions = {}
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    for line in config["dependency-groups"]["dev"]:
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        package = requirement.name
        expected = locked[package]
        if str(requirement.specifier) != "==" + expected:
            raise ContractError(f"development dependency and uv.lock disagree: {line}")
        actual = importlib.metadata.version(package)
        if actual != expected:
            raise ContractError(f"toolchain mismatch: {package} {actual} != {expected}")
        versions[package] = actual
    return versions


def check_real_splits(data_dirs: list[Path], run_root: Path) -> dict:
    before = source_hashes(data_dirs)
    splits = []
    train_predictions = None
    for index, directory in enumerate(data_dirs):
        split = load_split(directory)
        outputs = []
        for repeat in range(2):
            output = run_root / f"split-{index}-repeat-{repeat}"
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "predict.sh"),
                    "--data-dir",
                    str(directory.resolve()),
                    "--output-dir",
                    str(output),
                ],
                cwd=run_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ContractError(f"real-split inference failed: {result.stderr}")
            manifest = loads((output / "run_manifest.json").read_text())
            records = [r for _, r in read_jsonl(output / "submission.jsonl")]
            if [r["id"] for r in records] != [q.id for q in split.questions]:
                raise ContractError("prediction IDs/order differ from input")
            for record in records:
                validate_answer(record)
                if record["type"] != "abstain":
                    raise ContractError("Mock runtime unexpectedly answered")
            logs = sorted((output / "logs").glob("*.json"))
            if len(logs) != len(records) or manifest["question_errors"] != 0:
                raise ContractError("question logs/errors differ from clean-run contract")
            for path, question in zip(logs, split.questions, strict=True):
                log = loads(path.read_text())
                if log["id"] != question.id or not log["is_mock"] or log["error"] is not None:
                    raise ContractError("invalid per-question evidence")
            outputs.append(output / "submission.jsonl")
        if outputs[0].read_bytes() != outputs[1].read_bytes():
            raise ContractError("predictions changed between identical runs")
        splits.append(
            {
                "split": directory.name,
                "questions": len(split.questions),
                "businesses": len(split.businesses),
                "repeats": 2,
                "predictions_sha256": hashlib.sha256(outputs[0].read_bytes()).hexdigest(),
                "status": "passed",
            }
        )
        if directory.name == "train":
            train_predictions = outputs[0]
    if source_hashes(data_dirs) != before:
        raise ContractError("source files changed during acceptance")
    if train_predictions is None:
        raise ContractError("train predictions required for full-denominator acceptance")
    # Labels are read only here, after both isolated inference subprocesses have exited.
    train = next(d for d in data_dirs if d.name == "train")
    scoring = evaluate(train / "answers.jsonl", train_predictions)
    if not (
        scoring["pa_full"] == 0
        and scoring["coverage"] == 0
        and scoring["accuracy_answered"] is None
        and scoring["malformed"] == 0
        and scoring["missing"] == 0
        and scoring["abstain"] == scoring["total_questions"]
    ):
        raise ContractError("all-abstain evaluator acceptance failed")
    return {
        "splits": splits,
        "source_files_unchanged": len(before),
        "scoring": {k: v for k, v in scoring.items() if k != "details"},
    }


def checked(argv: list[str], timeout: int = 120) -> str:
    result = subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise ContractError(f"{argv[0]} failed: {(result.stderr or result.stdout)[-4000:]}")
    return result.stdout.strip()


def check_quality(work: Path) -> dict:
    checked([sys.executable, "-m", "ruff", "check", "--no-cache", "src", "scripts", "tests"])
    checked([sys.executable, "-m", "ruff", "format", "--check", "src", "scripts", "tests"])
    result = checked(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(work / "pytest"),
        ]
    )
    return {"lint": "passed", "format": "passed", "tests": result.splitlines()[-1]}


def check_docker(work: Path) -> dict:
    base = os.environ.get("DATAMATE_BASE_IMAGE", "python:3.11-slim-bookworm")
    checked(["docker", "image", "inspect", base])  # Never pull implicitly.
    context = work / "context.tar"
    build_context(ROOT, context)
    with context.open("rb") as stream:
        build = subprocess.run(
            [
                "docker",
                "build",
                "--network",
                "none",
                "--pull=false",
                "--build-arg",
                f"BASE_IMAGE={base}",
                "-t",
                "datamate-runtime-check:local",
                "-",
            ],
            stdin=stream,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    if build.returncode:
        raise ContractError(f"Docker build failed: {build.stderr[-4000:]}")
    output = work / "docker-result"
    output.mkdir()
    name = f"datamate-runtime-check-{uuid4().hex[:12]}"
    mounts = [
        "--mount",
        f"type=bind,source={ROOT / 'dataset/train'},target=/data,readonly",
        "--mount",
        f"type=bind,source={output},target=/result",
    ]
    restrictions = ["--network", "none", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}"]
    try:
        checked(
            [
                "docker",
                "run",
                "--name",
                name,
                *restrictions,
                "--tmpfs",
                "/tmp:rw,nosuid,noexec,size=64m",
                *mounts,
                "datamate-runtime-check:local",
            ],
            timeout=1800,
        )
        config = loads(checked(["docker", "inspect", "--format", "{{json .HostConfig}}", name]))
        bindings = loads(checked(["docker", "inspect", "--format", "{{json .Mounts}}", name]))
        if config["NetworkMode"] != "none" or not config["ReadonlyRootfs"]:
            raise ContractError("Docker network/root restrictions differ from contract")
        permissions = {binding["Destination"]: binding["RW"] for binding in bindings}
        if permissions.get("/data") is not False or permissions.get("/result") is not True:
            raise ContractError("Docker bind mounts differ from contract")
        checked(
            [
                "docker",
                "run",
                "--rm",
                *restrictions,
                *mounts,
                "--entrypoint",
                "python3",
                "datamate-runtime-check:local",
                "-c",
                "import errno,pathlib,sys\n"
                "try: pathlib.Path('/data/.runtime-readonly-probe').write_text('probe')\n"
                "except OSError as exc: sys.exit(0 if exc.errno == errno.EROFS else 1)\n"
                "sys.exit(1)",
            ]
        )
        manifest = loads((output / "run_manifest.json").read_text())
        score = evaluate(ROOT / "dataset/train/answers.jsonl", output / "submission.jsonl")
        expected = len(load_split(ROOT / "dataset/train").questions)
        if not (
            manifest["status"] == "completed"
            and manifest["question_errors"] == 0
            and manifest["questions_written"] == expected
            and len(list((output / "logs").glob("*.json"))) == expected
            and score["total_questions"] == score["abstain"] == expected
            and score["pa_full"] == score["coverage"] == 0
            and score["missing"] == score["malformed"] == 0
            and score["accuracy_answered"] is None
        ):
            raise ContractError("Docker output/logs/scoring check failed")
        return {
            "network": "none",
            "input": "read-only",
            "output": "writable",
            "questions": expected,
        }
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)


def check_gpu() -> dict:
    result = probe()
    # Docker has its own gate; a daemon-info timeout does not invalidate a CUDA tensor.
    required = ["gpu", "torch_cuda"]
    if any(result[key]["status"] != "passed" for key in required):
        raise ContractError(dumps({key: result[key] for key in required}))
    return {
        "device": result["gpu"]["stdout"],
        "torch_cuda": loads(result["torch_cuda"]["stdout"]),
    }


def check_llm_image(image: str, gpu_layers: int, work: Path, selection: dict) -> dict:
    """Run the frozen common-dev subset twice. Labels never enter the container."""
    data = ROOT / "dataset/train"
    split = load_split(data)
    protocol = loads((ROOT / "config/validation_split.json").read_text())
    expected = selection["expected_intents_and_windows"]
    for partition in protocol["protocols"].values():
        if not set(expected) <= set(partition["partitions"]["dev"]["question_ids"]):
            raise ContractError("LLM acceptance selection must belong to both dev partitions")
    questions = [q for q in split.questions if q.id in expected]
    if len(questions) != len(expected):
        raise ContractError("frozen LLM selection no longer matches questions")
    image_id = checked(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    inventory_code = (
        "import hashlib,json,platform;from pathlib import Path;"
        "print(json.dumps({'python':platform.python_version(),'libc':platform.libc_ver(),"
        "'source_sha256':{str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest()"
        " for p in sorted(Path('/app/src').rglob('*.py'))}}))"
    )
    inventory = loads(
        checked(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python3",
                image_id,
                "-c",
                inventory_code,
            ]
        )
    )
    local_sources = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "src").rglob("*.py"))
    }
    if inventory["source_sha256"] != local_sources:
        raise ContractError("image source differs from the runtime being accepted")
    before = dict(split.source_hashes)
    for biz in {q.business_id for q in questions}:
        before.update(
            {
                str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (data / "data" / biz).rglob("*")
                if p.is_file()
            }
        )
    result = {
        "image": image,
        "image_id": image_id,
        "gpu_layers": gpu_layers,
        "image_runtime": inventory,
        "runs": [],
    }
    for repeat in range(2):
        name = f"datamate-llm-{uuid4().hex[:12]}"
        output = work / f"run-{repeat}"
        monitor = None
        monitor_path = work / f"gpu-{repeat}.csv"
        monitor_stream = None
        baseline = None
        if gpu_layers:
            baseline = checked(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
            )
            monitor_stream = monitor_path.open("w")
            monitor = subprocess.Popen(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,utilization.gpu",
                    "--format=csv,noheader,nounits",
                    "-lms",
                    "500",
                ],
                stdout=monitor_stream,
                stderr=subprocess.DEVNULL,
            )
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=64m",
        ]
        # Mount only inference sources; answers/EDA/reports are physically absent.
        for relative in ["questions.jsonl", "rulebook.md", "calendar.csv", "data"]:
            command.extend(
                ["--mount", f"type=bind,source={data / relative},target=/data/{relative},readonly"]
            )
        command.extend(["--mount", f"type=bind,source={work},target=/result"])
        if gpu_layers:
            command.extend(["--gpus", "all", "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"])
        command.extend(
            [
                image_id,
                "--data-dir",
                "/data",
                "--output-dir",
                f"/result/{output.name}",
                "--cache-dir",
                "/result/cache",
                "--model",
                "/opt/model/model.gguf",
                "--model-threads",
                "4",
                "--question-timeout",
                "120",
                "--gpu-layers",
                str(gpu_layers),
            ]
        )
        for q in questions:
            command.extend(["--question-id", q.id])
        started = time.monotonic()
        try:
            checked(command)
            exit_code = checked(["docker", "wait", name], timeout=3600)
            configuration = loads(checked(["docker", "inspect", name]))[0]
            native_logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, timeout=30, check=False
            )
            native_output = native_logs.stdout + native_logs.stderr
            if exit_code != "0":
                raise ContractError(f"model container exited {exit_code}: {native_output[-2500:]}")
            host = configuration["HostConfig"]
            mounts = {m["Destination"]: m["RW"] for m in configuration["Mounts"]}
            if (
                host["NetworkMode"] != "none"
                or not host["ReadonlyRootfs"]
                or any(
                    mounts.get("/data/" + r) is not False
                    for r in ["questions.jsonl", "rulebook.md", "calendar.csv", "data"]
                )
                or mounts.get("/result") is not True
            ):
                raise ContractError("LLM container isolation differs from contract")
            manifest = loads((output / "run_manifest.json").read_text())
            records = [r for _, r in read_jsonl(output / "submission.jsonl")]
            logs = [loads(p.read_text()) for p in sorted((output / "logs").glob("*.json"))]
            if (
                manifest["is_mock"]
                or manifest["question_errors"]
                or [r["id"] for r in records] != [q.id for q in questions]
                or len(logs) != len(records)
            ):
                raise ContractError("LLM output identity/runtime gate failed")
            observations, seconds, peak_rss = [], [], 0
            for log in logs:
                plan, execution = log["plan"], log["execution"]
                attempts = plan.get("planning", {}).get("attempts", plan.get("attempts", []))
                for attempt in attempts:
                    if "seconds" in attempt:
                        seconds.append(attempt["seconds"])
                    peak_rss = max(peak_rss, attempt.get("peak_rss_mib", 0))
                spec = expected[log["id"]]
                if spec:
                    window = plan.get("time_windows", [{}])[0]
                    valid = (
                        plan.get("aggregations", [{}])[0].get("metric_id") == spec[0]
                        and window.get("requested_start") == spec[1]
                        and window.get("requested_end") == spec[2]
                        and execution.get("status") == "verified"
                    )
                else:
                    valid = log["answer"]["type"] == "abstain"
                observations.append(
                    {
                        "id": log["id"],
                        "answer": log["answer"],
                        "intent_and_execution_valid": valid,
                        "execution": execution,
                        "generation_seconds": sum(a.get("seconds", 0) for a in attempts),
                        "attempt_errors": [a["error"] for a in attempts if a.get("error")],
                    }
                )
            seconds.sort()
            result["runs"].append(
                {
                    "repeat": repeat,
                    "wall_seconds": time.monotonic() - started,
                    "model": manifest["planner_metadata"]["model"],
                    "predictions": records,
                    "preparation": manifest["preparation"],
                    "observations": observations,
                    "generation_seconds": seconds,
                    "peak_worker_rss_mib": peak_rss,
                }
            )
            print(
                dumps(
                    {
                        "repeat": repeat,
                        "container_completed": True,
                        "questions": len(records),
                        "gpu_layers": gpu_layers,
                    }
                ),
                flush=True,
            )
        finally:
            if monitor:
                monitor.terminate()
                monitor.wait(timeout=10)
                monitor_stream.close()
                rows = [line.split(",") for line in monitor_path.read_text().splitlines()]
                samples = [
                    (int(a.strip()), int(b.strip()))
                    for a, b in rows
                    if a.strip().isdigit() and b.strip().isdigit()
                ]
                if samples and result["runs"]:
                    result["runs"][-1]["gpu_device_memory"] = {
                        "baseline_mib": baseline,
                        "peak_used_mib": max(a for a, _ in samples),
                        "peak_utilization_percent": max(b for _, b in samples),
                        "samples": len(samples),
                        "scope": "whole device, includes desktop/other processes",
                    }
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False
            )
    # All inference containers exited before the only label-reading stage.
    gold = work / "gold.jsonl"
    gold.write_text(
        "".join(
            dumps(row) + "\n"
            for _, row in read_jsonl(data / "answers.jsonl")
            if row["id"] in expected
        )
    )
    for run in result["runs"]:
        run["evaluation"] = evaluate(gold, work / f"run-{run['repeat']}" / "submission.jsonl")
    result["predictions_identical"] = (
        result["runs"][0]["predictions"] == result["runs"][1]["predictions"]
    )
    result["inputs_unchanged"] = all(
        hashlib.sha256((data / p).read_bytes()).hexdigest() == v for p, v in before.items()
    )
    result["input_sha256"] = before
    durations = sorted(t for run in result["runs"] for t in run["generation_seconds"])
    result["latency"] = {
        "calls": len(durations),
        "median_seconds": statistics.median(durations) if durations else None,
        "p95_seconds": durations[math.ceil(0.95 * len(durations)) - 1] if durations else None,
    }
    result["verified"] = (
        result["inputs_unchanged"]
        and result["predictions_identical"]
        and all(
            run["evaluation"]["wrong"] == 0
            and run["evaluation"]["correct"] == 12
            and run["evaluation"]["abstain"] == 1
            and not run["evaluation"]["malformed"]
            and all(o["intent_and_execution_valid"] for o in run["observations"])
            for run in result["runs"]
        )
    )
    return result


def verify_llm(args) -> int:
    require_project_environment()
    manifest = loads(args.context_manifest.read_text())
    lock_sha = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    if manifest["uv_lock_sha256"] != lock_sha:
        raise ContractError("image build manifest uses a different uv.lock")
    frozen = loads((ROOT / "reports/planner_verification.json").read_text())
    selection = frozen["selection"]
    protocol_sha = hashlib.sha256((ROOT / "config/validation_split.json").read_bytes()).hexdigest()
    if protocol_sha != selection["protocol_sha256"]:
        raise ContractError("validation split differs from frozen S1 evidence")
    path = args.report or ROOT / "reports/docker_llm_verification.json"
    sources = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "src").rglob("*.py"))
    }
    if args.gpu_layers:
        if not path.is_file():
            raise ContractError("GPU comparison requires a verified CPU report first")
        report = loads(path.read_text())
        if not report.get("cpu", {}).get("verified") or report["runtime_source_sha256"] != sources:
            raise ContractError(
                "GPU comparison requires verified CPU image on the same runtime source"
            )
    else:
        report = {
            "scope": "Two offline Docker runs/backend on frozen 13-question common-dev S1 subset",
            "runtime_source_sha256": sources,
            "selection": selection,
            "model_sha256": frozen["model_artifact"]["sha256"],
        }
    report["recorded_at"] = datetime.now(UTC).isoformat()
    key = "gpu" if args.gpu_layers else "cpu"
    with tempfile.TemporaryDirectory(prefix=f"docker-llm-{key}-", dir=ROOT / ".artifacts") as tmp:
        try:
            check = check_llm_image(args.image, args.gpu_layers, Path(tmp), selection)
            check["build_context"] = {
                "manifest_sha256": hashlib.sha256(args.context_manifest.read_bytes()).hexdigest(),
                "context_sha256": manifest["context_sha256"],
                "runtime_packages": manifest["runtime_packages"],
                "model": next(
                    (f for f in manifest["files"] if f["path"] == "llm/model.gguf"),
                    {"source": "verified_cpu_image", "sha256": report["model_sha256"]},
                ),
                "uv_lock_sha256": manifest["uv_lock_sha256"],
            }
            for run in check["runs"]:
                if run["model"]["sha256"] != report["model_sha256"]:
                    raise ContractError("container loaded different model weights")
                if args.gpu_layers and not run["model"].get("layers_offloaded"):
                    raise ContractError("GPU layer offload not proven")
            report[key] = check
            if key == "gpu":
                if check["input_sha256"] != report["cpu"]["input_sha256"]:
                    raise ContractError("CPU/GPU comparison requires identical source inputs")
                report["cpu_gpu_predictions_equal"] = (
                    check["runs"][0]["predictions"] == report["cpu"]["runs"][0]["predictions"]
                )
        except (ContractError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            report[key] = {"verified": False, "error": str(exc)}
    write_json(path, report)
    print(dumps({"backend": key, "verified": report[key]["verified"], "report": str(path)}))
    return int(not report[key]["verified"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["cpu", "all", "llm"], default="all")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--image", help="prebuilt offline LLM image for --scope llm")
    parser.add_argument("--context-manifest", type=Path)
    parser.add_argument("--gpu-layers", type=int, default=0)
    args = parser.parse_args()
    if args.scope == "llm":
        if not args.image or not args.context_manifest:
            parser.error("--scope llm requires --image and --context-manifest")
        try:
            return verify_llm(args)
        except (ContractError, OSError) as exc:
            print(dumps({"error": str(exc)}), file=sys.stderr)
            return 2
    args.report = args.report or ROOT / "reports/runtime_verification.json"
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope": args.scope,
        "runtime_verified": False,
        "checks": {
            "quality": "not_run",
            "cpu": "not_run",
            "docker": "not_run",
            "gpu_runtime": "not_run",
        },
    }
    try:
        report["project_environment"] = require_project_environment()
    except ContractError as exc:
        print(dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    os.environ.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    artifacts = ROOT / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-verification-", dir=artifacts) as temporary:
        work = Path(temporary)

        def run_gate(name, operation):
            try:
                report[name] = operation()
                report["checks"][name] = "passed"
            except (ContractError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                report["checks"][name] = "failed"
                report[name] = {"error": str(exc)}

        run_gate("quality", lambda: {"toolchain": check_toolchain(), **check_quality(work)})

        def cpu_check():
            result = check_real_splits([ROOT / "dataset/train", ROOT / "dataset/public_test"], work)
            context = build_context(ROOT, work / "context.tar")
            if context != build_context(ROOT, work / "context-repeat.tar"):
                raise ContractError("Docker context is nondeterministic")
            result["docker_context_sha256"] = context["context_sha256"]
            return result

        run_gate("cpu", cpu_check)
        if args.scope == "all":
            run_gate("docker", lambda: check_docker(work))

            run_gate("gpu_runtime", check_gpu)
    report["runtime_verified"] = all(state == "passed" for state in report["checks"].values())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.report, report)
    print(dumps({key: report[key] for key in ["scope", "runtime_verified", "checks"]}))
    required = ["quality", "cpu"] if args.scope == "cpu" else list(report["checks"])
    return 0 if all(report["checks"][key] == "passed" for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
