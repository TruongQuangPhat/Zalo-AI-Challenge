# Repository Guidelines

## Scope & Documentation

Keep contributor practices here, setup in `README.md`, and plans, progress, and evidence in `roadmap.md`. Planned capabilities are not implemented features.

## Project Structure

- `src/`: application modules, including `data/`, `agent/`, and `evaluation/`; use this layout directly without another package layer.
- `scripts/`: reusable development and verification tools.
- `tests/`: pytest modules with shared synthetic fixtures in `conftest.py`.
- `models/`: ignored packaged inference weights; `training_code/`: no-training README and data manifest copied into the image.
- `dataset/`: ignored local inputs; `.artifacts/`: ignored working outputs.

## Development Commands

Use the isolated Python 3.11 `.venv` with the pinned tools in `requirements-dev.txt`; the image runtime is pinned in `requirements.txt`. `pyproject.toml` holds only Ruff/pytest configuration. Run from the repository root:

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m scripts.verify_runtime
docker build --platform linux/amd64 -t zaic2026_datamate_datamate:final .
docker run --rm --gpus '"device=0,1"' --network none --shm-size=16g --entrypoint bash \
  -v "$PWD/dataset/public_test:/data:ro" -v "$PWD/.artifacts/my-run:/result" \
  zaic2026_datamate_datamate:final /code/predict.sh
.venv/bin/python -m scripts.check_submission \
  --data-dir dataset/public_test --output-dir .artifacts/my-run
```

Packaging follows the organizers' Docker guide; README.md holds the build/run/export commands. `verify_runtime` checks the pinned tool versions and runs Ruff lint/format and pytest; it does not run a model or certify Docker/CUDA. The `.venv` has no vLLM: inference runs in the `vllm/vllm-openai` image through root `predict.sh`, copied to `/code/predict.sh`. Model weights are required; there is no production mock fallback. Use fresh output directories.

## Coding Style & Simplicity

Use four-space indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Ruff enforces import ordering and a 100-character line limit. Format with `.venv/bin/python -m ruff format src scripts tests`.

Keep changes scoped to the requested work. Prefer existing entrypoints and direct implementations. Add abstractions, shell wrappers, configuration, ADRs, or separate logs only for a demonstrated need. Pin exact versions (`==`) in the requirements files; keep runtime and dev dependencies separate.

## Testing & Verification

Name pytest files `test_*.py` and functions `test_*`. Add focused behavioral tests for meaningful changes; avoid implementation-mirroring or redundant tests. Prioritize scoring, precision, input immutability, and label isolation. No numeric coverage threshold is configured.

Run relevant checks once; repeat when changes or failures justify it. Documentation-only edits need content/link/whitespace checks, not inference runs. Clean temporary verification outputs; record results in `roadmap.md`, not separate report files. Record completion only when relevant gates pass; CPU checks cannot certify Docker/CUDA. State environmental limitations explicitly.

## Data & Runtime Boundaries

Never commit datasets, environments, credentials, weights, or generated runs. Preserve raw inputs and write outside `dataset/`. Keep inference offline and isolated from labels and EDA answer reports. Follow the current split's rulebook, README, and calendar. Never hard-code answers or use public/private test data for training, pseudo-labeling, or tuning.

## Commits & Pull Requests

Commit or push only when requested. Use focused, imperative messages: `feat: add schema validation` or `fix: preserve Decimal precision`. PR descriptions should explain the problem, behavior, validation, and limitations; link relevant issues.
