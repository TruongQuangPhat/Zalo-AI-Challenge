#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${project_root}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
if [ -x "${project_root}/.venv/bin/python" ]; then
    project_python="${project_root}/.venv/bin/python"
elif [ "${DATAMATE_CONTAINER:-0}" = 1 ]; then
    project_python="/usr/local/bin/python3"
else
    echo "Project .venv is missing. Run uv sync --locked --python /usr/bin/python3.11 first." >&2
    exit 2
fi
if [ "$#" -eq 0 ]; then
    set -- --data-dir /data --output-dir /result
fi
exec "${project_python}" -m src.cli predict "$@"
