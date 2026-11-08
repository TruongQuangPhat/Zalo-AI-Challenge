"""Score the deterministic rule operators on a labelled split, offline on CPU.

The operators never call the model, so their precision on train is measured directly:
prepare each business once (cached), answer every question the operators recognize,
and score those answers against the separate answers file.

    .venv/bin/python -m scripts.eval_rule_ops --data-dir dataset/train \
        --answers dataset/train/answers.jsonl --work-dir .artifacts/rule-ops

Shops no README mapper reads can get model column mappings (``llm_mapping``): ``--model``
proposes on GPU and caches under ``--work-dir/cache/llm_mapping``; ``--cached-proposals``
replays that cache on CPU. ``--baseline`` compares answers with an earlier predictions file
(e.g. the original train shops when scoring perturbed variants).
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import time
from pathlib import Path

from src.agent.rule_ops import answer_by_rule
from src.data.catalog import prepare_business
from src.data.normalization import prepare_facts
from src.data.split import Question, load_split
from src.evaluation.scorer import evaluate
from src.jsonio import ContractError
from src.semantics.answerability import prepare_semantics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--answers", type=Path, help="omit for an unlabelled smoke run")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true", help="print every non-correct answer")
    proposals = parser.add_mutually_exclusive_group()
    proposals.add_argument("--model", type=Path, help="local vLLM weights for mapping proposals")
    proposals.add_argument("--cached-proposals", action="store_true")
    parser.add_argument("--tensor-parallel-size", type=int, default=1)
    parser.add_argument("--max-model-len", type=int, default=16384)
    parser.add_argument("--baseline", type=Path, help="predictions.jsonl to compare answers with")
    args = parser.parse_args(argv)
    propose, model = None, None
    if args.model:
        from src.agent.local import LocalModel, MappingProposer

        model = LocalModel(
            args.model,
            load_timeout=1800,
            tensor_parallel=args.tensor_parallel_size,
            max_model_len=args.max_model_len,
        )
        propose = MappingProposer(model)
    elif args.cached_proposals:

        def propose(prompt, schema):
            raise ContractError("no cached mapping proposal")

    try:
        _evaluate(args, propose)
    finally:
        if model is not None:
            model.close()


def _evaluate(args, propose) -> None:
    split = load_split(args.data_dir)
    questions = [json.loads(line) for line in (args.data_dir / "questions.jsonl").open()]
    contexts = {}
    for business in sorted({q["business_id"] for q in questions}):
        started = time.perf_counter()
        try:
            context = prepare_business(split, business, cache_dir=args.work_dir / "cache")
            output = args.work_dir / "prepared" / business
            shutil.rmtree(output, ignore_errors=True)
            facts = prepare_facts(
                context, output, cache_dir=args.work_dir / "cache", propose=propose
            )
            prepare_semantics(context)
            contexts[business] = context
            if propose is not None:
                print(
                    f"mapping {business}: {facts.summary['status']} "
                    f"{facts.summary.get('mapping_source', 'readme')} "
                    f"{facts.summary.get('llm_rejections', '')}",
                    flush=True,
                )
        except Exception as exc:  # a failed shop abstains at runtime as well
            print(f"prepare failed {business}: {type(exc).__name__}: {exc}", flush=True)
            contexts[business] = None
        print(f"prepared {business} {time.perf_counter() - started:.1f}s", flush=True)
    rows = []
    for q in questions:
        context = contexts[q["business_id"]]
        if context is None:
            continue
        question = Question(q["id"], q["business_id"], q["question"], q["as_of_date"])
        decision = answer_by_rule(question, context)
        if decision is not None:
            rows.append((decision["plan"]["op"], q, decision["answer"]))
    if args.baseline is not None:
        asked = {q["id"] for q in questions}
        before = {
            a["id"]: a
            for a in map(json.loads, args.baseline.open(encoding="utf-8"))
            if a["id"] in asked
        }
        after = {a["id"]: a for _, _, a in rows}
        print(
            "baseline compare:",
            dict(
                collections.Counter(
                    "same"
                    if before.get(i) == after.get(i)
                    else "new"
                    if i not in before
                    else "lost"
                    if i not in after
                    else "changed"
                    for i in before.keys() | after.keys()
                )
            ),
        )
    if args.answers is None:
        counts = collections.Counter(op for op, _, _ in rows)
        for op, count in sorted(counts.items()):
            print(f"{op:20s} answered {count}")
        (args.work_dir / "predictions.jsonl").write_text(
            "".join(json.dumps(a, ensure_ascii=False) + "\n" for _, _, a in rows)
        )
        return
    gold = {json.loads(line)["id"]: line for line in args.answers.open()}
    answers, predictions = args.work_dir / "answers.jsonl", args.work_dir / "predictions.jsonl"
    answers.write_text("".join(gold[a["id"]] for _, _, a in rows))
    predictions.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for _, _, a in rows))
    verdicts = (
        {d["id"]: d["verdict"] for d in evaluate(answers, predictions)["details"]} if rows else {}
    )
    stats = collections.defaultdict(collections.Counter)
    for op, _, answer in rows:
        stats[op][verdicts[answer["id"]]] += 1
    for op, counts in sorted(stats.items()):
        print(f"{op:20s} {dict(counts)}")
    (args.work_dir / "verdicts.json").write_text(
        json.dumps({a["id"]: [op, verdicts[a["id"]]] for op, _, a in rows})
    )
    if args.verbose:
        for op, q, answer in rows:
            if verdicts[answer["id"]] != "correct":
                expected = json.loads(gold[q["id"]]).get("answer")
                print(
                    f"{verdicts[answer['id']]} {op} | {q['question'][:90]} | gold {expected}"
                    f" | pred {answer.get('answer')}"
                )


if __name__ == "__main__":
    main()
