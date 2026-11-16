# Training code — no training performed

This solution performs **no training or fine-tuning** (DataMate Docker guide §8). Inference
uses the public pretrained checkpoint `Qwen/Qwen3-14B` (revision
`40c069824f4251a91eefaf281ebe4c544efd3e18`, Apache-2.0, 14.77B parameters) unchanged,
packaged in full under `/code/models/qwen3-14b/`.

- Training samples: 0. Validation samples: 0.
- No `train.sh`, training configs, training logs or checkpoints exist, because none were
  produced.
- The rulebook operators in `/code/src/` were written by hand. The competition train split
  (`answers.jsonl`) was used only to score predictions offline after inference; no weights
  were fitted.

## Question vocabulary (the only derived data)

`/code/src/semantics/question_words.txt` (1,185 words) and `question_bigrams.txt`
(4,547 adjacent word pairs) list the wording of the train questions. The operators read them
to repair mistyped words and to detect words they do not cover. They are word lists, not a
learned model. They are built as follows:

| Item | Value |
|---|---|
| Source | `data/train_questions.jsonl`: the 3,000 questions of the competition train split (30 businesses), copied unchanged |
| Generator | `data_generation/build_question_vocabulary.py` |
| Variants | Two deterministic rule rewrites of the train questions, with no model and no randomness: chat spellings (2,129 questions changed) and other everyday wordings (1,682 questions changed). The rules are listed in the script (`CHAT_FORMS`, `PHRASINGS`) |
| Output | Every casefolded word and every adjacent accentless word pair of the train questions and both variants |
| Hardware / runtime | Any CPU, under 2 seconds; Python standard library plus `/code/src` |

Rebuild and compare with the packaged files byte for byte:

```bash
cd /code
PYTHONPATH=/code python3 training_code/data_generation/build_question_vocabulary.py \
  --questions training_code/data/train_questions.jsonl --out-dir /tmp/vocabulary --check
# check: identical to src/semantics
```

The command also writes the generated variants (`chat_questions.jsonl`,
`phrasing_questions.jsonl`) to `--out-dir` for inspection.

## Files

- `data_manifest.json`: paths, sizes and SHA-256 of the train questions and the two
  vocabulary files, the generation command and the variant counts; the packaged inference
  model files with sizes and SHA-256, plus the public source and fixed revision of the model.
- `model.sha256`: the model checksum list in `sha256sum -c` format (run from `/code`).
- `requirements.txt` is empty: there are no training dependencies, and the vocabulary
  generator needs only the Python standard library and `/code/src`.
