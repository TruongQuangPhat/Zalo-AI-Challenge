# Training code — no training performed

This solution performs **no training, fine-tuning, or training-data generation**
(DataMate Docker guide §8). Inference uses the public pretrained checkpoint
`Qwen/Qwen3-14B` (revision `40c069824f4251a91eefaf281ebe4c544efd3e18`, Apache-2.0,
14.77B parameters) unchanged, packaged in full under `/code/models/qwen3-14b/`.

- Training samples: 0. Validation samples for training: 0.
- No `train.sh`, configs, generated data, logs or checkpoints exist, because none were produced.
- The rulebook operators in `/code/src/` were written by hand. The competition train split
  (`answers.jsonl`) was used only to score predictions offline after inference; no weights
  were fitted.
- The question vocabulary in `/code/src/semantics/` (`question_words.txt`,
  `question_bigrams.txt`) lists the words and adjacent word pairs of the train questions and
  of their rule-generated wording variants. It is a word list read by the operators, not a
  learned model.
- `data_manifest.json` lists the packaged inference model files with sizes and SHA-256,
  plus the public source and fixed revision of the model.
- `model.sha256` is the same checksum list in `sha256sum -c` format (run from `/code`).
- `requirements.txt` is empty: there are no training dependencies.
