"""Docker guide §10 checks for a finished run: coverage, logs, time estimate, repeatability.

Reads only questions.jsonl from the input split, never answers.
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from src.contracts import validate_answer
from src.data.split import load_split
from src.jsonio import ContractError, dumps, loads

PRIVATE_QUESTIONS = 500
TIME_LIMIT_SECONDS = 6 * 3600


def check_run(data_dir: Path, output: Path) -> dict:
    ids = [q.id for q in load_split(data_dir).questions]
    rows = [loads(line) for line in (output / "submission.jsonl").read_text().splitlines()]
    if [row.get("id") for row in rows] != ids:
        raise ContractError("submission.jsonl must cover every question once, in input order")
    for row in rows:
        validate_answer(row)
    records = {}
    for path in sorted((output / "logs").glob("*.json")):
        record = loads(path.read_text())
        records[record.get("id")] = record
    if set(records) != set(ids):
        raise ContractError("one log record per question is required")
    for row in rows:
        record = records[row["id"]]
        elapsed = record.get("elapsed_ms")
        if record.get("answer") != row or type(elapsed) is not int or elapsed < 0:
            raise ContractError(f"{row['id']}: log needs the final answer and elapsed_ms >= 0")
        if "thinking" not in record:
            raise ContractError(f"{row['id']}: log needs thinking (null if none)")
        if record.get("code") and "output" not in record:
            raise ContractError(f"{row['id']}: executed code needs its output")
    manifest = loads((output / "run_manifest.json").read_text())
    model = (manifest.get("planner_metadata") or {}).get("model") or {}
    load_seconds = float(model.get("startup_seconds_including_hash", 0))
    elapsed = [records[i]["elapsed_ms"] / 1000 for i in ids]
    estimate = load_seconds + statistics.fmean(elapsed) * PRIVATE_QUESTIONS
    return {
        "questions": len(ids),
        "status": manifest.get("status"),
        "question_errors": manifest.get("question_errors"),
        "types": {t: sum(row["type"] == t for row in rows) for t in {r["type"] for r in rows}},
        "model_load_seconds": load_seconds,
        "mean_question_seconds": statistics.fmean(elapsed),
        "estimated_private_seconds": estimate,
        "fits_6_hours": estimate <= TIME_LIMIT_SECONDS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--compare", type=Path, help="second run on the same input")
    args = parser.parse_args()
    try:
        report = check_run(args.data_dir, args.output_dir)
        if args.compare is not None:
            check_run(args.data_dir, args.compare)
            first = (args.output_dir / "submission.jsonl").read_text().splitlines()
            second = (args.compare / "submission.jsonl").read_text().splitlines()
            report["repeat_differences"] = sum(a != b for a, b in zip(first, second, strict=True))
        if report["status"] != "completed" or report["question_errors"]:
            raise ContractError("run did not complete without question errors")
        if report.get("repeat_differences"):
            raise ContractError(f"repeat runs differ on {report['repeat_differences']} answers")
    except (ContractError, OSError, ValueError) as exc:
        print(dumps({"error": str(exc)}))
        return 1
    print(dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
