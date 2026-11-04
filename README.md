# Zalo AI Challenge — DataMate

Runtime đọc CSV/XLSX/SQLite, chuẩn bị context một lần/business và ghi JSONL/log theo câu. Mặc định vẫn **mock abstain**; `--model` bật local planner S1/S2, QueryPlan validator, resolve/preflight và executor xác định. S2 gồm difference/growth, tỉ lệ huỷ, giá trị trung bình đơn, trung bình ngày/thứ, tỉ trọng nhóm/kênh và ca bán hàng có authority phù hợp. Tiền tính bằng Python Decimal trên SQLite read-only, rồi verifier và output validator kiểm tra trước khi ghi đáp án. Coverage dùng khoảng ngày của toàn export khi đọc/parse đầy đủ, ưu tiên khoảng đầy đủ được README khai báo và chặn mâu thuẫn. Giới hạn và bằng chứng theo revision nằm trong [roadmap.md](roadmap.md), [JSON kết quả](reports/stage_results.json); kết quả dev không chứng nhận PA toàn dataset. Giữ lookup inventory snapshot hẹp từ M1.

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

Output của lần chạy chủ động gồm `submission.jsonl`, `logs/`, một catalog mỗi business trong `catalogs/`, `run_manifest.json` và `prepared/<business>/facts.sqlite` cho nguồn được hỗ trợ. Context được chuẩn bị một lần/business và không cắt theo ngày của câu hỏi. Các file này được giữ để người dùng xem; chỉ output do verifier tạo mới tự xóa. Thêm `--cache-dir .artifacts/source-cache` để reuse catalog và prepared facts giữa các lượt; mặc định không sinh cache đĩa. Catalog nguồn giữ riêng metadata `prepared`; `BusinessContext.prepared` cho planner truy cập facts read-only.

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

## Chuẩn hóa dữ liệu

Dùng entrypoint hiện có với output mới, không mở labels hoặc chạy LLM:

```bash
uv run --no-sync python -m src.cli prepare \
  --data-dir dataset/train --business biz_101 \
  --output-dir .artifacts/prepared-inspection
```

Lệnh ghi `<business>.json` gồm catalog, summary chuẩn hóa và metadata `semantics`. Nguồn phù hợp có thêm
`<business>/facts.sqlite`: `orders`, `order_lines`, `lineage`, `issues`, `metadata`.
Mapping dựa vào mô tả cột/grain trong README và rulebook runtime, không dựa business ID.
Các mapper hiện có xử lý CSV order-line; header-only count và revenue khi có cột tiền hàng đúng nghĩa được README xác nhận; XLSX nhiều sheet/phần có authority tiếp nối; CSV/XLSX/SQLite header/detail/master có khóa kiểm tra được; sổ bán không mã đơn theo grain bản ghi. Không dựng order giả cho sổ hoặc line giả cho header. JOIN không đưa payment vào phép cộng doanh thu. Nguồn mơ hồ hoặc representation chồng lấn chưa đối soát ghi `unsupported`, không tạo facts rỗng. Bằng chứng native cho từng họ nguồn phải đọc theo revision trong report. Một XLSX inventory snapshot bốn cột vẫn giữ giới hạn M1 (xem cuối README).
Summary `ready` chỉ xác nhận preparation không bị blocking issue; không chứng minh đủ coverage
hoặc metric đã có executor. `blocked` giữ lỗi/facts để kiểm tra và chặn metric phụ thuộc;
CLI `prepare` trả exit 1 khi có blocking issues.

Tiền lưu TEXT Decimal, giữ phần lẻ nguồn; đọc qua `PreparedBusiness.iter_records()` trả Decimal.
Không dùng SQLite `SUM/AVG/CAST REAL` trực tiếp cho tiền; phép tính chính xác dùng Python Decimal.
Theo quyết định người dùng ngày **04/11/2026**, khi không có locale/README phân cách tiền,
chấp nhận dấu nhóm nghìn nếu toàn cột tiền phù hợp quy ước nhóm 3 chữ số. Metadata ghi
`money_basis=user_2026_11_04_consistent_three_digit_groups`, các source áp dụng và cảnh báo
đây là quy ước export được chấp nhận, không phải locale được chứng minh. README tường minh
ưu tiên; format trộn, giá trị lỗi hoặc mâu thuẫn vẫn chặn metric phụ thuộc. Scalar parser khi
không có convention vẫn từ chối token mơ hồ; policy chỉ được xác lập ở bước đọc cột nguồn.

Theo quyết định cùng ngày, JOIN nhóm hàng qua tên chuẩn hóa hoa/thường, khoảng trắng và
dấu câu. Tên dòng bán mất dấu chỉ được nối khi khớp **duy nhất một mã** trong danh mục.
Tên có dấu khác không tự bị bỏ dấu để ép khớp; nhiều mã ứng viên hoặc nhóm mâu thuẫn
vẫn chặn tỉ trọng. Metadata ghi `name_matching_basis=user_2026_11_04_unaccented_unique_catalog_code`,
số dòng dùng policy và cảnh báo. Dòng đã có mã chỉ nối theo mã, không fallback qua tên.
Ngày chỉ có ngày giữ ISO date, timestamp giữ giờ/offset. Giữ mọi status và ngày sau câu hỏi;
chỉ query-time mới áp policy metric. Summary/tổng nguồn không tạo giao dịch; exact dedup trong
cùng đơn giữ lineage; trả theo SL âm chỉ xử lý khi README và rulebook xác nhận.
Dòng tổng tháng chỉ ghi “Cộng”/“Tổng”, thiếu identity và quantity, được nhận diện trước
kế thừa header; không gắn tiền tổng vào đơn cuối sheet. Dòng bán thật có tên như vậy
vẫn giữ theo identity/quantity nguồn. Filter kênh áp dụng cả header revenue lẫn line
revenue; thiếu kênh của bản ghi liên quan giữ abstain, không coi là không có doanh thu.

Cache facts nằm trong `<cache-dir>/prepared/<fingerprint>/`, gồm database và manifest checksum.
Metadata trong database chứa schema/software/source/authority versions, mapping, coverage và
quality summary; không tạo thêm file audit/quality theo từng dòng. Coverage theo chính sách dữ liệu phát hành bên dưới; export rỗng hoặc parse chưa đủ không được tự tạo phạm vi. Sổ không mã đơn ghi `observed_sales_record_dates`, không biến ngày dòng bán thành bằng chứng có order identity. Lịch sử return cho kho, quantity/unit/cost và nghiệp vụ ngành chưa được bật.

Kiểm tra bằng `uv run --no-sync python -m pytest tests/test_normalization.py`.
Bằng chứng đối chiếu raw độc lập, hai cold runs và warm cache trên nguồn dev S1 nằm tại
[reports/normalization_verification.json](reports/normalization_verification.json).
Preparation không tự cấp quyền trả số; đường local planner phải qua QueryPlan validator và preflight theo từng câu.

## Ngữ nghĩa và thời gian

`BusinessContext.rules` và `.calendar` dùng authority đúng split; `.semantics` được tạo
sau preparation trong `predict`/`prepare`, ghi cùng catalog hiện có. Không có script,
entrypoint hoặc file context bổ sung. Rule profile giữ hash/line/section/đoạn nguồn;
luật thiếu, mâu thuẫn hoặc chưa nhận diện trong cấu trúc được hỗ trợ không dùng ngầm mặc định train. Parser có aliases hữu hạn và guard cho một số ngoại lệ/định nghĩa trái chiều ở paragraph/section; không chứng nhận đã hiểu mọi ngoại lệ trong tài liệu.

API `metric_contract(rules, metric_id)` cung cấp contract revenue/count.
`resolve_window(expression, as_of_date, rules, calendar)` nhận biểu thức có cấu trúc,
không tự hiểu câu tiếng Việt. Hỗ trợ day/range, today/yesterday, current/previous week,
month/current_month/previous_month/previous_mtd, last_n_days, quarter/year/year_to_date
và tet/before_tet theo calendar. Kỳ tường minh giữ nguyên; chỉ cắt theo as-of khi biểu thức yêu cầu.

`preflight(context, metric_id, expression, as_of_date=...)` trả `eligible_to_execute`,
`unanswerable` hoặc `abstain`, cùng contract, requested/effective windows và evidence.
Eligibility chưa phải đáp án. Executor chỉ bật với validated plan; entity filters chưa hỗ trợ.
S2 mở hẹp difference hai kỳ như phần local planner dưới đây.
Coverage theo quyết định người dùng ngày 03/11: dùng khoảng min/max ngày của **mọi đơn**
đã parse đầy đủ trong export, kể cả đơn huỷ/trả/đang giao; không tính min/max trên riêng kỳ hỏi
hoặc chỉ các đơn được cộng. README khai báo khoảng đầy đủ thì ưu tiên khoảng đó.
`biz_101` có phạm vi 01/06/2025–24/08/2026; metadata ghi `observed_order_dates` và cảnh báo
không phát hiện được bản ghi thiếu bên trong export. Đây là chính sách dùng dữ liệu phát hành,
không phải chứng nhận completeness từ bên ngoài.

- Ngày không có giao dịch **bên trong** phạm vi: trả 0 khi các gate còn lại đạt.
- Kỳ giao một phần phạm vi: tính phần giao theo rulebook; hoàn toàn bên ngoài: unanswerable.
- Export rỗng chưa có khai báo đầy đủ: unknown; ngày lỗi/đọc thiếu/mâu thuẫn README: abstain.
- Không suy coverage từ calendar; snapshot vẫn chỉ có authority tại đúng mốc riêng.

Kiểm tra bằng `uv run --no-sync python -m pytest tests/test_semantics.py tests/test_temporal.py`.
Bằng chứng dev/cold/warm nằm trong [reports/semantics_verification.json](reports/semantics_verification.json).

## Local planner S1 và S2

Cài dependency ở bước setup (cần C/C++ compiler và CMake nếu build từ source), trước inference:

```bash
uv sync --locked --group llm --python /usr/bin/python3.11
```

Thử nghiệm local dùng [Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF),
Q4_K_M, Apache-2.0; weights revision `91cad51170dc346986eccefdc2dd33a9da36ead9`, file
`qwen2.5-1.5b-instruct-q4_k_m.gguf`, 1.117.320.736 bytes,
SHA-256 `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`.
Tải weights riêng ở bước setup vào thư mục ignored ngoài dataset; inference chỉ nhận file có sẵn.
Đây là model thử wiring trên máy local, chưa chốt model nộp thi hoặc chứng nhận quy định BTC.

```bash
./predict.sh --data-dir dataset/train --output-dir .artifacts/local-s1-run \
  --model .artifacts/models/qwen2.5-1.5b-instruct/model.gguf \
  --question-id q_0cb7c9b93048 --question-id q_ed4fe0895229 --question-timeout 120
```

`--question-id` lặp lại để chọn subset rõ ràng, giữ thứ tự input; bỏ option để chạy cả split.
Khi đánh giá subset, chỉ evaluator riêng được tạo/đọc nhãn của subset theo protocol đã khóa;
không coi PA subset là PA toàn train. Lệnh mẫu chọn câu thuộc dev của cả hai protocol.

Runtime `llama-cpp-python==0.3.16` dùng CPU, ChatML, seed 0, temperature 0,
context 4096, batch 128, decode/batch threads mặc định cùng bằng 4, tối đa 256 output tokens
cho S1 và 384 cho draft nhiều aggregates (cùng refs cần JSON dài hơn).
Worker giữ model một lần/run; load timeout riêng,
question timeout chung cho tối đa hai attempts, quá hạn dừng worker và abstain.
Retry sửa JSON lỗi; plan parse được nhưng có intent/scope chưa hỗ trợ giữ abstain.
Nếu worker đã dừng, các câu sau abstain; không tải hoặc tự reload model ngầm.

`src/agent/plan.py` giữ schema draft của **cùng QueryPlan theo Phụ lục E**.
Parser hữu hạn ràng buộc metric/time/filter/refs trong grammar khi cue rõ; LLM sinh
draft trong grammar đó. Server gắn identity, authority, requirements, units/policy,
rule refs và resolved/effective windows. S1 giới hạn một aggregate/một window;
S2 dùng tối đa ba windows/aggregations. Một phép derived dùng hai aggregate và
`current_ref`/`baseline_ref`; không có derived chain hay DAG tổng quát. Hai hoặc ba số
độc lập dùng output `list`, `result_refs` giữ thứ tự câu hỏi. Chuỗi ba tháng giữ thứ tự
tháng cũ tới mới; hai kênh điện thoại/tại quầy giữ thứ tự được hỏi, không sort theo giá trị.
Output nhiều số cần clause `ordered_results` được xác nhận trong rulebook runtime.

| Operator | Dependencies và output |
|---|---|
| `difference` | Hai kỳ, cùng revenue hoặc order_count; kỳ hỏi − kỳ so; number có dấu |
| `percent_change` | Hai kỳ, cùng revenue hoặc order_count; (kỳ hỏi − kỳ so)/kỳ so × 100; percent |
| `cancellation_rate` | Một kỳ; cancelled_order_count/placed_order_count mọi trạng thái × 100; percent |
| `average_order_value` | Một kỳ; revenue chưa làm tròn/order_count hợp lệ; number VND, half-up |
| `average_daily` | Một kỳ; revenue hoặc order_count/calendar_day_count; number; weekday=0…6 áp cả hai |
| `share` | Một kỳ; revenue nhóm/revenue toàn shop × 100; percent; filter chỉ ở tử số |

Mẫu số phải dương; zero/âm/undefined chưa có authority thì abstain. Trung bình ngày đếm
mọi ngày thuộc effective window, kể cả không có đơn. Tỉ trọng chỉ bật khi README xác nhận
mapping customer_group kèm customer hoặc item_group; không fuzzy merge các nhóm có tên
mơ hồ, mẫu số giữ nhóm khác và khách vãng lai. Có checked header/detail/master joins
N:1 cho các schema đã nhận diện; orphan/conflicting keys chặn metric phụ thuộc, không
JOIN payments vào sales để cộng tiền. Product/customer ranking vẫn ngoài scope.
Identity, SQL, policy hay numeric answer do model tự thêm bị từ chối.
Context chỉ lấy metadata schema/contracts/coverage/rule refs/warnings, không đưa raw rows vào prompt.
Authority hashes được chia sẻ thay vì lặp theo từng rule; metadata business đứng trước
`as_of_date`/question để llama.cpp tái dùng token prefix, không cache đáp án. Logs ghi
tách thời gian prompt/decode và số prompt tokens thực sự được tính lại.

Executor dùng fixed read-only SELECT, kiểm tra grain/status/mapping/line completeness và
Decimal hữu hạn, giữ số chính xác tới bước rounding. Half tie chưa có luật được xác minh
vẫn abstain. Zero chỉ có nghĩa khi coverage đủ; ngoài coverage đã chứng minh mới unanswerable.
Validator kiểm tra contract của plan, chưa chứng minh model luôn hiểu đúng mọi câu tiếng Việt;
phải kiểm tra lỗi bỏ filter/phủ định trước khi mở phạm vi.

Difference cần rule runtime xác nhận **kỳ hỏi − kỳ so**, giữ dấu và đơn vị. Validator
kiểm tra IDs/refs/scope/metric/unit và hai requested windows trước coverage clipping.
Mỗi window có preflight, requested/effective bounds, coverage status và basis riêng;
unknown/quality issue khiến abstain, kỳ ngoài nguồn khiến unanswerable khi không có
dependency chưa rõ. Partial lấy giao theo luật, không thay kỳ thiếu bằng 0.
Executor dùng các tổng Decimal chưa làm tròn, trừ chính xác, kiểm tra recomposition
và làm tròn một lần ở output; count difference được phép âm. Half tie chưa rõ vẫn abstain.

Guard S2 hữu hạn nhận “kỳ hỏi … so với kỳ so … chênh lệch/hơn/kém/tăng/giảm” hoặc
“So với kỳ so, kỳ hỏi …”, mỗi vế có một ngày/range/tháng/quý tường minh hoặc cue hôm nay/qua, tuần này/trước,
tháng này/trước. “Tháng này so với tháng trước” dùng `current_month` và `previous_mtd` theo
runtime rule, gồm trường hợp tháng trước ngắn hơn. Tháng tường minh so với “tháng trước”
được anchor tháng trước của kỳ hỏi, gồm rollover tháng 1. Ca sáng/chiều dùng clock nguồn
và khoảng nửa mở từ rulebook; không suy clock từ ngày. Tổng/so sánh ca, tổng nhóm và
tỉ trọng nhóm/kênh chỉ chạy khi mapping/authority tương ứng đủ. Entity/ranking rộng chưa bật.
Không coi các guard này là parser tiếng Việt tổng quát.

Kết quả các giai đoạn dùng chung [reports/stage_results.json](reports/stage_results.json).
`stages.S1`, `stages.S2` có cùng cấu trúc; khi có S3–S6 chỉ thêm một stage cùng mẫu.
Mỗi stage có `primary_evaluation`, `summaries` và `questions`. Mỗi câu lưu ID/business,
nguyên văn câu hỏi, ngày tham chiếu, loại case, `expected_answer` và `results` gồm
`actual_answer`, `verdict`, `points` theo lượt đánh giá. `evaluations` mô tả ngắn loại
lượt chạy và số repetitions; các lượt có predictions giống nhau không lặp từng câu.
Native/synthetic chấm riêng, diagnostic/scenario chỉ kiểm tra hành vi. Không lưu
drafts, SQL, logs, hashes hoặc runtime metadata trong file kết quả.

**Baseline S1/S2 đã kiểm chứng trong scope hữu hạn — 04/11/2026:**
17 common-dev businesses thuộc 10 họ nguồn, preparation ready và 0 issues; source cuối
đạt **411 tests**, lint/format và lockfile offline. S1: **51 correct / 0 wrong / 8 abstain**
trên 59 câu; S2: **115 correct / 0 wrong / 0 abstain** trên 115 câu. Đây là replay nguyên
draft của hai lượt LLM đầy đủ trước sửa, trên facts/executor cuối; đã đo context của
192 câu/case giống nhau từng byte và source planner không đổi. **Không gọi replay là
một lượt LLM mới trên đủ 174 câu.**

Hai fresh CUDA workers trên source cuối, mỗi lượt **47 câu = 44 native + 3 header fixtures**,
cho predictions giống nhau: native **41 correct / 0 wrong / 3 abstain**, cả 3 fixtures đúng
và fresh44 khớp replay. Giữ toàn bộ 27 câu biz_105, cả câu đúng/sai/unsupported; không chỉ
chọn các câu sai. 17 synthetic cũ đạt behavior, 15 case có gold được chấm riêng; ledger
không mã đơn giữ unanswerable có căn cứ. Không cộng repetitions/fixtures vào mẫu số native.

Lịch sử trước sửa giữ nguyên **153 correct / 13 wrong / 8 abstain** trên 174 câu;
13 wrong thuộc lỗi subtotal sổ tay; JSON giữ từng câu trước/sau sửa, source revision giữ trong evidence local.
CPU smoke cuối: **3 correct / 0 wrong / 0 abstain** trên 3 câu, budget vẫn 120 s; chưa chứng nhận CPU toàn S1/S2.
Inference offline, input/cache/root read-only; evaluator host chỉ đọc gold sau các workers.
Kết quả là dev engineering, không chứng nhận mọi schema/paraphrase, blind holdout,
S3–S5, scorer BTC hay hardware nộp thi.

JSON hiện tại: [kết quả các giai đoạn](reports/stage_results.json).
Evidence kỹ thuật chi tiết giữ local tại `.artifacts/s1-s2-full/verification-evidence.json`,
section `s1_s2_final_baseline`; không thuộc file kết quả. Checkpoint gốc là `0421d68`;
các checkpoint phía dưới là lịch sử.

Tái chạy batch 47 câu đã kiểm chứng bằng image/cache local hiện có (input staging
không chứa nhãn). Chạy từ repo root và dùng output mới; mount `/train` giữ cùng
namespace với cache đã chuẩn bị. Không truyền `--model` ở entrypoint sẽ chạy mock.

```bash
mkdir .artifacts/baseline-s1-s2-run
docker run --rm --gpus all --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --mount "type=bind,src=$(pwd)/.artifacts/s1-s2-full/final-scope/input/train,dst=/train,readonly" \
  --mount "type=bind,src=$(pwd)/.artifacts/s1-s2-full/cache-final,dst=/cache,readonly" \
  --mount "type=bind,src=$(pwd)/.artifacts/baseline-s1-s2-run,dst=/result" \
  datamate-s1-s2-complete-cuda:local \
  --data-dir /train --output-dir /result --cache-dir /cache \
  --model /opt/model/model.gguf --gpu-layers -1 --question-timeout 120
```

**Checkpoint S2 operator 0421d68 — 03/11/2026, đã commit/push:** 366 tests, lint/format,
lockfile offline và fixed-plan wiring 17/17 đạt. Hai fresh CUDA workers trên cùng source/image
chạy 52 câu/lượt: **S1 12 correct / 0 wrong / 1 abstain** (13 câu), **S2 native
22 correct / 0 wrong / 0 abstain** (8 growth + 7 AOV + 7 cancellation), **17/17 synthetic
đúng behavior**; 15 fixture có gold xác định đạt 15 correct/0 wrong/0 abstain, hai zero-denominator
diagnostics không tạo gold. Predictions 52/52 khớp giữa hai lượt. Gold chỉ evaluator host đọc
sau khi cả hai containers thoát; source/model/raw/staged hashes và isolation đạt.

Checkpoint này đủ bộ operator trong scope nguồn S1 khi đó đã bật: signed difference, revenue/order_count growth,
cancellation ratio, AOV, daily/weekday average và group share có mapping rõ. Tỉ trọng và trung
bình ngày/thứ mới có nghiệm thu synthetic; native dev phù hợp trên source hiện tại chỉ kiểm
chứng growth/AOV/cancellation. **Không phải hoàn tất S2 mọi source family**: native share đang
cần master/group mappings và thường raw JOIN trên các family chưa bật. Header-only revenue,
multi-sheet, raw JOIN và toàn S1 mọi shape còn mở ở revision 0421d68. Không chứng nhận CPU LLM source mới,
Blackwell, official scorer equivalence hoặc PA đại diện.

Schema tuple bind metric/filter/ID/ref theo vai trò operand **trước generation**, dựa vào finite
question cues; validator tổng quát vẫn kiểm tra plan độc lập, không sửa semantic plan sau sinh.
Một lượt diagnostic trước binding đã bị chặn vì duplicate IDs/sai operand; giữ trong
`pre_binding_diagnostic`, không dùng làm acceptance. Half-up có authority riêng cho AOV;
zero/âm và half-tie khác chưa có luật vẫn abstain. Requested/effective windows và denominator
được giữ trong JSON từng câu. Tiếp tục dùng một QueryPlan/entrypoint/model worker, không
thêm dependency/framework/script thực thi vào repo.

Các đoạn tiếp theo giữ checkpoint lịch sử và revision tương ứng.

Bảng câu hỏi → gold → runtime mong đợi → actual hai lượt → điểm tại
[JSON kết quả](reports/stage_results.json). **Native S2 ở checkpoint growth trước operator: 8 correct / 0 wrong / 0 abstain
ở cả hai lượt CUDA**; 6 giá trị gold không dùng để triển khai trước khi khóa/inference và 2 đã exposed.
Đây là common-dev đã chọn, có historical EDA exposure, không phải blind holdout hoặc PA toàn dataset.
Regression S1 giữ 12 correct/0 wrong/1 abstain (92,31% toàn 13 câu; 100% trong 12 câu đã trả lời).
Các bảng 0 correct/2 abstain của checkpoint trước growth được giữ riêng làm lịch sử, không phải kết quả mới.

Growth dùng cùng QueryPlan hai windows và aggregate revenue; operator `percent_change` tính
`(current - baseline) / baseline * 100`, output percent làm tròn 2 chữ số. Tiền giữ Decimal;
tỷ số dùng exact Fraction từ Decimal để không sai sát half-tie. Baseline 0/âm, undefined,
unknown/outside hoặc authority thiếu không được đoán số; giữ preflight/fallback theo từng kỳ.
Tháng tường minh so tháng trước được anchor vào **tháng hỏi**, gồm rollover tháng 1 → tháng 12 năm trước.
Không lấy tháng trước của as_of khi câu hỏi đã ghi rõ kỳ hỏi.

Preparation mới chỉ bật một sheet XLSX order-line với README xác nhận mỗi dòng một sản phẩm,
header đơn lặp đủ và mapping theo đúng filename/section. Lookup quy đổi không JOIN vào revenue/count;
multi-sheet, file chưa phân loại hoặc thiếu authority vẫn unsupported. Native source đã chuẩn bị
39.006 orders / 62.451 lines, 443 summary, 0 issues; coverage all-order dates 01/04/2025–02/06/2026.
Topping có dòng sản phẩm/thành tiền riêng theo README được giữ là sale item; không mở embedded-role parser.
Dòng tổng chỉ cấp bằng chứng định dạng số của cột tiền, không tạo transaction. Alias trạng thái
`Đã hoàn hàng` là returned; `Đang giao dịch` là active không huỷ/trả cho revenue/count, không chứng minh shipment vật lý.

Source mới có **343 tests**, lint/format và lockfile đạt. Hai fresh CUDA workers chạy cùng 36 câu/case:
13 S1 native + 8 S2 native + 15 synthetic; predictions 36/36 giống nhau. Synthetic behavior đạt 15/15;
11 fixture có gold xác định chấm 9 correct/0 wrong/2 abstain, 4 diagnostic không được gán placeholder gold.
Source/model/input hashes và read-only/network-none/label isolation đạt; gold chỉ evaluator host đọc sau cả hai containers.
Evidence hiện tại ở `native_growth_continuation` trong JSON; các root fields giữ checkpoint cũ theo đúng hashes.
Image mới: `datamate-s2-growth-cuda:local`, build source target offline trên `datamate-llm-cuda:local` bằng
`scripts.build_context`/Dockerfile hiện có. Chưa nghiệm thu model CPU growth, Blackwell hoặc toàn S2/Phase 5–7.

Bằng chứng S2 mới nhất và các checkpoint lịch sử theo source hashes tại
`.artifacts/s1-s2-full/verification-evidence.json` (evidence local). Dev của cả hai protocol
không có câu comparison trên hai shape CSV đã bật (`biz_101`, `biz_110`); checkpoint ban đầu chỉ kiểm chứng numeric S2 bằng synthetic; continuation mới nhất đã có 8/8 native revenue growth. Đây vẫn chưa là PA đại diện toàn dataset.

**Checkpoint S2 difference trước growth — lịch sử 03/11/2026:** 318 tests, lint/format và lockfile đạt. Docker CUDA
trên source cuối có 12/12 expected behaviors synthetic đạt: 6 number, 1 unanswerable,
5 abstain; evaluator giữ đủ mẫu số: **7 correct / 0 wrong / 5 abstain**. Regression S1
trên cùng image: **12 correct / 0 wrong / 1 abstain**; hai câu native S2 phần trăm giữ
abstain. Model offload thực 29 layers trên GTX 1650, không CPU fallback. Source/model
hashes khớp, input/root read-only, network none; evaluator host đọc nhãn sau khi mọi
container đã thoát. Output/facts/gold tạm đã tự dọn. Mỗi group trên source cuối có một lượt
CUDA; diagnostics trên revision trước giữ riêng trong S2 report.

Hai lượt CPU synthetic ở hai revision S2 đều timeout tại câu đầu trong budget 120 s,
worker bị dừng; các câu sau worker-unavailable không phải evidence cho scope guards.
**Chưa nghiệm thu numeric S2 trên CPU** hoặc CPU/GPU prediction equivalence. WSL có dùng
swap và host load/cache không kiểm soát; startup/preparation/generation chỉ là quan sát,
không phải SLA. Không tăng budget để đổi kết quả. Native numeric S2 cũng chưa được nghiệm thu.

Image baseline hiện tại: `datamate-s1-s2-complete-cpu:local` và
`datamate-s1-s2-complete-cuda:local`; source/model/image hashes nằm trong JSON mới nhất.
Dùng lại source target trên hai image LLM S1 local để giữ nguyên weights/dependencies;
COPY entrypoint dùng `--chmod` nên hoạt động cả khi base image có USER non-root.
Có thể rebuild source layer offline bằng entrypoint hiện có:

```bash
uv run --no-sync python -m scripts.build_context \
  --output .artifacts/s2-context.tar --manifest .artifacts/s2-context.json
docker build --network none --pull=false --target source \
  --build-arg BASE_IMAGE=datamate-llm-cpu:local -t datamate-s1-s2-complete-cpu:local - < .artifacts/s2-context.tar
docker build --network none --pull=false --target source \
  --build-arg BASE_IMAGE=datamate-llm-cuda:local -t datamate-s1-s2-complete-cuda:local - < .artifacts/s2-context.tar
```

Bằng chứng S1 trước thay đổi S2 tại [reports/planner_verification.json](reports/planner_verification.json).
Hai report normalization/semantics trước đó giữ phạm vi lịch sử; không chứng nhận thay đổi S1 mới.
Chính sách ban đầu giữ coverage unknown đã được thay bằng khoảng quan sát theo yêu cầu
người dùng. Raw input/nhãn không thay đổi. Trạng thái và kết quả numeric train hiện tại
được ghi riêng trong phần tiếp tục S1 của report; kết quả trước đó giữ phạm vi lịch sử.

Grammar sinh S1 giới hạn một window/một aggregate và bounded arrays/text; vẫn dùng cùng
QueryPlan validator. Với cue tương đối rõ ràng (`tháng này`, `tuần này/trước`, `hôm qua/hqua`,
`hôm nay`…), decoder chỉ cho operator tương ứng; resolver vẫn áp runtime rules/as-of.
Validator đối chiếu metric revenue/count với cue câu hỏi, requested bounds trước coverage clipping (gồm ngày/range và tháng/quý có năm tường minh) và bác dấu hiệu scope/operator
chưa hỗ trợ kể cả khi model bỏ `filters`. Không sửa draft hoặc đáp án để vượt gate.
Đây là guard từ vựng hữu hạn, có thể over-abstain và không chứng minh phát hiện mọi entity,
paraphrase hoặc phủ định. Câu chưa có cue vẫn phụ thuộc model; chưa có general NLP certification.

Bằng chứng lịch sử trước chính sách coverage mới: model trials gồm count/revenue timeout 180 giây và một plan train hợp lệ ở budget chẩn đoán
600 giây. Chúng giữ phạm vi lịch sử. Lượt mới có 19 model calls không timeout ở budget 120 giây: 8/8 numeric synthetic đúng,
2/2 filter abstain, 8/8 plan dev đúng nhưng đều bị coverage_unknown chặn execution; một
count unknown synthetic cũng giữ abstain. Hai vòng answers/windows/policies khớp nhau.
Generation quan sát khoảng 9–79 giây, không phải SLA. Xem revision/phạm vi trong report. Bằng chứng synthetic không thay numeric train execution. Các kết quả S1 trên giữ phạm vi lịch sử; M1 ban đầu có numeric train execution qua snapshot bên dưới.

**Nghiệm thu S1 hẹp — 03/11/2026:** hai lượt với model worker mới, cùng subset dev đã khóa
trước khi đọc nhãn; mỗi lượt **12 correct / 0 wrong / 1 abstain**, 0 missing/malformed.
Gồm 3 revenue + 3 count trên CSV order-line và 6 count trên CSV header-only; câu filter sản phẩm
chưa hỗ trợ giữ abstain. Numeric và metric/requested windows khớp raw reference độc lập.
Hai lượt predictions giống nhau. Synthetic mỗi lượt có 5 numeric đúng, 1 unanswerable đúng,
2 unsupported abstain, không loại các abstain khỏi mẫu số evaluator.

**271 tests**, lint/format/lockfile đạt. 42 model calls không timeout ở budget 120 giây;
generation median **25,47 s**, p95 **48,81 s**, max **80,39 s**, peak worker RSS **1.909,84 MiB**.
Đây là benchmark subset CPU với OS caches giữ lại; thời gian model startup/data preparation
được ghi riêng trong report, không phải SLA toàn dataset. Repeat 2 dùng cache facts/catalog.
Docker smoke CPU numeric synthetic có host/container bằng nhau, network none, input/root
read-only; image này chưa đóng gói LLM hoặc chứng nhận GPU/Blackwell.

Phạm vi nghiệm thu là một aggregate/window, toàn business, output number trên hai shape
CSV trên. Header-only revenue, JOIN, Excel order normalization và phần S2–S4 ngoài difference vẫn planned;
parser/guards hữu hạn chưa chứng minh hiểu mọi paraphrase/ngoại lệ. Chi tiết và source hashes
nằm trong [planner report](reports/planner_verification.json).

## Docker LLM offline

Target `llm` dùng cùng entrypoint, đóng gói GGUF local, sáu dependency runtime theo
`uv.lock`, license và thư viện OpenMP cần thiết. Build context chỉ lấy allowlist; không
copy toàn `.venv`, dataset, credentials hoặc reports. Kiểm tra RECORD trước khi bundle;
base image Python 3.11 Bookworm được pin digest trong Dockerfile. Setup cần môi trường
Linux x86_64/Python 3.11 đã sync group `llm`; inference không cài hoặc tải artifact.

```bash
uv run --no-sync python -m scripts.build_context \
  --model .artifacts/models/qwen2.5-1.5b-instruct/model.gguf \
  --output .artifacts/llm-cpu-context.tar --manifest .artifacts/llm-cpu-context.json
# Base image pin trong Dockerfile phải có sẵn local; không pull trong lượt offline.
docker build --network none --pull=false --target llm \
  -t datamate-llm-cpu:local - < .artifacts/llm-cpu-context.tar
uv run --no-sync python -m scripts.verify_runtime --scope llm \
  --image datamate-llm-cpu:local --context-manifest .artifacts/llm-cpu-context.json
```

Verifier dùng 13 câu S1 dev đã khóa trong planner report, hai container mới với cache
preparation dùng lại ở lượt hai. Chỉ mount questions/rulebook/calendar/raw data read-only;
labels/reports không có trong container. Root read-only, network none, output/cache nằm
ngoài dataset. Chỉ evaluator host đọc nhãn sau khi cả hai inference đã thoát. Runs/facts/gold
được dọn tự động; image và model local được giữ. Target `runtime` mặc định vẫn là source-only.

`--gpu-layers 0` giữ CPU; `-1` yêu cầu offload toàn bộ layers. Adapter ghi số layer thực sự
offloaded từ native log và bác yêu cầu GPU nếu không offload được, không âm thầm tính CPU.
GPU cần backend CUDA riêng và Docker GPU access; verifier chỉ cho so sánh sau khi CPU đã đạt
trên cùng source runtime. Không dùng Torch/CUDA host làm bằng chứng LLM Docker đã tương thích.

Bản CUDA local dùng wheel cùng version `0.3.16`, CUDA 12.4/cp311 x86_64 từ
[upstream release](https://github.com/abetlen/llama-cpp-python/releases/tag/v0.3.16-cu124).
SHA-256 wheel: `a455263e7608a51c26079e82fe524f9b7f1f73ee9fc563221a297de8da2a8c51`.
Tải ở bước setup vào `.artifacts/wheels/llama_cpp_python-0.3.16-cp311-cp311-linux_x86_64.whl`,
kiểm hash trước khi stage. Phiên bản dependency vẫn theo `uv.lock`; checksum binary build
được ghi riêng trong build-context manifest. Không thay `.venv` CPU.

```bash
uv pip install --no-cache --no-index --no-deps --link-mode copy --python .venv/bin/python \
  --target .artifacts/llama-cuda \
  .artifacts/wheels/llama_cpp_python-0.3.16-cp311-cp311-linux_x86_64.whl
uv run --no-sync python -m scripts.build_context --cuda-runtime .artifacts/llama-cuda \
  --output .artifacts/llm-cuda-context.tar --manifest .artifacts/llm-cuda-context.json
docker build --network none --pull=false --target cuda \
  -t datamate-llm-cuda:local - < .artifacts/llm-cuda-context.tar
uv run --no-sync python -m scripts.verify_runtime --scope llm --gpu-layers -1 \
  --image datamate-llm-cuda:local --context-manifest .artifacts/llm-cuda-context.json
```

Target CUDA kế thừa model/source từ `datamate-llm-cpu:local` đã nghiệm thu, thêm binding
và các thư viện cần từ hai package NVIDIA đã khóa. Không bundle bản CMake `lib/` trùng
với `llama_cpp/lib`, không copy Torch hay toàn bộ CUDA environment. Verifier so sánh cùng
source, weights và input hashes; GPU memory là số đo toàn device, gồm desktop/process khác.
Hai backend cập nhật cùng report Docker, không tạo các báo cáo lặp theo từng run.
Kết quả và giới hạn tại [reports/docker_llm_verification.json](reports/docker_llm_verification.json).

**Nghiệm thu ngày 03/11/2026:** CPU trước, sau đó CUDA trên GTX 1650 4 GiB,
driver 555.97; mỗi backend hai lượt trên cùng 13 câu đã khóa. Cả bốn lượt đều
**12 correct / 0 wrong / 1 abstain**, không missing/malformed; predictions bằng nhau
giữa các lượt và CPU/GPU, weights/source/input hashes khớp.

| Số đo | CPU | CUDA, 29 layer offload |
|---|---|---|
| Generation median / p95, 26 calls/backend | 33,27 / 73,64 s | 5,77 / 10,30 s |
| Wall time hai lượt, gồm startup/preparation | 505,93 / 601,67 s | 171,05 / 169,02 s |
| Peak worker RSS lớn nhất | 1.870,79 MiB | 1.313,64 MiB |
| Peak VRAM toàn device, sampling 500 ms | Không đo | 1197 MiB |

Lượt GPU đầu bị laptop reset gián đoạn đã được loại khỏi nghiệm thu; hai lượt trên
được chạy lại đầy đủ sau khi khôi phục Docker Desktop. Tải host và OS cache không được
kiểm soát, nên số đo latency chỉ mô tả các lượt này. Chứng nhận giới hạn ở model/runtime,
GTX 1650 và subset S1 trên; chưa chứng nhận Blackwell, toàn dataset hoặc SLA.
Quality sau thay đổi đóng gói: **285 tests**, lint/format và lockfile đạt.
Tar build context và thư mục stage CUDA có thể dọn sau khi build; giữ image, model,
wheel và manifest nhỏ để chạy/rebuild offline.

## M1 ban đầu — inventory snapshot tại mốc xác nhận

M1 đã kiểm chứng ngày 03/11/2026 qua **một câu train-dev**, không thay đổi gate coverage lịch sử.
README của nguồn POS xác nhận `Tồn cuối kỳ` là tồn cuối ngày cuối có dữ liệu theo đơn vị cơ bản,
và xác nhận ngày kết thúc. Đây là authority cho một snapshot point, không chứng minh đủ dữ liệu
bán hàng cho bất kỳ kỳ nào. Lookup này không cần suy available start.

```bash
./predict.sh --data-dir dataset/train --output-dir .artifacts/m1-snapshot-run \
  --model .artifacts/models/qwen2.5-1.5b-instruct/model.gguf \
  --question-id q_cea1b708d80a --question-timeout 120
```

Lát cắt hẹp dùng cùng QueryPlan: `inventory_on_hand`, scope `product`, một source identity,
`today` đúng snapshot date và đơn vị nguồn được hỏi tường minh. Chỉ nhận một bảng XLSX có đúng
`Mã hàng`, `Tên hàng`, `ĐVT`, `Tồn cuối kỳ`, không JOIN. Context chỉ đưa candidate code/name/unit;
quantity không đưa cho LLM. Decoder giới hạn entities bằng mã nguồn; validator vẫn kiểm tra
identity xuất hiện trong câu, unique match giữ dấu, unit và thời gian. Không sửa plan/output
sau khi model sinh. Unsupported/mơ hồ/sai unit/ngày hoặc lỗi nguồn dẫn tới abstain.

Prepared store `prepared-facts-v2` thêm `inventory_snapshot`; quantity TEXT Decimal và source ref
trên từng dòng, header/blank/invalid có accounting. Duplicate identity không tự dedup. Executor
chạy fixed SELECT read-only, đòi đúng một dòng và xuất exact source quantity, không làm tròn tiền.
Không đổi đơn vị, tính giá trị tồn, tra lô hoặc tái dựng inventory history. Revenue/count trên
nguồn thiếu historical coverage vẫn abstain; không bật các shape order-line XLSX/JOIN mới.

Câu train được chạy hai lượt bằng LLM thật, preflight đủ snapshot authority, SQLite execution,
verifier/typed number rồi evaluator riêng chấm đúng cả hai lượt. Nhãn chỉ đọc sau inference;
input giữ nguyên hash, output tạm đã dọn. Report có source/model/code hashes, SQL và raw-reference
độc lập tại [planner_verification.json](reports/planner_verification.json). Đây là M1 wiring
trên một câu dev, không phải PA toàn train, model entity-generalization hoặc toàn S1/S4 đã đạt.

## Cấu trúc tối thiểu

- `src/`: contracts, split, readers/profile/catalog/cache, normalization/semantics S1, temporal resolver, local planner/executor, mock runtime và evaluator.
- `scripts/verify_runtime.py`: kiểm tra runtime/Docker/GPU; `verify_ingestion.py` kiểm tra nguồn train với output tạm. `build_context.py`, `probe_environment.py` hỗ trợ Docker/phần cứng.
- `tests/`: output/scorer/runtime, data tools, readers, catalog/cache, normalization, semantic/preflight và temporal; dùng dữ liệu synthetic.
- `scripts/audit_dataset.py`, `scripts/annotate_questions.py`: đối chiếu/validate train và annotation offline; runtime không import hai script này.
- `docs/`: data contracts, observations, capability matrix và acceptance cases; `config/validation_split.json` giữ protocol đánh giá.
- `README.md`, `roadmap.md`: hướng dẫn và kế hoạch; không duy trì ADR/nhật ký/config mô tả trùng lặp.
- `reports/`: bằng chứng runtime, data preparation và ingestion; `.venv/`, `.artifacts/`, `dataset/` được ignore.

Rulebook/README/calendar đúng split là nguồn ngữ nghĩa runtime. Inference không đọc nhãn/EDA, không kết nối mạng và không sửa input. Scorer nội bộ chưa được đối chiếu hoàn toàn với evaluator BTC; các giới hạn model/phần cứng/nộp bài cần đối chiếu theo mục 2.1 và Phụ lục B của roadmap. Model candidates ở Phụ lục G vẫn là hướng nghiên cứu cho bài nộp; thử nghiệm S1 local dùng Qwen2.5-1.5B Q4_K_M như mô tả ở trên.
