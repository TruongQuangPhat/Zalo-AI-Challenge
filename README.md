# Zalo AI Challenge 2026 — DataMate

Một Docker image chứa toàn bộ giải pháp. Đường chạy duy nhất:

`/code/predict.sh → src.cli predict → LocalModel (vLLM) + LocalPlanner → run_inference`

## Approach

1. **Đọc dữ liệu.** Đọc `/data/rulebook.md`, `calendar.csv`, `questions.jsonl` và
   CSV/XLSX/SQLite + README của từng `data/<business_id>/`. Mỗi business chỉ chuẩn bị một
   lần: reader chặt chẽ, catalog/profile, chuẩn hoá thành `facts.sqlite` (dòng bán, header,
   ledger, tồn kho, công nợ, lô) có lineage, Decimal và quality gates.
2. **Lập kế hoạch bằng LLM.** Qwen2.5-1.5B-Instruct sinh một *structured QueryPlan* (JSON)
   bằng greedy decoding, ràng buộc bởi JSON schema (vLLM guided decoding). Model không tự
   tính số và không sinh code tự do.
3. **Thực thi bằng code có sẵn.** Plan được validate/preflight theo rulebook, README và
   coverage, rồi thực thi bằng các hàm Python cố định (`src/agent/execution.py`,
   `src/agent/business_execution.py`) trên SQLite read-only với Decimal chính xác.
4. **Chọn đáp án.** Kết quả được định dạng theo đúng type (number/percent/string/date/
   boolean/list/table). Khi intent/schema chưa hỗ trợ, plan lỗi hoặc quá thời gian thì
   trả `abstain`. Chỉ trả `unanswerable` khi có bằng chứng dữ liệu nguồn thiếu.

Phạm vi nghiệp vụ hỗ trợ được liệt kê trong [capability matrix](docs/capability-matrix.md).
Không hard-code đáp án hay logic theo question/business ID. Không đọc nhãn khi inference.

## Models

| Trường | Giá trị |
|---|---|
| Model | Qwen2.5-1.5B-Instruct (BF16 safetensors, không quantize) |
| Nguồn public | https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct |
| Revision cố định | `989aa7980e4cf806f80c7fef2b1adb7bc71aa306` |
| Ngày phát hành gốc | 2024-09-17/18 (trước 26/10/2026) |
| License | Apache-2.0 (`/code/models/qwen2.5-1.5b-instruct/LICENSE`) |
| Số tham số | 1,54B (budget khai báo 1,6B ≤ 15B); không có model phụ |
| Vị trí trong image | `/code/models/qwen2.5-1.5b-instruct/` (weights, tokenizer, config) |

Runtime: `vllm==0.10.1.1` từ base image `vllm/vllm-openai:v0.10.1.1` (PyTorch 2.7.1+cu128,
hỗ trợ RTX 5090). Cố định trong `src/agent/local.py`: một GPU (`tensor_parallel_size=1`),
`max_model_len` 4096, temperature 0, seed 0, output tối đa 256/384 tokens, tối đa 2 attempts,
120 s/câu, 900 s cho load model. Không fine-tune. Qwen2.5 không sinh reasoning riêng, nên
log có `thinking: null`; raw response của model được giữ trong `plan` của log.

## Data

- Inference chỉ dùng input của split được mount vào `/data`.
- Không có training hoặc data generation; số mẫu train/validation cho training: 0/0.
  Chi tiết xem [training_code/README.md](training_code/README.md) và `data_manifest.json`.
- Train split (có `answers.jsonl`) chỉ dùng để chấm offline sau inference. Không dùng
  public/private test để train, pseudo-label hoặc tuning.

## Input/output

- `/data` (read-only): `questions.jsonl`, `rulebook.md`, `calendar.csv`, `data/<business_id>/`.
- `/result/submission.jsonl`: một typed answer mỗi câu, ghi và fsync ngay theo thứ tự input.
- `/result/logs/<n>_<id>.json`: một record mỗi câu, gồm `id`, `answer`, `elapsed_ms`,
  `thinking`, `code` (hàm Python đã gọi), `inputs`, `output`, cùng plan và execution chi tiết.
- `/result/run_manifest.json`, `catalogs/`, `prepared/<business>/facts.sqlite`: identity,
  tiến độ, timing và dữ liệu đã chuẩn hoá.

## Layout trong image

```
/code/
├── predict.sh            # entry point, chạy không cần tham số
├── README.md
├── requirements.txt
├── src/                  # inference code (chỉ dùng standard library + vLLM)
├── models/qwen2.5-1.5b-instruct/
└── training_code/        # README no-training, data_manifest.json
```

## Build, kiểm tra và nộp (theo Docker guide)

Chuẩn bị weights một lần, vào `models/` (bị gitignore):

```bash
R=989aa7980e4cf806f80c7fef2b1adb7bc71aa306
mkdir -p models/qwen2.5-1.5b-instruct && cd models/qwen2.5-1.5b-instruct
for f in LICENSE config.json generation_config.json merges.txt tokenizer.json \
         tokenizer_config.json vocab.json model.safetensors; do
  curl -fL -o "$f" "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/resolve/$R/$f"
done
cd - && sha256sum -c training_code/model.sha256
```

Build và kiểm tra layout:

```bash
docker build --platform linux/amd64 -t zaic2026_datamate_datamate:final .
docker image inspect -f '{{.Os}}/{{.Architecture}} {{.Size}}' zaic2026_datamate_datamate:final
docker run --rm --network none --entrypoint "" zaic2026_datamate_datamate:final find /code -maxdepth 2
```

Chạy bằng đúng lệnh chấm của BTC (output mới mỗi lần):

```bash
docker run --rm --gpus '"device=0,1"' --network none --shm-size=16g \
  --entrypoint bash \
  -v /path/to/public_test:/data:ro \
  -v /path/to/output:/result \
  zaic2026_datamate_datamate:final /code/predict.sh
```

Kiểm tra trước khi nộp (mục 10): đủ câu, log đủ trường, ước lượng `load + avg × 500 ≤ 6h`,
và hai lần chạy cho cùng đáp án:

```bash
uv run --no-sync python -m scripts.check_submission \
  --data-dir /path/to/public_test --output-dir /path/to/output --compare /path/to/output-2
```

Chấm train (chỉ sau inference, đọc nhãn riêng):

```bash
uv run --no-sync python -m src.cli evaluate --answers dataset/train/answers.jsonl \
  --predictions /path/to/train-output/submission.jsonl --report /path/to/score.json
```

Export và nộp (mục 11):

```bash
set -o pipefail
docker save zaic2026_datamate_datamate:final | pigz -p 8 > zaic2026_datamate_datamate.tar.gz
pigz -dc zaic2026_datamate_datamate.tar.gz | wc -c      # uncompressed .tar bytes, ≤ 100 GB
md5sum zaic2026_datamate_datamate.tar.gz > zaic2026_datamate_datamate.tar.gz.md5
```

Upload `.tar.gz`, gửi link, file `.md5` và kích thước `.tar` không nén cho BTC. Không đổi
tên archive sau khi tạo MD5; tải lại từ link trên máy khác và chạy `md5sum -c` trước khi gửi.

## Phát triển (CPU)

```bash
uv sync --locked --python /usr/bin/python3.11
uv run --no-sync python -m scripts.verify_runtime     # lockfile, Ruff lint/format, pytest
```

`.venv` chỉ dùng cho tests, evaluator và công cụ chẩn đoán dữ liệu
(`src.cli validate/catalog/prepare`); nó không có vLLM. Inference chạy trong image. Trên host
có sẵn môi trường vLLM thì `./predict.sh --data-dir ... --output-dir ...` dùng cùng
entrypoint. Tiến độ và evidence nằm trong [roadmap.md](roadmap.md).
