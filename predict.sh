#!/usr/bin/env bash
# Entry point: /data -> /result/submission.jsonl and /result/logs/. Defaults live in src/cli.py.
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${project_root}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
if [ -x "${project_root}/.venv/bin/python" ]; then
    project_python="${project_root}/.venv/bin/python"
else
    project_python="python3"
fi
exec "${project_python}" -m src.cli predict "$@"
