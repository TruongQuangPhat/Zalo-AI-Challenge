# Zalo AI Challenge 2026 — DataMate

Một Docker image chứa toàn bộ giải pháp, theo *Docker Submission Guide — DataMate*. Đường
chạy duy nhất:

`/code/predict.sh → src.cli predict → LocalModel (vLLM, Qwen3-14B) + LocalPlanner → run_inference`

## Approach

1. **Đọc dữ liệu.** Đọc `/data/rulebook.md`, `calendar.csv`, `questions.jsonl` và
   CSV/XLSX/SQLite + README của từng `data/<business_id>/`. Mỗi business được chuẩn bị một
   lần thành `facts.sqlite` (dòng bán, đơn, khách, thanh toán, tồn kho, nhập hàng, lô) có
   lineage và số Decimal chính xác:
   - nghĩa cột lấy từ README của shop;
   - README thiếu hoặc không khớp: Qwen3-14B đề xuất nghĩa cột (JSON theo schema), đề xuất
     chỉ được dùng khi qua kiểm tra số học, ngày và trạng thái;
   - trạng thái đơn lạ: model phân loại về hoàn thành / huỷ / trả / đang giao.
2. **Trả lời câu hỏi** (`src/agent/planner.py`):
   - chuẩn hoá câu hỏi: chữ chat, lỗi gõ, tên món viết theo phương ngữ Bắc/Nam được đổi về
     tên trong danh mục;
   - **phép tính theo rulebook** (`src/agent/rule_ops.py` và các module ngành): mỗi phép là
     một hàm Python cố định, có chốt chặn về từ ngữ, hình dạng đáp án và điều kiện chưa tính;
   - không phép nào nhận: model viết lại câu theo cách nói chuẩn rồi thử lại;
   - vẫn không: model lập *QueryPlan* JSON (guided decoding), plan được kiểm tra theo
     rulebook/README rồi thực thi bằng hàm cố định (`src/agent/execution.py`,
     `src/agent/business_execution.py`) trên SQLite read-only.
3. **Chọn đáp án.** Kết quả được định dạng đúng type (number/percent/string/date/boolean/
   list/table). Không đủ chắc chắn thì trả `abstain`; chỉ trả `unanswerable` khi rulebook
   hoặc dữ liệu cho thấy shop không ghi thông tin được hỏi.

Model không tự tính số và không sinh code tự do: mọi con số đến từ hàm Python có sẵn. Không
hard-code đáp án hay logic theo question/business ID.

## Models

| Trường | Giá trị |
|---|---|
| Model | Qwen3-14B (BF16 safetensors, không quantize, không fine-tune) |
| Nguồn public | https://huggingface.co/Qwen/Qwen3-14B |
| Revision cố định | `40c069824f4251a91eefaf281ebe4c544efd3e18` |
| Ngày phát hành gốc | 29/04/2025 (repo tạo 27/04/2025), trước 26/10/2026 |
| License | Apache-2.0 (`/code/models/qwen3-14b/LICENSE`) |
| Số tham số | 14,77B (≤ 15B); không có model phụ |
| Vị trí trong image | `/code/models/qwen3-14b/` (8 shard safetensors, tokenizer, config) |

Runtime: `vllm==0.10.1.1` trên base image `vllm/vllm-openai:v0.10.1.1` (PyTorch
2.7.1+cu128, hỗ trợ RTX 5090). Mặc định cố định trong `src/cli.py` và `src/agent/local.py`:

- 2 GPU (`tensor_parallel_size=2`), `NCCL_P2P_DISABLE=1` và `nvidia-nccl-cu12==2.27.7` theo
  Dockerfile mẫu của hướng dẫn;
- `max_model_len` 16384, `gpu_memory_utilization` 0,85;
- greedy (temperature 0), seed 0, guided JSON decoding, tối đa 256/384 token mỗi plan;
- 120 s mỗi câu, 900 s cho load model.

Chế độ thinking của Qwen3 tắt (`enable_thinking=False`) vì grammar JSON không cho phép, nên
log ghi `thinking: null`; raw response của model nằm trong `plan` của log.

## Data

- Inference chỉ dùng input của split được mount vào `/data`.
- Không có training, fine-tuning hay sinh dữ liệu; số mẫu train/validation: 0/0. Chi tiết
  ở `training_code/README.md` và `training_code/data_manifest.json`.
- Phép tính theo rulebook do đội viết tay. Căn cứ: rulebook, README và dữ liệu của bộ train
  (có `answers.jsonl`, chỉ dùng để chấm offline sau inference). Trong lúc phát triển, đội cũng
  đã đọc câu hỏi và dữ liệu public test (không có nhãn) để tìm lỗi và mở rộng luật. Không
  có đáp án nào được ghi cứng; mọi đáp án được tính từ dữ liệu lúc chạy.

## Input/output

- `/data` (read-only): `questions.jsonl`, `rulebook.md`, `calendar.csv`, `data/<business_id>/`.
- `/result/submission.jsonl`: một typed answer mỗi câu, ghi và fsync ngay theo thứ tự input.
- `/result/logs/<n>_<id>.json`: một record mỗi câu, gồm `id`, `answer`, `elapsed_ms`,
  `thinking`, `code` (hàm Python đã gọi), `inputs`, `output`, cùng plan và execution chi tiết.
- `/result/run_manifest.json`, `catalogs/`, `prepared/<business>/facts.sqlite`: identity,
  tiến độ, thời gian và dữ liệu đã chuẩn hoá.

## Layout trong image

```
/code/
├── predict.sh            # entry point, chạy không cần tham số
├── README.md
├── requirements.txt
├── src/                  # inference code (standard library + vLLM)
├── models/qwen3-14b/     # weights, tokenizer, config
└── training_code/        # README (không training), data_manifest.json, model.sha256
```

## Build, kiểm tra và nộp

Tải weights một lần vào `models/` (bị gitignore), rồi kiểm tra checksum:

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

Build và kiểm tra layout (team name viết thường thay cho `datamate`):

```bash
docker build --platform linux/amd64 -t zaic2026_datamate_datamate:final .
docker image inspect -f '{{.Os}}/{{.Architecture}} {{.Size}}' zaic2026_datamate_datamate:final
docker run --rm --network none --entrypoint "" zaic2026_datamate_datamate:final find /code -maxdepth 2
```

Chạy bằng đúng lệnh chấm (2 GPU, không mạng, input read-only, shm 16 GiB; output mới mỗi lần):

```bash
docker run --rm --gpus '"device=0,1"' --network none --shm-size=16g \
  --entrypoint bash \
  -v /path/to/public_test:/data:ro \
  -v /path/to/output:/result \
  zaic2026_datamate_datamate:final /code/predict.sh
```

Trước khi nộp, kiểm tra: chạy hết không lỗi; `submission.jsonl` đủ câu; mỗi câu có log với
`id`, `answer`, `elapsed_ms`, `thinking`; `load + thời gian trung bình mỗi câu × 500 ≤ 6 giờ`;
hai lần chạy cho cùng đáp án.

Export và nộp:

```bash
set -o pipefail
docker save zaic2026_datamate_datamate:final | pigz -p 8 > zaic2026_datamate_datamate.tar.gz
pigz -dc zaic2026_datamate_datamate.tar.gz | wc -c      # uncompressed .tar bytes, ≤ 100 GB
md5sum zaic2026_datamate_datamate.tar.gz > zaic2026_datamate_datamate.tar.gz.md5
```

Upload `.tar.gz`, gửi link, file `.md5` và kích thước `.tar` không nén cho BTC. Không đổi
tên archive sau khi tạo MD5; tải lại từ link trên máy khác và chạy `md5sum -c` trước khi gửi.
