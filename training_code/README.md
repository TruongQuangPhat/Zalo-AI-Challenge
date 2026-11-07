# Training code — no training performed

This solution performs **no training, fine-tuning, or training-data generation**
(Docker guide §8). Inference uses the public pretrained checkpoint
`Qwen/Qwen2.5-1.5B-Instruct` unchanged, packaged in full under
`/code/models/qwen2.5-1.5b-instruct/`.

- Training samples: 0. Validation samples for training: 0.
- No `train.sh`, configs, generated data, logs or checkpoints exist, because none were produced.
- The competition train split (`answers.jsonl`) was used only to evaluate predictions
  offline with `python -m src.cli evaluate`; it was never used to fit weights, prompts or
  pseudo-labels. Public/private test data are never used for training or tuning.
- `data_manifest.json` lists the packaged inference model files with sizes and SHA-256,
  plus the public source and fixed revision of the base model.
- `model.sha256` is the same checksum list in `sha256sum -c` format (run from `/code`).
- `requirements.txt` is empty: there are no training dependencies.
