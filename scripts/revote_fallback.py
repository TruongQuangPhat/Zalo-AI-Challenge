"""Re-decide code-fallback answers from a finished predict run's logs, offline.

Every sample the model wrote is logged with its executed answer, so a different vote
threshold needs no new model run. With --answers (labelled split) it prints, per threshold,
how many fallback answers are correct / wrong; with --write it writes a new submission.

usage: python -m scripts.revote_fallback --output-dir RUN [--answers answers.jsonl]
           [--min-votes 3] [--write new_submission.jsonl]
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from src.agent.code_agent import suspicious_zero, vote_key
from src.evaluation.scorer import compare


def fallback_choice(log: dict, min_votes: int, unanswerable: bool) -> dict | None:
    runs = [r for r in log.get("runs", []) if "record" in r]
    if not runs:
        return None
    votes = collections.Counter(vote_key(r["record"]) for r in runs)
    key, count = votes.most_common(1)[0]
    chosen = next(r["record"] for r in runs if vote_key(r["record"]) == key)
    if count < min_votes or (chosen["type"] == "unanswerable" and not unanswerable):
        return None
    if suspicious_zero(chosen):
        return None
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--min-votes", type=int, default=3)
    parser.add_argument("--unanswerable", action="store_true")
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()

    records = []
    for path in sorted((args.output_dir / "logs").glob("*.json")):
        records.append(json.loads(path.read_text()))
    order = [json.loads(line)["id"] for line in (args.output_dir / "submission.jsonl").open()]
    by_id = {r["id"]: r for r in records}
    gold = (
        {json.loads(line)["id"]: json.loads(line) for line in args.answers.open()}
        if args.answers
        else {}
    )

    def base_answer(record: dict) -> dict:
        # The rule planner's own answer (non-abstain answers never reached the fallback).
        if "fallback" not in (record.get("plan") or {}):
            return record["answer"]
        return {"id": record["id"], "type": "abstain"}

    if gold:
        print("min_votes unanswerable | fallback answered correct wrong | total correct wrong pa")
        for unanswerable in (False, True):
            for threshold in range(1, 9):
                fb = collections.Counter()
                total = collections.Counter()
                for qid in order:
                    record = by_id[qid]
                    answer = base_answer(record)
                    log = (record.get("plan") or {}).get("fallback")
                    if answer["type"] == "abstain" and log:
                        chosen = fallback_choice(log, threshold, unanswerable)
                        if chosen is not None:
                            answer = chosen
                            fb["answered"] += 1
                            fb["correct" if compare(chosen, gold[qid]) else "wrong"] += 1
                    if answer["type"] != "abstain":
                        total["correct" if compare(answer, gold[qid]) else "wrong"] += 1
                pa = (total["correct"] - total["wrong"]) / len(order)
                print(
                    f"{threshold:8d} {str(unanswerable):12s} | {fb['answered']:8d} "
                    f"{fb['correct']:7d} {fb['wrong']:5d} | {total['correct']:13d} "
                    f"{total['wrong']:5d} {pa:.4f}"
                )
    if args.write:
        lines = []
        counts = collections.Counter()
        for qid in order:
            record = by_id[qid]
            answer = base_answer(record)
            log = (record.get("plan") or {}).get("fallback")
            if answer["type"] == "abstain" and log:
                chosen = fallback_choice(log, args.min_votes, args.unanswerable)
                if chosen is not None:
                    answer = chosen
                    counts["fallback"] += 1
            counts[answer["type"] == "abstain" and "abstain" or "answered"] += 1
            lines.append(json.dumps(answer, ensure_ascii=False))
        args.write.write_text("\n".join(lines) + "\n")
        print(dict(counts))


if __name__ == "__main__":
    main()
