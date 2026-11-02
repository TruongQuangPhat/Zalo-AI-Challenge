# Zalo AI Challenge — DataMate

Runtime đọc CSV/XLSX/SQLite, tạo catalog/profile theo business, rồi chạy **mock abstain** và ghi JSONL/log theo câu. Có provenance, thống kê có giới hạn bộ nhớ và cache catalog tùy chọn; chưa có normalization nghiệp vụ, metric execution hoặc LLM. Tiến độ và bằng chứng nằm trong [roadmap.md](roadmap.md).

## Môi trường riêng

Dùng uv với `.venv` của dự án, Python 3.11; không dùng môi trường của dự án khác. `pyproject.toml` và `uv.lock` là nguồn dependency duy nhất. Môi trường hiện có torch 2.6.0+cu124 và NumPy 2.4.6 cho GPU local; mock inference chỉ cần standard library.

```bash
uv sync --locked --python /usr/bin/python3.11
```

Không cần chạy setup lại nếu `.venv` đã có. Không cài package hoặc tải weights trong inference.

## Kiểm tra runtime

Một lệnh kiểm tra lint/format, test cốt lõi, train/public smoke, tính bất biến của input, Docker offline/mount và CUDA:

```bash
uv run --no-sync python -m scripts.verify_runtime --scope all
```

Lệnh ghi đè **một report** tại [reports/runtime_verification.json](reports/runtime_verification.json). Predictions/logs/context dùng khi verify nằm trong thư mục tạm và tự xóa sau lượt chạy, kể cả khi kiểm tra lỗi. Không sinh JUnit, nhật ký hoặc thư mục runs tích lũy.

Nếu chỉ kiểm tra phần CPU, dùng `--scope cpu`; kết quả này không nghiệm thu Docker/CUDA. Chỉ ghi nhận hoàn thành bản hiện tại khi `runtime_verified=true` và mọi gate `passed`. Bằng chứng cũ của M0 được ghi trong roadmap; report runtime luôn thuộc lượt kiểm tra gần nhất.

Docker cần daemon truy cập được và base image `python:3.11-slim-bookworm` đã có ở máy. Verifier không pull ngầm; có thể chọn image local khác bằng `DATAMATE_BASE_IMAGE`. Image này là bootstrap CPU; tương thích image BTC/Blackwell vẫn cần xác minh ở bước đóng gói model.

## Chạy mock và chấm riêng

```bash
# Dùng output mới, nằm ngoài dataset
./predict.sh --data-dir dataset/train --output-dir .artifacts/my-run

# Chỉ evaluator mới đọc nhãn
uv run --no-sync python -m src.cli evaluate \
  --answers dataset/train/answers.jsonl \
  --predictions .artifacts/my-run/submission.jsonl
```

Output của lần chạy chủ động gồm `submission.jsonl`, `logs/`, một catalog mỗi business trong `catalogs/` và `run_manifest.json`. Context được chuẩn bị một lần/business và không cắt theo ngày của câu hỏi. Các file này được giữ để người dùng xem; chỉ output do verifier tạo mới tự xóa. Thêm `--cache-dir .artifacts/source-cache` khi muốn tái sử dụng catalog giữa các lượt; mặc định không sinh cache đĩa.

## Kiểm tra deliverables Phase 1

Chạy từ root với output mới nằm ngoài `dataset/`. Lệnh đầu đối chiếu inventory train,
fingerprint và membership đã khóa; không dùng `--freeze` với manifest đã hoàn chỉnh.
Lệnh sau tạo annotation đề xuất từ câu hỏi, không đọc giá trị nhãn.

```bash
uv run --no-sync python -m scripts.audit_dataset \
  --data-dir dataset/train --eda-dir dataset/explore \
  --manifest config/validation_split.json --output-dir .artifacts/dataset-audit-check
uv run --no-sync python -m scripts.annotate_questions \
  --data-dir dataset/train --manifest config/validation_split.json \
  --output-dir .artifacts/question-annotations-check
```

Để tái tạo annotation có 100 câu đã rà, thêm
`--reviews .artifacts/data_preparation/pilot_reviews.jsonl` vào lệnh annotation nếu còn artifact cục bộ.
Review không được commit; khi thiếu file này, output là toàn bộ đề xuất, không có manual review.
Fixtures tại `tests/fixtures/data_acceptance_cases.json` là yêu cầu nghiệp vụ, chưa phải engine đã chạy đạt.
Chạy tests bằng `uv run --no-sync python -m pytest`; toolchain vẫn trong `.venv` riêng.

## Kiểm tra nguồn và catalog

Chỉ kiểm tra cấu trúc, không đọc đáp án hoặc tính metric:

```bash
uv run --no-sync python -m src.cli catalog \
  --data-dir dataset/train --business biz_101 \
  --output-dir .artifacts/source-inspection
```

Một JSON/business chứa manifest, tables/columns, locator, row accounting, issues và profile.
Raw rows được đọc theo luồng qua `BusinessContext.iter_rows(source_id)` khi cần; không sao chép
mọi cell vào catalog. Cell numeric XLSX giữ token gốc, ngày chưa parse và coverage chưa có
căn cứ giữ unknown. Header/summary là phân loại cấu trúc, không phải facts đã chuẩn hóa.

CSV mặc định UTF-8 có/không BOM và tự nhận comma/semicolon/tab/pipe. Dùng `--encoding` hoặc
`--delimiter` chỉ khi có căn cứ nguồn. Giới hạn mặc định: 100.000 distinct/cột, 32 MiB counter
accounting/bảng, 12 key candidates/bảng, 1.000 issue samples/bảng, 5 value samples/cột;
relationship evidence giữ tối đa 16 MiB/business, kiểm tra tối đa 64 quan hệ. Vượt giới hạn
được ghi partial/not_checked; đây không phải giới hạn cứng RSS của toàn process. Raw reader
giới hạn cell 1 MiB, 1.024 cột và shared strings XLSX 16 MiB; vượt giới hạn báo failed có ref.

Verifier nguồn mặc định dùng giao hai dev; mọi catalog/cache/pytest output nằm trong thư mục
tạm và tự dọn. Một report ngắn tại [reports/ingestion_verification.json](reports/ingestion_verification.json)
ghi quality, inventory, row accounting, hai cold runs, warm cache, input hashes và peak RSS:

```bash
uv run --no-sync python -m scripts.verify_ingestion --scope dev
# Sau khi khóa code/config, nghiệm thu kỹ thuật toàn train:
uv run --no-sync python -m scripts.verify_ingestion --scope all-train
```

Đây là phép kiểm tra reader, không phải đánh giá PA trên holdout. Không dùng lỗi holdout để
tune rồi tiếp tục gọi kết quả là final độc lập. Fixtures nghiệp vụ C01–C30 vẫn giữ trạng thái
planned cho tới khi normalization/execution tương ứng được triển khai.

## Cấu trúc tối thiểu

- `src/`: contracts, split, source readers/profile/catalog/cache, mock runtime và evaluator.
- `scripts/verify_runtime.py`: kiểm tra runtime/Docker/GPU; `verify_ingestion.py` kiểm tra nguồn train với output tạm. `build_context.py`, `probe_environment.py` hỗ trợ Docker/phần cứng.
- `tests/`: output/scorer/runtime, data tools, readers và catalog/cache; dùng dữ liệu synthetic.
- `scripts/audit_dataset.py`, `scripts/annotate_questions.py`: đối chiếu/validate train và annotation offline; runtime không import hai script này.
- `docs/`: data contracts, observations, capability matrix và acceptance cases; `config/validation_split.json` giữ protocol đánh giá.
- `README.md`, `roadmap.md`: hướng dẫn và kế hoạch; không duy trì ADR/nhật ký/config mô tả trùng lặp.
- `reports/`: bằng chứng runtime, data preparation và ingestion; `.venv/`, `.artifacts/`, `dataset/` được ignore.

Rulebook/README/calendar đúng split là nguồn ngữ nghĩa runtime. Inference không đọc nhãn/EDA, không kết nối mạng và không sửa input. Scorer nội bộ chưa được đối chiếu hoàn toàn với evaluator BTC; các giới hạn model/phần cứng/nộp bài cần đối chiếu theo mục 2.1 và Phụ lục B của roadmap. Model candidates ở Phụ lục G là hướng nghiên cứu, chưa có model active hoặc weights được tải.
