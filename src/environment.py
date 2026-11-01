"""The project owns its interpreter; never silently borrow another environment."""

from __future__ import annotations

import site
import sys
from pathlib import Path

from src.jsonio import ContractError

ROOT = Path(__file__).resolve().parents[1]


def project_python() -> str:
    executable = ROOT / ".venv" / "bin" / "python"
    if not executable.is_file():
        raise ContractError("Project .venv is missing. Run uv sync --locked first.")
    return str(executable)


def require_project_environment() -> dict:
    expected = ROOT / ".venv"
    if Path(sys.prefix).resolve() != expected.resolve() or sys.prefix == sys.base_prefix:
        raise ContractError(
            "Use the project's .venv: uv run --no-sync python -m scripts.check_phase0"
        )
    if site.ENABLE_USER_SITE:
        raise ContractError("Project environment must not enable user site-packages")
    config = expected / "pyvenv.cfg"
    if not config.is_file():
        raise ContractError("Project .venv must contain pyvenv.cfg")
    settings = dict(
        line.lower().strip().split("=", 1)
        for line in config.read_text().splitlines()
        if "=" in line
    )
    settings = {key.strip(): value.strip() for key, value in settings.items()}
    if settings.get("include-system-site-packages") != "false":
        raise ContractError("Project .venv must disable system site-packages")
    foreign = [
        value
        for value in sys.path
        if {"site-packages", "dist-packages"}.intersection(Path(value).parts)
        and not Path(value).resolve().is_relative_to(expected.resolve())
    ]
    if foreign:
        raise ContractError("Foreign site-packages leaked into the project environment")
    return {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "user_site_enabled": site.ENABLE_USER_SITE,
        "system_site_enabled": False,
        "foreign_site_packages": foreign,
    }
