"""Docker guide §10 run checks must reject incomplete coverage and missing log fields."""

from __future__ import annotations

import pytest

from scripts.check_submission import check_run
from src.jsonio import ContractError, dumps, loads
from src.runtime import run_inference
from tests.helpers import StubPlanner


def test_complete_run_passes_and_reports_time_estimate(synthetic_split, tmp_path):
    output = tmp_path / "run"
    run_inference(synthetic_split, output, planner=StubPlanner())
    report = check_run(synthetic_split, output)
    assert report["status"] == "completed"
    assert report["types"] == {"abstain": report["questions"]}
    assert report["fits_6_hours"] is True


def test_missing_answer_or_thinking_field_is_rejected(synthetic_split, tmp_path):
    output = tmp_path / "run"
    run_inference(synthetic_split, output, planner=StubPlanner())
    log = sorted((output / "logs").glob("*.json"))[0]
    record = loads(log.read_text())
    del record["thinking"]
    log.write_text(dumps(record))
    with pytest.raises(ContractError, match="thinking"):
        check_run(synthetic_split, output)
    submission = output / "submission.jsonl"
    submission.write_text("".join(submission.read_text().splitlines(keepends=True)[1:]))
    with pytest.raises(ContractError, match="every question"):
        check_run(synthetic_split, output)
