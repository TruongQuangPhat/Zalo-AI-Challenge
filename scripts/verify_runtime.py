"""CPU quality gates and optional real-image checks through the single submission runner."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import Requirement

from scripts.build_context import build_context
from scripts.probe_environment import probe
from src.environment import require_project_environment
from src.jsonio import ContractError, dumps, loads, write_json

ROOT = Path(__file__).resolve().parents[1]


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
        ],
        timeout=600,
    )
    return {"lint": "passed", "format": "passed", "tests": result.splitlines()[-1]}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["cpu", "all"], default="cpu")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/runtime_verification.json")
    parser.add_argument("--image", help="already built submission image; never built/pulled here")
    parser.add_argument("--data-dir", type=Path, help="split for the real offline smoke")
    parser.add_argument("--gpus", default="all")
    parser.add_argument("--timeout", type=float, default=21600)
    args = parser.parse_args()
    if args.scope == "all" and (not args.image or not args.data_dir):
        parser.error("--scope all requires --image and --data-dir; build with scripts.submission")
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope": args.scope,
        "runtime_verified": False,
        "model_inference": "NOT_TESTED",
        "checks": {"quality": "not_run", "source_context": "not_run"},
    }
    try:
        report["project_environment"] = require_project_environment()
        os.environ.update(
            PYTHONPATH=str(ROOT),
            PYTHONNOUSERSITE="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        )
        artifacts = ROOT / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="runtime-verification-", dir=artifacts
        ) as temporary:
            work = Path(temporary)
            report["checks"]["quality"] = "running"
            report["quality"] = {"toolchain": check_toolchain(), **check_quality(work)}
            report["checks"]["quality"] = "passed"
            report["checks"]["source_context"] = "running"
            context = build_context(ROOT, work / "context.tar")
            if context != build_context(ROOT, work / "context-repeat.tar"):
                raise ContractError("Docker context is nondeterministic")
            report["source_context"] = {
                "sha256": context["context_sha256"],
                "files": len(context["files"]),
                "dataset_included": False,
            }
            report["checks"]["source_context"] = "passed"
            if args.scope == "all":
                report["checks"].update(docker_llm="not_run", gpu_runtime="not_run")
                command = [
                    sys.executable,
                    "-m",
                    "scripts.submission",
                    "smoke",
                    "--image",
                    args.image,
                    "--input",
                    str(args.data_dir.resolve()),
                    "--output",
                    str(work / "offline-smoke"),
                    "--mode",
                    "smoke",
                    "--limit",
                    "3",
                    "--gpus",
                    args.gpus,
                    "--timeout",
                    str(args.timeout),
                    "--execute",
                ]
                report["checks"]["docker_llm"] = "running"
                checked(command, timeout=int(args.timeout * 2 + 600))
                report["checks"]["docker_llm"] = "passed"
                report["model_inference"] = "VERIFIED_LOCAL_IMAGE"
                if args.gpus != "none":
                    report["checks"]["gpu_runtime"] = "running"
                    report["gpu_runtime"] = check_gpu()
                    report["checks"]["gpu_runtime"] = "passed"
                    report["runtime_verified"] = True
                else:
                    report["checks"]["gpu_runtime"] = "NOT_TESTED"
    except (ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        for name, state in report["checks"].items():
            if state == "running":
                report["checks"][name] = "failed"
        report["error"] = str(exc)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.report, report)
    print(dumps({key: report[key] for key in ["scope", "runtime_verified", "checks"]}))
    return int("error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
