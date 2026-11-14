"""Write the question vocabulary the rule operators are measured on.

    .venv/bin/python -m scripts.build_question_words dataset/train/questions.jsonl \
        <variants>/chat_questions/questions.jsonl <variants>/phrasing_questions/questions.jsonl

Only labelled train questions and their generated wording variants may be passed: the
list is the wording on which ``scripts.eval_rule_ops`` reports zero wrong answers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.semantics.vocabulary import WORDS_FILE, question_tokens


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("questions", nargs="+", type=Path)
    args = parser.parse_args(argv)
    words = set()
    for path in args.questions:
        if "train" not in path.resolve().parts and "variants" not in path.resolve().parts:
            parser.error(f"{path}: only train questions and their variants are allowed")
        for line in path.open(encoding="utf-8"):
            words.update(question_tokens(json.loads(line)["question"]))
    WORDS_FILE.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    print(f"{len(words)} words -> {WORDS_FILE}")


if __name__ == "__main__":
    main()
