# Zalo AI Challenge 2026 — DataMate

One Docker image holds the whole solution, following the *DataMate Docker Submission Guide*.
There is a single run path:

`/code/predict.sh → src.cli predict → LocalModel (vLLM, Qwen3-14B) + LocalPlanner → run_inference`

## Approach

1. **Reading the data.** The run reads `/data/rulebook.md`, `calendar.csv`,
   `questions.jsonl` and the CSV/XLSX/SQLite files plus README of every `data/<business_id>/`.
   Each business is prepared once into `facts.sqlite` (sale lines, orders, customers,
   payments, stock, purchases, lots) with source lineage and exact Decimal values:
   - column meanings come from the shop README;
   - when the README is missing or does not match the files, Qwen3-14B proposes column
     meanings as schema-constrained JSON; a proposal is used only after arithmetic, date and
     status checks pass;
   - unknown order-status labels are classified by the model as completed, cancelled,
     returned or in delivery.
2. **Answering a question** (`src/agent/planner.py`):
   - the question is normalised: chat spellings, typing errors, and item names written in
     another regional form are mapped to standard words and catalog names;
   - **rulebook operators** (`src/agent/rule_ops.py` and the industry modules) are tried
     first. Each is a fixed Python function, guarded on wording, answer shape and
     conditions it does not compute;
   - if no operator applies, the model rewrites the question in standard wording and the
     operators are tried again;
   - otherwise the model writes a JSON *QueryPlan* (guided decoding). The plan is validated
     against the rulebook and README, then executed by fixed functions
     (`src/agent/execution.py`, `src/agent/business_execution.py`) on read-only SQLite.
3. **Choosing the answer.** Results are formatted to the required type (number, percent,
   string, date, boolean, list, table). Without enough certainty the answer is `abstain`;
   `unanswerable` is returned only when the rulebook or data show the shop does not record
   what is asked.

The model never computes numbers and never writes free-form code: every value comes from a
packaged Python function. There are no hard-coded answers and no logic keyed on question or
business IDs.

## Models

| Field | Value |
|---|---|
| Model | Qwen3-14B (BF16 safetensors, not quantized, not fine-tuned) |
| Public source | https://huggingface.co/Qwen/Qwen3-14B |
| Fixed revision | `40c069824f4251a91eefaf281ebe4c544efd3e18` |
| Original release date | 2025-04-29 (repository created 2025-04-27), before 2026-10-26 |
| License | Apache-2.0 (`/code/models/qwen3-14b/LICENSE`) |
| Parameters | 14.77B (≤ 15B); no other model |
| Location in the image | `/code/models/qwen3-14b/` (8 safetensors shards, tokenizer, config) |

Runtime: `vllm==0.10.1.1` on the base image `vllm/vllm-openai:v0.10.1.1` (PyTorch
2.7.1+cu128, RTX 5090 support). Fixed defaults in `src/cli.py` and `src/agent/local.py`:

- 2 GPUs (`tensor_parallel_size=2`), with `NCCL_P2P_DISABLE=1` and
  `nvidia-nccl-cu12==2.27.7` as in the guide's reference Dockerfile;
- `max_model_len` 16384, `gpu_memory_utilization` 0.85;
- greedy decoding (temperature 0), seed 0, guided JSON decoding, at most 256/384 output
  tokens per plan;
- 120 s per question, 900 s for model loading.

Qwen3 thinking mode is disabled (`enable_thinking=False`) because the JSON grammar does not
allow it, so logs record `thinking: null`; the raw model response is kept in the log's `plan`.

## Data

- Inference reads only the split mounted at `/data`.
- No training, fine-tuning or data generation was performed; train/validation samples: 0/0.
  See `training_code/README.md` and `training_code/data_manifest.json`.
- The rulebook operators are hand-written from the rulebook, the shop READMEs and the
  provided train split. The train answers (`answers.jsonl`) are used only to score
  predictions offline after inference.

## Input/output

- `/data` (read-only): `questions.jsonl`, `rulebook.md`, `calendar.csv`, `data/<business_id>/`.
- `/result/submission.jsonl`: one typed answer per question, written and fsynced in input
  order.
- `/result/logs/<n>_<id>.json`: one record per question with `id`, `answer`, `elapsed_ms`,
  `thinking`, `code` (the Python function called), `inputs`, `output`, plus the detailed plan
  and execution.
- `/result/run_manifest.json`, `catalogs/`, `prepared/<business>/facts.sqlite`: identity,
  progress, timing and the prepared data.

## Layout in the image

```
/code/
├── predict.sh            # entry point, runs without arguments
├── README.md
├── requirements.txt
├── src/                  # inference code (standard library + vLLM)
├── models/qwen3-14b/     # weights, tokenizer, config
└── training_code/        # README (no training), data_manifest.json, model.sha256
```

## Build, check and submit

Download the weights once into `models/` (git-ignored) and verify the checksums:

```bash
R=40c069824f4251a91eefaf281ebe4c544efd3e18
mkdir -p models/qwen3-14b && cd models/qwen3-14b
for f in LICENSE config.json generation_config.json merges.txt tokenizer.json \
         tokenizer_config.json vocab.json model.safetensors.index.json \
         model-0000{1..8}-of-00008.safetensors; do
  curl -fL -o "$f" "https://huggingface.co/Qwen/Qwen3-14B/resolve/$R/$f"
done
cd - && sha256sum -c training_code/model.sha256
```

Build and inspect the layout (replace `datamate` with the lowercase team name):

```bash
docker build --platform linux/amd64 -t zaic2026_datamate_datamate:final .
docker image inspect -f '{{.Os}}/{{.Architecture}} {{.Size}}' zaic2026_datamate_datamate:final
docker run --rm --network none --entrypoint "" zaic2026_datamate_datamate:final find /code -maxdepth 2
```

Run with the scoring command (two GPUs, no network, read-only input, 16 GiB shared memory;
use a fresh output directory each time):

```bash
docker run --rm --gpus '"device=0,1"' --network none --shm-size=16g \
  --entrypoint bash \
  -v /path/to/public_test:/data:ro \
  -v /path/to/output:/result \
  zaic2026_datamate_datamate:final /code/predict.sh
```

Before submitting, check that the run finishes without errors, `submission.jsonl` covers
every question, every question has a log with `id`, `answer`, `elapsed_ms` and `thinking`,
`model loading + average time per question × 500 ≤ 6 hours`, and two runs give the same
answers.

Export and submit:

```bash
set -o pipefail
docker save zaic2026_datamate_datamate:final | pigz -p 8 > zaic2026_datamate_datamate.tar.gz
pigz -dc zaic2026_datamate_datamate.tar.gz | wc -c      # uncompressed .tar bytes, ≤ 100 GB
md5sum zaic2026_datamate_datamate.tar.gz > zaic2026_datamate_datamate.tar.gz.md5
```

Upload the `.tar.gz` and send its link, the `.md5` file and the uncompressed `.tar` size to
the organizers. Do not rename the archive after creating the MD5; download it from the link
on another machine and run `md5sum -c` before sending.
