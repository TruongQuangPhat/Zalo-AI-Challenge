"""Separate inference, split validation and label-reading evaluation commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.data.split import load_split
from src.jsonio import ContractError, dumps, write_json
from src.runtime import run_inference


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0].startswith("--data-dir"):
        args_list.insert(0, "predict")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    predict = commands.add_parser("predict", help="label-free mock abstention; no LLM")
    predict.add_argument("--data-dir", type=Path, required=True)
    predict.add_argument("--output-dir", type=Path, required=True)
    validate = commands.add_parser("validate", help="validate split structure without labels")
    validate.add_argument("--data-dir", type=Path, required=True)
    score = commands.add_parser("evaluate", help="explicit, separate ground-truth evaluation")
    score.add_argument("--answers", type=Path, required=True)
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--report", type=Path)
    args = parser.parse_args(args_list)
    try:
        if args.command == "predict":
            report = run_inference(args.data_dir, args.output_dir)
        elif args.command == "validate":
            report = load_split(args.data_dir).summary()
        else:
            from src.evaluation.scorer import evaluate

            report = evaluate(args.answers, args.predictions)
            if args.report:
                if args.report.resolve() in {args.answers.resolve(), args.predictions.resolve()}:
                    raise ContractError("report cannot overwrite labels or predictions")
                if args.report.resolve().is_relative_to(args.answers.parent.resolve()):
                    raise ContractError("report must be outside the ground-truth directory")
                args.report.parent.mkdir(parents=True, exist_ok=True)
                write_json(args.report, report)
        print(dumps(report))
        return int(report.get("question_errors", 0) > 0)
    except (ContractError, OSError, UnicodeError) as exc:
        print(dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
