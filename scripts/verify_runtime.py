"""Offline runtime verifier; temporary predictions/logs are removed after every run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
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
            ]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["cpu", "all"], default="all")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/runtime_verification.json")
    args = parser.parse_args()
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

            def gpu_check():
                result = probe()
                if any(
                    result[key]["status"] != "passed" for key in ["docker", "gpu", "torch_cuda"]
                ):
                    raise ContractError(
                        dumps({key: result[key] for key in ["docker", "gpu", "torch_cuda"]})
                    )
                return {
                    "device": result["gpu"]["stdout"],
                    "torch_cuda": loads(result["torch_cuda"]["stdout"]),
                }

            run_gate("gpu_runtime", gpu_check)
    report["runtime_verified"] = all(state == "passed" for state in report["checks"].values())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.report, report)
    print(dumps({key: report[key] for key in ["scope", "runtime_verified", "checks"]}))
    required = ["quality", "cpu"] if args.scope == "cpu" else list(report["checks"])
    return 0 if all(report["checks"][key] == "passed" for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
