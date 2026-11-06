# Repository Guidelines

## Scope & Documentation

Keep contributor practices here, setup in `README.md`, and plans, progress, and evidence in `roadmap.md`. Planned capabilities are not implemented features.

## Project Structure

- `src/`: application modules, including `data/`, `agent/`, and `evaluation/`; use this layout directly without another package layer.
- `scripts/`: reusable development and verification tools.
- `tests/`: pytest modules with shared synthetic fixtures in `conftest.py`.
- `dataset/`: ignored local inputs; `.artifacts/`: ignored working outputs; `reports/`: concise verification summaries.

## Development Commands

Use the isolated Python 3.11 `.venv`, managed through `pyproject.toml` and `uv.lock`. Run from the repository root:

```bash
uv sync --locked --python /usr/bin/python3.11 --group llm
uv run --no-sync python -m scripts.verify_runtime --scope cpu
uv run --no-sync python -m scripts.verify_runtime --scope all \
  --image zaic2026_datamate_team:review1 --data-dir dataset/train
./predict.sh --data-dir dataset/train --output-dir .artifacts/my-run
```

CPU verification runs lint, formatting, tests, and deterministic source-context checks; it does not run a model or certify Docker/CUDA. Full verification uses an already built image through the shared submission smoke runner and checks CUDA. Build/export/reload belong to `scripts.submission`; all inference uses root `predict.sh`, copied to `/code/predict.sh` in Docker. Model weights are required; there is no production mock fallback. Use fresh output directories.

## Coding Style & Simplicity

Use four-space indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Ruff enforces import ordering and a 100-character line limit. Format with `uv run --no-sync python -m ruff format src scripts tests`.

Keep changes scoped to the requested work. Prefer existing entrypoints and direct implementations. Add abstractions, shell wrappers, configuration, ADRs, or separate logs only for a demonstrated need. Update dependency declarations and regenerate the uv lockfile together.

## Testing & Verification

Name pytest files `test_*.py` and functions `test_*`. Add focused behavioral tests for meaningful changes; avoid implementation-mirroring or redundant tests. Prioritize scoring, precision, input immutability, and label isolation. No numeric coverage threshold is configured.

Run relevant checks once; repeat when changes or failures justify it. Documentation-only edits need content/link/whitespace checks, not inference runs. Clean temporary verification outputs and avoid accumulating reports. Record completion only when relevant gates pass; CPU checks cannot certify Docker/CUDA. State environmental limitations explicitly.

## Data & Runtime Boundaries

Never commit datasets, environments, credentials, weights, or generated runs. Preserve raw inputs and write outside `dataset/`. Keep inference offline and isolated from labels and EDA answer reports. Follow the current split's rulebook, README, and calendar. Never hard-code answers or use public/private test data for training, pseudo-labeling, or tuning.

## Commits & Pull Requests

Commit or push only when requested. Use focused, imperative messages: `feat: add schema validation` or `fix: preserve Decimal precision`. PR descriptions should explain the problem, behavior, validation, and limitations; link relevant issues.
