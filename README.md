# Zalo AI Challenge — DataMate

Runtime hiện chỉ có đường chạy **mock abstain**: kiểm tra input, xuất JSONL/log theo câu và evaluator riêng. Phase 1 bổ sung đặc tả dữ liệu, validation manifest và công cụ annotation offline; chưa có LLM, ETL bảng nghiệp vụ hoặc SQL execution. Tiến độ và bằng chứng nằm trong [roadmap.md](roadmap.md).

## Môi trường riêng

Dùng uv với `.venv` của dự án, Python 3.11; không dùng môi trường của dự án khác. `pyproject.toml` và `uv.lock` là nguồn dependency duy nhất. Môi trường hiện có torch 2.6.0+cu124 và NumPy 2.4.6 cho GPU local; mock inference chỉ cần standard library.

```bash
uv sync --locked --python /usr/bin/python3.11
```

Không cần chạy setup lại nếu `.venv` đã có. Không cài package hoặc tải weights trong inference.

## Verify Phase 0

Một lệnh kiểm tra lint/format, test cốt lõi, train/public smoke, tính bất biến của input, Docker offline/mount và CUDA:

```bash
uv run --no-sync python -m scripts.verify_runtime --scope all
```

Lệnh ghi đè **một report** tại [reports/runtime_verification.json](reports/runtime_verification.json). Predictions/logs/context dùng khi verify nằm trong thư mục tạm và tự xóa sau lượt chạy, kể cả khi kiểm tra lỗi. Không sinh JUnit, nhật ký hoặc thư mục runs tích lũy.

Nếu chỉ kiểm tra phần CPU, dùng `--scope cpu`; kết quả này không nghiệm thu Docker/CUDA. Chỉ ghi nhận hoàn thành bản hiện tại khi `runtime_verified=true` và mọi gate `passed`. Bản sau cleanup đã được full reverify trên host ngày 01/11/2026: 58 tests, quality/CPU/Docker/GPU đều đạt; Phase 0/M0 hoàn thành theo report mới.

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

Output của lần chạy chủ động gồm `submission.jsonl`, `logs/` và `run_manifest.json`. Các file này được giữ để người dùng xem; chỉ output do verifier tạo mới tự xóa.

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

## Cấu trúc tối thiểu

- `src/`: contracts, đọc split, mock runtime và evaluator.
- `scripts/verify_runtime.py`: verifier duy nhất; `build_context.py` và `probe_environment.py` là hai helper Docker/phần cứng.
- `tests/`: output contracts, scorer, runtime và deliverables Phase 1; dùng dữ liệu synthetic.
- `scripts/audit_dataset.py`, `scripts/annotate_questions.py`: đối chiếu/validate train và annotation offline; runtime không import hai script này.
- `docs/`: data contracts, observations, capability matrix và acceptance cases; `config/validation_split.json` giữ protocol đánh giá.
- `README.md`, `roadmap.md`: hướng dẫn và kế hoạch; không duy trì ADR/nhật ký/config mô tả trùng lặp.
- `reports/runtime_verification.json`, `reports/data_preparation.json`: bằng chứng nghiệm thu từng phase; `.venv/`, `.artifacts/`, `dataset/` được ignore.

Rulebook/README/calendar đúng split là nguồn ngữ nghĩa runtime. Inference không đọc nhãn/EDA, không kết nối mạng và không sửa input. Scorer nội bộ chưa được đối chiếu hoàn toàn với evaluator BTC; các giới hạn model/phần cứng/nộp bài cần đối chiếu theo mục 2.1 và Phụ lục B của roadmap. Model candidates ở Phụ lục G là hướng nghiên cứu, chưa có model active hoặc weights được tải.
