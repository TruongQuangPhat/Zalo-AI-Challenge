"""CPU quality gate: toolchain lock, Ruff lint/format and pytest. No model, Docker or GPU."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

from src.environment import require_project_environment
from src.jsonio import ContractError, dumps

ROOT = Path(__file__).resolve().parents[1]


def check_toolchain() -> dict:
    require_project_environment()
    versions = {}
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    for line in config["dependency-groups"]["dev"]:
        requirement = Requirement(line)
        expected = locked[requirement.name]
        if str(requirement.specifier) != "==" + expected:
            raise ContractError(f"development dependency and uv.lock disagree: {line}")
        actual = importlib.metadata.version(requirement.name)
        if actual != expected:
            raise ContractError(f"toolchain mismatch: {requirement.name} {actual} != {expected}")
        versions[requirement.name] = actual
    return versions


def checked(argv: list[str], timeout: int = 120) -> str:
    result = subprocess.run(
        argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise ContractError(f"{argv[2]} failed: {(result.stderr or result.stdout)[-4000:]}")
    return result.stdout.strip()


def main() -> int:
    os.environ.update(
        PYTHONPATH=str(ROOT),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    report = {}
    try:
        report["toolchain"] = check_toolchain()
        paths = ["src", "scripts", "tests"]
        checked([sys.executable, "-m", "ruff", "check", "--no-cache", *paths])
        checked([sys.executable, "-m", "ruff", "format", "--check", "--no-cache", *paths])
        with tempfile.TemporaryDirectory(prefix="datamate-pytest-") as work:
            output = checked(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    work,
                ],
                timeout=900,
            )
        report.update(lint="passed", format="passed", tests=output.splitlines()[-1])
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    print(dumps(report))
    return int("error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
