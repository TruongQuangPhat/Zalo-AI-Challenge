# Zalo AI Challenge — DataMate

Runtime đọc CSV/XLSX/SQLite, chuẩn bị context một lần/business và ghi JSONL/log theo câu. Mặc định vẫn **mock abstain**; `--model` bật local planner S1, QueryPlan validator, resolve/preflight và executor revenue/order_count toàn business. Tiền tính bằng Python Decimal trên SQLite read-only, rồi verifier và output validator kiểm tra trước khi ghi đáp án. Coverage dùng khoảng ngày đơn quan sát được khi đọc/parse đầy đủ, ưu tiên khoảng đầy đủ được README khai báo và chặn mâu thuẫn. CSV một dòng/một hóa đơn hỗ trợ count, chưa hỗ trợ revenue. Có thêm lookup inventory snapshot tại đúng mốc được README xác nhận, một sản phẩm và base unit. M1 và S1 hẹp đã có bằng chứng LLM thật → execution → evaluator trên train-dev; các shape mở rộng và toàn baseline chưa hoàn thành. Tiến độ và bằng chứng nằm trong [roadmap.md](roadmap.md).

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
Hiện hỗ trợ CSV order-line có cấu trúc rõ, CSV header-only cho order_count và một XLSX inventory snapshot bốn cột có authority rõ (xem phần M1). Các shape khác ghi `unsupported`, không tạo facts rỗng.
Summary `ready` chỉ xác nhận preparation không bị blocking issue; không chứng minh đủ coverage
hoặc metric đã có executor. `blocked` giữ lỗi/facts để kiểm tra và chặn metric phụ thuộc;
CLI `prepare` trả exit 1 khi có blocking issues.

Tiền lưu TEXT Decimal, giữ phần lẻ nguồn; đọc qua `PreparedBusiness.iter_records()` trả Decimal.
Không dùng SQLite `SUM/AVG/CAST REAL` trực tiếp cho tiền; phép tính chính xác dùng Python Decimal.
Ngày chỉ có ngày giữ ISO date, timestamp giữ giờ/offset. Giữ mọi status và ngày sau câu hỏi;
chỉ query-time mới áp policy metric. Summary/tổng nguồn không tạo giao dịch; exact dedup trong
cùng đơn giữ lineage; trả theo SL âm chỉ xử lý khi README và rulebook xác nhận.

Cache facts nằm trong `<cache-dir>/prepared/<fingerprint>/`, gồm database và manifest checksum.
Metadata trong database chứa schema/software/source/authority versions, mapping, coverage và
quality summary; không tạo thêm file audit/quality theo từng dòng. Coverage theo chính sách dữ liệu phát hành bên dưới; export rỗng hoặc parse chưa đủ không được tự tạo phạm vi. Lịch sử return cho kho, JOIN nhiều nguồn, Excel/SQLite normalization,
quantity/unit/cost và nghiệp vụ ngành chưa được bật.

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
Eligibility chưa phải đáp án. Executor S1 chỉ bật với validated plan; entity filters và derived execution chưa hỗ trợ.
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

## Local planner S1

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
context 4096, batch 128, decode/batch threads mặc định cùng bằng 4, tối đa 256 output tokens.
Worker giữ model một lần/run; load timeout riêng,
question timeout chung cho tối đa hai attempts, quá hạn dừng worker và abstain.
Retry sửa JSON lỗi; plan parse được nhưng có intent/scope chưa hỗ trợ giữ abstain.
Nếu worker đã dừng, các câu sau abstain; không tải hoặc tự reload model ngầm.

`src/agent/plan.py` giữ schema draft của **cùng QueryPlan theo Phụ lục E**.
LLM chọn metric/time/scope; server gắn identity, authority, requirements, units/policy,
rule refs và resolved/effective windows. S1 giới hạn một aggregate/một window;
mảng windows/aggregations và derived_measures giữ đường mở S2. Identity, SQL, policy hay
numeric answer do model tự thêm bị từ chối. Không có free-form code execution.
Context chỉ lấy metadata schema/contracts/coverage/rule refs/warnings, không đưa raw rows vào prompt.
Authority hashes được chia sẻ thay vì lặp theo từng rule; metadata business đứng trước
`as_of_date`/question để llama.cpp tái dùng token prefix, không cache đáp án. Logs ghi
tách thời gian prompt/decode và số prompt tokens thực sự được tính lại.

Executor dùng fixed read-only SELECT, kiểm tra grain/status/mapping/line completeness và
Decimal hữu hạn, giữ số chính xác tới bước rounding. Half tie chưa có luật được xác minh
vẫn abstain. Zero chỉ có nghĩa khi coverage đủ; ngoài coverage đã chứng minh mới unanswerable.
Validator kiểm tra contract của plan, chưa chứng minh model luôn hiểu đúng mọi câu tiếng Việt;
phải kiểm tra lỗi bỏ filter/phủ định trước khi mở phạm vi.

Bằng chứng mới được ghi tại [reports/planner_verification.json](reports/planner_verification.json).
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
CSV trên. Header-only revenue, JOIN, Excel order normalization và S2–S4 vẫn planned;
parser/guards hữu hạn chưa chứng minh hiểu mọi paraphrase/ngoại lệ. Chi tiết và source hashes
nằm trong [planner report](reports/planner_verification.json).

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
