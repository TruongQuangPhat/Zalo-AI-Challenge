"""Record hardware/daemon evidence; an unavailable check is never a pass."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.environment import project_python
from src.jsonio import ContractError, dumps, write_json


def command(argv: list[str], timeout: int = 10) -> dict:
    if shutil.which(argv[0]) is None:
        return {"status": "unavailable", "command": argv, "reason": "executable_not_found"}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "command": argv, "reason": "timeout"}
    except OSError as exc:
        return {
            "status": "unavailable",
            "command": argv,
            "reason": "process_start_failed",
            "error": str(exc),
        }
    return {
        "status": "passed" if result.returncode == 0 else "unavailable",
        "command": argv,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def probe(python_executable: str | None = None) -> dict:
    target_python = python_executable or os.environ.get("DATAMATE_GPU_PYTHON") or project_python()
    versions = {}
    for package in ["pytest", "ruff", "packaging", "pluggy", "iniconfig", "Pygments", "torch"]:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    torch_code = (
        "import json,sys,torch; ok=torch.cuda.is_available(); "
        "x=torch.ones(1,device='cuda') if ok else None; "
        "torch.cuda.synchronize() if ok else None; "
        "print(json.dumps({'python':sys.version.split()[0],'executable':sys.executable,"
        "'torch':torch.__version__,'torch_module':torch.__file__,'cuda_available':ok,"
        "'cuda_version':torch.version.cuda,'tensor_device':str(x.device) if ok else None,"
        "'device_name':torch.cuda.get_device_name(0) if ok else None,"
        "'tensor_value':x.cpu().tolist() if ok else None})); "
        "raise SystemExit(0 if ok else 1)"
    )
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "controller_executable": sys.executable,
        "torch_probe_python": target_python,
        "platform": platform.platform(),
        "versions": versions,
        "versions_scope": "controller_interpreter_only_not_all_installed_environments",
        "devices_visible_in_this_session": {
            "wsl_dxg": Path("/dev/dxg").exists(),
            "linux_nvidia0": Path("/dev/nvidia0").exists(),
        },
        "docker": command(["docker", "info", "--format", "{{.ServerVersion}}"]),
        "gpu": command(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]
        ),
        "torch_cuda": command([target_python, "-c", torch_code], timeout=30),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--python",
        dest="python_executable",
        help="Explicit diagnostic override; default is the project .venv (or DATAMATE_GPU_PYTHON)",
    )
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    try:
        report = probe(args.python_executable)
    except ContractError as exc:
        print(dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, report)
    print(dumps({key: report[key]["status"] for key in ["docker", "gpu", "torch_cuda"]}))
    ready = all(report[key]["status"] == "passed" for key in ["docker", "gpu", "torch_cuda"])
    return int(args.require_ready and not ready)


if __name__ == "__main__":
    raise SystemExit(main())
