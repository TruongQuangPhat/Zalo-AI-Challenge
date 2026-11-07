"""Separate inference, split validation and label-reading evaluation commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.data.catalog import prepare_business, require_output
from src.data.ingestion import ReaderOptions
from src.data.normalization import prepare_facts
from src.data.profiling import ProfileOptions
from src.data.split import load_split
from src.jsonio import ContractError, dumps, write_json
from src.runtime import run_inference
from src.semantics.answerability import prepare_semantics


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    predict = commands.add_parser("predict", help="offline local LLM planner and execution")
    predict.add_argument("--data-dir", type=Path, default=Path("/data"))
    predict.add_argument("--output-dir", type=Path, default=Path("/result"))
    predict.add_argument("--cache-dir", type=Path)
    predict.add_argument(
        "--resume-from", type=Path, help="verify immutable prior run; output must be fresh"
    )
    predict.add_argument("--resume-tail", choices=["reject", "discard"], default="reject")
    predict.add_argument(
        "--model",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "models/qwen2.5-1.5b-instruct",
        help="packaged local model directory; missing weights fail before writing output",
    )
    predict.add_argument("--question-timeout", type=float, default=120)
    predict.add_argument("--model-load-timeout", type=float, default=900)
    predict.add_argument(
        "--question-id", action="append", help="explicit subset, input order retained"
    )
    for command, help_text in [
        ("catalog", "inspect source tables without labels or metrics"),
        ("prepare", "normalize supported sources into exact, traceable local facts"),
    ]:
        catalog = commands.add_parser(command, help=help_text)
        catalog.add_argument("--data-dir", type=Path, required=True)
        catalog.add_argument("--output-dir", type=Path, required=True)
        catalog.add_argument("--business", action="append", help="repeat to select businesses")
        catalog.add_argument("--cache-dir", type=Path)
        catalog.add_argument("--encoding", default="utf-8-sig")
        catalog.add_argument("--delimiter", choices=[",", ";", "\t", "|"])
        catalog.add_argument("--max-distinct", type=int, default=100000)
        catalog.add_argument("--counter-memory-mib", type=int, default=32)
    validate = commands.add_parser("validate", help="validate split structure without labels")
    validate.add_argument("--data-dir", type=Path, required=True)
    score = commands.add_parser("evaluate", help="explicit, separate ground-truth evaluation")
    score.add_argument("--answers", type=Path, required=True)
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--report", type=Path)
    args = parser.parse_args(args_list)
    try:
        if args.command == "predict":
            from src.agent.local import LocalModel
            from src.agent.planner import LocalPlanner

            model = LocalModel(args.model, load_timeout=args.model_load_timeout)
            try:
                report = run_inference(
                    args.data_dir,
                    args.output_dir,
                    cache_dir=args.cache_dir,
                    planner=LocalPlanner(model, timeout=args.question_timeout),
                    question_ids=args.question_id,
                    resume_from=args.resume_from,
                    resume_tail=args.resume_tail,
                )
            finally:
                model.close()
        elif args.command in {"catalog", "prepare"}:
            split = load_split(args.data_dir)
            require_output(args.output_dir, split.root)
            if args.cache_dir is not None:
                require_output(args.cache_dir, split.root)
            businesses = args.business or split.businesses
            if len(set(businesses)) != len(businesses) or not set(businesses) <= set(
                split.businesses
            ):
                raise ContractError("duplicate or unknown catalog businesses")
            args.output_dir.mkdir(parents=True, exist_ok=False)
            reader = ReaderOptions(encoding=args.encoding, delimiter=args.delimiter)
            profile = ProfileOptions(
                max_distinct=args.max_distinct,
                max_counter_bytes=args.counter_memory_mib * 1024 * 1024,
            )
            results = {}
            for business in businesses:
                context = prepare_business(
                    split,
                    business,
                    cache_dir=args.cache_dir,
                    reader_options=reader,
                    profile_options=profile,
                )
                payload = dict(context.catalog)
                if args.command == "prepare":
                    facts = prepare_facts(
                        context, args.output_dir / business, cache_dir=args.cache_dir
                    )
                    payload["prepared"] = facts.summary
                    payload["semantics"] = prepare_semantics(context)
                write_json(args.output_dir / f"{business}.json", payload)
                results[business] = {
                    "read_complete": context.catalog["read_complete"],
                    "tables": len(context.catalog["tables"]),
                    "cache_hit": context.cache_hit,
                    "elapsed_seconds": context.elapsed_seconds,
                }
                if args.command == "prepare":
                    results[business]["normalization"] = facts.summary
                    results[business]["facts_cache_hit"] = facts.cache_hit
            report = {
                "businesses": results,
                "read_complete": all(r["read_complete"] for r in results.values()),
                "normalization_blocked": sum(
                    r.get("normalization", {}).get("status") == "blocked" for r in results.values()
                ),
            }
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
        return int(
            report.get("question_errors", 0) > 0
            or report.get("read_complete") is False
            or report.get("normalization_blocked", 0) > 0
        )
    except (ContractError, OSError, UnicodeError) as exc:
        print(dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
