# Zalo AI Challenge — DataMate

Phase 0 hiện chỉ có đường chạy **mock abstain**: kiểm tra input, xuất JSONL/log theo câu và evaluator riêng. Chưa có LLM, ETL bảng nghiệp vụ hoặc SQL execution. Kế hoạch triển khai nằm trong [roadmap.md](roadmap.md).

## Môi trường riêng

Dùng uv với `.venv` của dự án, Python 3.11; không dùng môi trường của dự án khác. `pyproject.toml` và `uv.lock` là nguồn dependency duy nhất. Môi trường hiện có torch 2.6.0+cu124 và NumPy 2.4.6 cho GPU local; mock inference chỉ cần standard library.

```bash
uv sync --locked --python /usr/bin/python3.11
```

Không cần chạy setup lại nếu `.venv` đã có. Không cài package hoặc tải weights trong inference.

## Verify Phase 0

Một lệnh kiểm tra lint/format, test cốt lõi, train/public smoke, tính bất biến của input, Docker offline/mount và CUDA:

```bash
uv run --no-sync python -m scripts.check_phase0 --scope all
```

Lệnh ghi đè **một report** tại [reports/phase0.json](reports/phase0.json). Predictions/logs/context dùng khi verify nằm trong thư mục tạm và tự xóa sau lượt chạy, kể cả khi kiểm tra lỗi. Không sinh JUnit, nhật ký hoặc thư mục runs tích lũy.

Nếu chỉ kiểm tra phần CPU, dùng `--scope cpu`; kết quả này không nghiệm thu Docker/CUDA. Chỉ ghi nhận hoàn thành bản hiện tại khi `phase0_complete=true` và mọi gate `passed`. Bản trước khi dọn đã được host nghiệm thu; bản sau khi dọn cần chạy lại full check trên terminal WSL có quyền Docker/GPU.

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

## Cấu trúc tối thiểu

- `src/`: contracts, đọc split, mock runtime và evaluator.
- `scripts/check_phase0.py`: verifier duy nhất; `build_context.py` và `probe_environment.py` là hai helper Docker/phần cứng.
- `tests/`: ba nhóm test output contracts, scorer và runtime; dùng dữ liệu synthetic.
- `README.md`, `roadmap.md`: hướng dẫn và kế hoạch; không duy trì ADR/nhật ký/config mô tả trùng lặp.
- `reports/phase0.json`: kết quả verify gần nhất; `.venv/`, `.artifacts/`, `dataset/` được ignore.

Rulebook/README/calendar đúng split là nguồn ngữ nghĩa runtime. Inference không đọc nhãn/EDA, không kết nối mạng và không sửa input. Scorer nội bộ chưa được đối chiếu hoàn toàn với evaluator BTC; các giới hạn model/phần cứng/nộp bài cần đối chiếu theo mục 2.1 và Phụ lục B của roadmap. Model candidates ở Phụ lục G là hướng nghiên cứu, chưa có model active hoặc weights được tải.
