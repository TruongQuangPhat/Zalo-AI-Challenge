# Zalo AI Challenge — DataMate

Một đường chạy inference duy nhất:

`predict.sh → src.cli predict → LocalModel + LocalPlanner → run_inference → typed JSONL`

Runtime tự đọc rulebook/calendar và CSV/XLSX/SQLite, chuẩn bị facts một lần/business,
validate/preflight structured plan rồi thực thi bằng SQLite read-only và Python Decimal.
Không cần chạy preprocessing riêng. Không có mock mode hoặc fallback khi thiếu model.
Phạm vi nghiệp vụ hữu hạn nằm trong [capability matrix](docs/capability-matrix.md);
khả năng chưa hỗ trợ giữ abstain. Lịch sử/bằng chứng nằm trong `roadmap.md`.

## Setup

Dùng Python 3.11 và `.venv` của repository. Nếu môi trường hiện tại đã chạy được
CPU/GPU thì giữ nguyên; không sync lại để vô tình thay CUDA binding đang dùng.
Trên môi trường mới, cài từ lockfile trước khi inference:

```bash
uv sync --locked --python /usr/bin/python3.11 --group llm
```

Weights là file local có sẵn, không nằm trong Git. Nguồn/checksum/license và parameter
budget khai báo tại [config/submission_model.json](config/submission_model.json);
[license model](docs/model-license.txt) được giữ trong image.

Baseline dùng trực tiếp Qwen2.5-1.5B-Instruct-GGUF Q4_K_M, Apache-2.0, weights phát
hành 18/09/2024, revision `91cad51170dc346986eccefdc2dd33a9da36ead9`.
Không fine-tune hoặc sinh training data; số mẫu train/validation cho training: 0/0.
GGUF chứa tokenizer metadata; không cần tải tokenizer khi chạy. Không có model phụ.
Runtime `llama-cpp-python==0.3.16`, ChatML, seed 0, temperature 0, context 4096,
batch 128, threads 4, output tối đa 256/384 tokens, tối đa hai attempts, 120 s/câu,
180 s/load. Tổng budget khai báo bảo thủ 1.6B, dưới giới hạn 15B.

## Chạy inference

Chạy từ repository root, chọn output mới:

```bash
./predict.sh \
  --data-dir dataset/train \
  --output-dir .artifacts/train-run-new \
  --model .artifacts/models/qwen2.5-1.5b-instruct/model.gguf \
  --gpu-layers 0
```

GPU dùng **cùng lệnh**, đổi `--gpu-layers -1`. Runtime từ chối silent CPU fallback
khi đã yêu cầu GPU. Smoke subset thêm `--question-id ID` cho từng câu cần chạy;
bỏ option để chạy toàn split. Không sửa đáp án hoặc dùng test làm training data.

Defaults nằm tại `src/cli.py`, không lặp lại trong shell wrappers:

| Option | Default |
|---|---|
| `--data-dir` | `/data` |
| `--output-dir` | `/result` |
| `--model` | `DATAMATE_MODEL`, nếu chưa đặt: `.artifacts/models/qwen2.5-1.5b-instruct/model.gguf` trong repo |
| `--gpu-layers` | `DATAMATE_GPU_LAYERS`, mặc định 0 |
| `--cache-dir` | `DATAMATE_CACHE`, mặc định không dùng cache đĩa |

Thiếu weights/dependencies là lỗi, không tạo một submission toàn abstain giả.
Trong Docker, **chính file root này** được copy tới `/code/predict.sh`; không có
entrypoint inference thứ hai. Image đặt model
`/opt/model/model.gguf`, cache `/result/cache`, GPU layers -1; chạy no-args đúng BTC.

Input split gồm `questions.jsonl`, `rulebook.md`, `calendar.csv`, `data/<business>/`.
Inference không đọc `answers.jsonl`, EDA answer reports hoặc labels. Input Docker
mount read-only. Output nằm ngoài input/dataset:

- `submission.jsonl`: một typed answer mỗi câu, ghi/fsync theo thứ tự input.
- `logs/*.json`: calls/plans, execution, errors, elapsed_ms, thinking và code/inputs/output.
- `run_manifest.json`: source/input/model/config identity, progress và preparation timing.
- `catalogs/`, `prepared/<business>/facts.sqlite`: dữ liệu suy ra và provenance.

Qwen ở cấu hình hiện tại không sinh reasoning channel riêng, nên thinking=null;
raw structured responses được giữ nguyên trong logs. Ghi log không đổi format JSONL.
Cache là catalog/facts, không phải đáp án; fingerprint/config/checksum kiểm stale data.
Thêm `--cache-dir .artifacts/source-cache` để reuse preparation hợp lệ.

## Tiếp tục run và chấm train

Resume yêu cầu source/model/input/config khớp, parent đã dừng và output mới:

```bash
./predict.sh --data-dir dataset/train --output-dir .artifacts/resumed-new \
  --model .artifacts/models/qwen2.5-1.5b-instruct/model.gguf \
  --resume-from .artifacts/train-run-new --gpu-layers 0
```

Checkpoint cũ khác source revision không được ghép vào run mới. Evaluator là command
riêng, chỉ chạy với labels train sau inference:

```bash
uv run --no-sync python -m src.cli evaluate \
  --answers dataset/train/answers.jsonl \
  --predictions .artifacts/train-run-new/submission.jsonl \
  --report .artifacts/train-score-new.json
```

`src.cli validate/catalog/prepare` là công cụ chẩn đoán input, không là pipeline
prediction khác. Không chấm public/private test bằng nhãn tự thu thập.

## Kiểm tra và đóng gói

```bash
uv run --no-sync python -m scripts.verify_runtime --scope cpu
```

CPU gate chạy lint/format/tests và kiểm context source; **không chạy model giả hoặc
chứng nhận Docker/GPU**. Report hiện hành: `reports/runtime_verification.json`.
Test doubles chỉ nằm trong `tests/`, không nằm trong image hoặc production fallback.

Build/test/export/reload dùng [một hướng dẫn submission](docs/submission_guide.md).
Các thao tác đóng gói trong `scripts.submission` đều gọi lại root predict.sh:

```bash
uv run --no-sync python -m scripts.submission prepare \
  --base-image datamate-baseline:local --image zaic2026_datamate_team:review1 \
  --input dataset/train --output .artifacts/submission-review-new \
  --mode smoke --limit 3 --gpus none --timeout 1800
```

Mặc định dry-run. Thêm `--execute` khi đã dành tài nguyên để build, test, export/load.
Không upload, commit hoặc freeze tự động. Full gate cần image mới đã build:

```bash
uv run --no-sync python -m scripts.verify_runtime --scope all \
  --image zaic2026_datamate_team:review1 --data-dir dataset/train --gpus all
```

Image cũ `/app` cần rebuild trước khi dùng đường chạy `/code`. CUDA12.4/GTX1650 local
chưa chứng nhận RTX5090/Blackwell, và chưa chứng nhận SLA sáu giờ trên máy BTC.

## Colab: train 3000 và public test 1000

Dùng duy nhất [scripts/DataMate_Colab.ipynb](scripts/DataMate_Colab.ipynb), chọn GPU và
Run all. Notebook clone GitHub, tự setup model/runtime đã pin; không cần runtime ZIP.
Repo private dùng Colab Secret `GITHUB_TOKEN` với quyền Contents:read. Ref được clone
phải chứa notebook/helper train-public mới; thay đổi chưa push không tự có trên Colab.

Dữ liệu đã có trong `MyDrive/DataMate/`: `train.zip`, folder `train/` và
`public_test/`. Folder split chứa `questions.jsonl`, `rulebook.md`, `calendar.csv`,
`data/`; train có thêm `answers.jsonl` để evaluator riêng sử dụng sau inference.

| Cấu hình notebook | Hành vi |
|---|---|
| `SPLIT="train"` | Đủ3000 predictions, sau đó chấm cả3000 nếu `EVALUATE_TRAIN=True` |
| `SPLIT="public_test"` | Đủ1000 predictions, không đọc nhãn/chấm điểm; tải file public JSONL |
| `INPUT_SOURCE="auto"` | Ưu tiên `<split>.zip` nếu có, nếu không dùng folder `<split>/` |
| `INPUT_SOURCE="folder"` | Dùng trực tiếp folder khi đã giải nén hoặc folder mới hơn ZIP |
| `CHUNK_SIZE=100` | Checkpoint mỗi100 câu; giữ RUN_ID/source/config để resume |

Chỉ đổi `SPLIT` rồi Run all để chuyển chế độ; train/public có namespace riêng.
Inference vẫn gọi root `predict.sh`, chạy trên SSD `/content`, không đọc SQLite từ
Drive. Kết quả/checkpoint nằm ở `DataMate/results/<split>/<RUN_ID>/`; phần đang chạy
dở chưa checkpoint có thể phải chạy lại khi VM mất. Đổi RUN_ID cho lượt độc lập.
Public `submission.jsonl` để tự nộp portal → Submission; file train không dùng nộp
public leaderboard. Notebook không tự submit/commit/push và chưa được test hosted Colab.
