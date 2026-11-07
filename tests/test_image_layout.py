"""Inference must run from only the files the Docker build context allowlists."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from src.jsonio import loads

ROOT = Path(__file__).resolve().parents[1]

CODE = """
import sys
from pathlib import Path
from src.runtime import run_inference

class Planner:
    name = "layout-planner"
    metadata = {"model": {"files": {}}}

    def decide(self, question, *, context=None):
        return {"answer": {"id": question.id, "type": "abstain"}, "plan": {}}

run_inference(Path(sys.argv[1]), Path(sys.argv[2]), planner=Planner())
"""


def image_copy(target: Path) -> Path:
    allowed = [
        line[1:].rstrip("/")
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.startswith("!")
    ]
    target.mkdir()
    for name in allowed:
        source = ROOT / name
        if name == "models" or not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target / name)
    return target


def test_allowlisted_layout_runs_inference_and_rejects_missing_weights(synthetic_split, tmp_path):
    code = image_copy(tmp_path / "code")
    assert not (code / "tests").exists() and not (code / "pyproject.toml").exists()
    output = tmp_path / "result"
    subprocess.run(
        [sys.executable, "-c", CODE, str(synthetic_split), str(output)],
        cwd=code,
        env={"PYTHONPATH": str(code), "PATH": "/usr/bin:/bin"},
        check=True,
    )
    assert loads((output / "run_manifest.json").read_text())["status"] == "completed"
    assert (output / "submission.jsonl").read_text().count("\n") == len(
        list((output / "logs").glob("*.json"))
    )
    missing = subprocess.run(
        ["bash", str(code / "predict.sh"), "--data-dir", str(synthetic_split)],
        cwd=tmp_path,
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert "local model directory" in missing.stderr
