# DataMate submission — một pipeline

Chạy trên Linux/WSL Bash từ repository root. Prediction luôn dùng **root predict.sh**,
trên host hoặc bản copy `/code/predict.sh` trong image. `scripts.submission` chỉ điều phối
build/validation/export/reload, không có implementation inference riêng. Setup trong
[README](../README.md); tiến độ và evidence theo revision trong `roadmap.md`.

## Requirements và nguồn

Đã đối chiếu ngày 06/11/2026:

- **D-R:** [Portal DataMate → Rule](https://challenge.zalo.ai/portal/datamate), public API
  `updated_time=1793767860` (04/11/2026).
- **D-G:** cùng portal → Submission Guidance, `updated_time=1793853700` (05/11/2026),
  đã bổ sung Docker guide riêng cho DataMate.
- **D-D:** [Docker guide DataMate, 10 trang](https://dl-challenge.zalo.ai/2026/datamate_dkewxftdzo/zac2026_DataMate_docker_guideline_final.pdf),
  840.677 bytes, SHA-256 `c27d52ef1ab2c5490d86ae4b371a970151740feefa626367f4cedb07c1c9cb49`.
  Đã tải và đọc trực tiếp; không phải PDF LifeMate.
- **D-I:** rulebook/calendar/README và question contract của đúng split được cấp.
- **L:** nội dung LifeMate do người dùng cung cấp; không có PDF LifeMate trong workspace
  khi audit. Các quy trình ZAC dùng chung chỉ áp cho DataMate khi D-R/D-G/D-D xác nhận.
- **E:** engineering recommendation, không tự trở thành luật BTC.

`IMPLEMENTED` chỉ nói code có mặt. `VERIFIED` cần gate chạy đạt trên đúng source/image/input;
CPU tests hoặc report lịch sử không chứng nhận Docker/GPU hiện tại.

| Requirement | Source | Implementation / evidence | Gap / action |
|---|---|---|---|
| ≤15B tổng mọi model, cả MoE/model phụ; weights public trước26/10/2026, license phù hợp | D-R §2; D-D §2/8 | Một pretrained Qwen1.5B, config và license trong image | Human review mọi model cuối, không suy số tham số từ bytes quantized |
| Offline, mọi inference weights/tokenizer/config có sẵn | D-R §3; D-D §1/4 | LocalModel, locked closure, inventory hash; smoke network none | Image mới phải test thật; không download trong predict |
| /data read-only; output đúng DataMate JSONL | D-R §5; D-D §3/5; D-I | load_split/readers, existing strict Decimal/types validator, streaming output | Smoke kiểm IDs/count/order/logs; không thêm trường time của LifeMate |
| Không đọc labels, không hardcode IDs, không train/pseudo-label test | D-R §4/6 | Input staging theo allowlist, evaluator command riêng | Source review + tests; không mount answers |
| /code/predict.sh no-args và /code/README.md | D-D §4/5/8 | Docker COPY **root script**; defaults chỉ tại CLI/environment | Actual container gate sau rebuild; không còn adapter script |
| Logs id/answer/elapsed_ms/thinking; actual code/inputs/output | D-D §3.3 | runtime logs; Qwen không reasoning channel → null, raw calls giữ nguyên | Validation và model smoke thật |
| Linux/amd64; 2×RTX5090 32GB; RAM512GB; CUDA≥12.8/SM12.0 | D-R §7; D-D §2/9/10 | Inspect platform; selector GPU, shm16g, no privileged/socket | Local cu124/GTX1650 **chưa chứng nhận Blackwell**; runtime compatible là gate riêng |
| Toàn private≤6 giờ gồm initialization/loading/preparation | D-R §7; D-D §7/10 | Wall/model/preparation/business/question/generation/execution timing | Full cold reference-host run, không coi smoke estimate là SLA |
| PA(uploaded) vs PA(re-run) chênh tuyệt đối≤0.03 | D-R §8; D-D §6 | Fixed seed0/temperature0; hai cold repeats + byte-identical predictions | Actual same-image repeats/reload; không hứa mọi GPU bitwise deterministic |
| README, actual training/data/evidence và manifest trong image | D-D §1/4/8/11 | README root, config, artifact manifest; training README/data manifest được sinh từ declaration | Không team training/generation, samples0/0; không fake train.sh. Trained successor bị từ chối tới khi có actual asset allowlist |
| Một image/tag, docker save tar.gz, tar không nén≤100GB | D-D §2/10/11; shared ZAC corroborated | Export streaming, CRC, actual tar byte count, check manifest/config và mọi layer | Actual export/reload chưa chạy trong cleanup; không extract archive lên host |
| MD5 file, link và tar byte size; Google Drive public view | D-D §11; D-G | MD5/SHA256 sidecars + submission manifest, manual guide dưới | Không upload tự động; download/check trên máy khác |
| Notebook/JupyterLab/start_jupyter/time field, CUDA13.2/1GPU/RAM64GB/audio/tools | **L only** | Không áp cho DataMate guide hiện tại | Chỉ bổ sung nếu BTC có addendum xác nhận; không đổi output/model vì LifeMate |
| SHA256, fresh outputs, cold caches, layer inspection, dry-run | **E** | Implemented + synthetic tests | Không coi synthetic archive là actual Docker proof |

Mốc 01/10/2026 trong giả định ban đầu đã được thay bằng **26/10/2026** theo D-R/D-D.
`NEEDS_OFFICIAL_CONFIRMATION`: addenda mới hơn PDF đã đọc, private split authority khi
thay đổi, scorer BTC tương đương local (aliases/list/table), kênh gửi tar size nếu form
không có ô riêng. Kiểm portal Data sau đăng nhập; không suy từ API `Authen Failed`.

## Model, training và image contents

[config/submission_model.json](../config/submission_model.json) là model declaration:
public source/revision/release/license/parameter budget/hash/byte size. Qwen GGUF được
sử dụng trực tiếp, không có fine-tuning/training-data generation, counts0/0; weights phát
hành 18/09/2024 đáp ứng cutoff. [License](model-license.txt) được copy vào `/opt/model/LICENSE`.

Image chứa source `/code/src`, **root predict.sh** tại `/code/predict.sh`, README,
pyproject/uv.lock, model/runtime dưới `/opt`, config và `/code/artifact_manifest.json`.
Metadata training no-training được sinh tại `/code/training_code/`; không có training
script/data/log giả. Chỉ eligible open-source training-only checkpoints được phép dùng
external reference; mọi inference asset vẫn phải trong image. Với successor đã train,
đóng gói code/data/prompts/responses/config/seed/deps/logs/checkpoints thật và local
path/size/SHA256 manifest trước khi thay no-training declaration.

Không copy dataset/labels/EDA reports/cache/tests/Git vào build context. Builder dùng
allowlist; layer checker phát hiện cả files đã bị whiteout trong parent. Source refresh
xóa `/app` và source stale trong filesystem mới, giữ model/runtime assets. Default UID0
cho bind-mounted result của command BTC; local verifier override UID/GID host và drop caps.
Không dùng privileged mode, host socket/services hoặc host credentials.

## Prepare và smoke

Dùng `.venv`/parent model image đã có. Không sync dependencies hoặc tải weights trong
inference. Parent CUDA12.4 hiện có không chứng nhận RTX5090; giữ nguyên trong cleanup,
chuẩn bị/test compatible runtime ở công việc riêng. Host cần daemon, input, disk cho
staging/caches/results/image/archive; không tự đổi driver hoặc Docker system config.

```bash
DATAMATE_IMAGE=zaic2026_datamate_team:review1
DATAMATE_PACKAGE="$PWD/.artifacts/submission-review-new"

# Dry-run: không gọi Docker, không tạo outputs.
uv run --no-sync python -m scripts.submission prepare \
  --base-image datamate-baseline:local --image "$DATAMATE_IMAGE" \
  --input dataset/train --output "$DATAMATE_PACKAGE" \
  --mode smoke --limit 3 --gpus none --timeout 1800

# Cùng options + --execute để build, hai cold runs, export/load và một cold re-run.
uv run --no-sync python -m scripts.submission prepare \
  --base-image datamate-baseline:local --image "$DATAMATE_IMAGE" \
  --input dataset/train --output "$DATAMATE_PACKAGE" \
  --mode smoke --limit 3 --gpus none --timeout 1800 --execute
```

Prepare dùng Dockerfile target `submission`, local parent được inspect trước/sau build,
`--platform linux/amd64 --network=none --pull=false`. Weights không bị copy lại khi source
refresh. Final image và metadata được kiểm bằng hash rồi chạy chính `/code/predict.sh`
no-args. Host command và container command đều đi vào cùng CLI/LocalPlanner/runtime.

Smoke stage đúng inference inputs, bỏ labels; chọn N câu đầu theo thứ tự input.
`--mode full` chạy toàn split, không đọc answers. `--gpus none` CPU diagnostic;
`--gpus 'device=0,1'` trên host có GPU/runtime phù hợp. Input bind read-only, network none,
rootfs read-only, shm16g, writable result/cache/tmp; mỗi repeat có cold cache riêng.
Timeout mặc định21600s mỗi run, build timeout3600s; `--build-timeout`, `--cache-dir` (root
mới), `--temp-dir` (scratch root có sẵn), `--tmpfs-size` cấu hình riêng. Không hardcode
đường dẫn host. Chọn path/tag mới khi chạy lại; không overwrite/xóa/prune/upload tự động.
Full prepare chạy ba lượt nên có thể tốn nhiều lần budget6h; không tự chạy mặc định.

Chỉ kiểm image đã build, chưa export:

```bash
uv run --no-sync python -m scripts.submission smoke \
  --image "$DATAMATE_IMAGE" --input dataset/train \
  --output .artifacts/smoke-new --mode smoke --limit 3 --gpus none --execute
```

CPU/CUDA `llm`/`cuda` stages chỉ bootstrap parent assets, không là pipeline inference
khác. Nếu chưa có CPU parent, với GGUF và locked installed LLM closure đã có:

```bash
uv run --no-sync python -m scripts.build_context \
  --model .artifacts/models/qwen2.5-1.5b-instruct/model.gguf \
  --output .artifacts/cpu-context-new.tar --manifest .artifacts/cpu-context-new.json
docker build --platform linux/amd64 --network=none --pull=false \
  --target llm -t datamate-cpu-parent:review1 - < .artifacts/cpu-context-new.tar
```

Builder cũ cần path mới, base Python pin local và installed closure đúng lock/RECORD;
không download. GPU parent dùng `--cuda-runtime` với artifact compatible đã staging,
Docker target `cuda` và `CPU_IMAGE` trỏ CPU parent mới. Không dùng cu124 làm Blackwell proof.

## Validate, timing và reproducibility

- Existing strict validator kiểm typed JSON, IDs/order/count, log answer/identity,
  mandatory log fields, manifest complete/errors0, adapter local-planner và model identity.
  Missing weights không được biến thành một output mock. Smoke subset không phải full PA.
- `before_export/run-0`, `run-1` giữ submission/logs/catalogs/facts, stdout/stderr,
  model/preparation/business/generation/execution timing và manifest. `container.json`
  ghi wall/exit/timeout; `resources.jsonl` là samples, không phải peak bảo đảm.
- Wall time gồm Docker/Python/input validation/loading/preparation/output/fsync.
  Generation là một phần riêng, không đồng nghĩa end-to-end. Model startup gồm hash;
  Decimal timing nằm trong Python timing, không cộng hai số như phase rời nhau.
- Estimate `startup + avg question elapsed ×500` chỉ tham khảo; cold prep/business mix
  có thể chi phối. Docker startup riêng/GPU peak thiếu đo phải ghi NOT_MEASURED.
- Hai cold repeats so exact prediction bytes và source/input/model/config identity.
  Metadata lưu runtime versions, Git commit **và dirty flag**, source snapshot/model hash.
  Không dùng commit hash một mình để mô tả source chưa commit.
- `scripts.submission validate --input SPLIT --output RUN` kiểm cả split được đưa vào.
  Auto smoke staging đã cleanup thì xem verification.json, không validate subset bằng full split.
- Evaluator train chạy riêng theo README sau inference; không evaluate labels public/private.

## Export và verify

Prepare export đúng một tested tag bằng docker save, gzip; lỗi save không được che bởi
compression. `.tar.gz.md5`/`.sha256` dùng tên archive cố định. Manifest ghi UTC, image/tag/ID,
model/dependency/source/config, bytes và **uncompressed_tar_bytes**. Đọc hết gzip để kiểm
CRC/size, kiểm config và mọi layer trước load. Không đổi tên archive sau khi tạo MD5.

Prepare giữ export record `submission_manifest.json` bất biến ở trạng thái reload pending;
`after_reload/verification.json` chỉ pass khi load/hash inventory/run/compare đều đạt.
Same-daemon load không chứng minh clean host; report ghi NOT_VERIFIED cho clean daemon.

Copy toàn package và source checkout có tools tới host khác; chuẩn bị `.venv` dev tools
trên checkout mới bằng `uv sync --locked --python /usr/bin/python3.11 --no-default-groups --group dev`
trước bước offline. Không cần weights hoặc development cache ngoài image.

```bash
cd /path/to/package
md5sum -c image.tar.gz.md5
sha256sum -c image.tar.gz.sha256
cd /path/to/Zalo-AI-Challenge
uv run --no-sync python -m scripts.submission verify \
  --package /path/to/package --input /path/to/original/input-split \
  --output .artifacts/reload-new --mode smoke --limit 3 --gpus none --timeout 1800
```

Thêm `--execute` để load/re-run; mode/limit/GPU phải như prepare. Package/archive hashes
sai hoặc tag đang trỏ image khác thì từ chối. Không xóa image của người dùng để giả môi
trường sạch. Lỗi giữ logs/partial artifacts, không chỉnh manifest/checksum để ép pass.

## Checklist và manual submission

| Gate | Trạng thái / evidence cần có |
|---|---|
| Model eligibility/budget/licenses và no-training declaration đúng | Review config/upstream/source rights; declaration không phải proof |
| Một predict.sh, model bắt buộc, numeric semantics/label isolation/recovery không đổi | CPU regression tests + CLI defaults/missing-weights tests |
| Actual build linux/amd64, correct source/assets/no-args | build.log + image_inventory.json; NOT_TESTED nếu chưa build |
| Read-only input/network none/cold prep/logs/contract | actual smoke/full verification, exit0/errors0 |
| RTX5090-compatible runtime và ≤6h full private | Reference-host cold benchmark, NOT_TESTED ở máy local |
| Repeat/reload same predictions và identity | before_export + after_reload proofs, clean-host verify riêng |
| Tar≤100GB, MD5/SHA256 và download integrity | manifest + sidecars + checksum check trên máy khác |
| External upload/portal/freeze | MANUAL_PENDING; không được task hiện tại thực hiện |

Theo D-G đã đọc: solution deadline16/11/2026 23:59 GMT+7, tối đa2 solution submissions,
BTC chỉ xét lần cuối; public JSONL quota5/ngày; private phase20/11 09:00–23/11 23:59.
Kiểm lại portal trước nộp, không coi lịch snapshot là bất biến.

1. Sau toàn bộ gates và người dùng review final, upload `image.tar.gz` lên Drive;
   giữ/gửi MD5 file/line và tar không nén size bằng bytes theo D-D §11.
2. Share → Anyone with the link → Viewer → Copy link. Kiểm anonymous download toàn file
   trên máy khác, MD5/SHA256 rồi clean-host verify. Mở preview chưa đủ chứng minh tải được.
3. Portal DataMate → **Solution Submission**: Google URL, MD5, mô tả; gửi tar size theo
   form/kênh BTC xác nhận, không đoán endpoint. **Submission** JSONL là luồng khác.
4. Người dùng tự submit/freeze theo deadline. Giữ receipt, archive/sidecars/manifests,
   source context, licenses/config/lock, logs/repeats/reload proof và actual training
   evidence nếu có. Task này không upload/submit/commit/push hoặc freeze final solution.
