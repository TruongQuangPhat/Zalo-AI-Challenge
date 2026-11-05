# Data contracts — Phase 1

Contract version: `data-contracts-v1`. Taxonomy: `question-taxonomy-v1`.
Đây là đặc tả đầu vào cho S1; trạng thái triển khai nằm trong
[roadmap](../roadmap.md), không suy ra từ việc có tài liệu hoặc fixture.
Các hợp đồng C–F trong roadmap và rulebook/README/calendar đúng split là nguồn đối chiếu.
Readers, profile và business catalog đã có implementation theo Phase 2;
Prepared facts cho CSV/XLSX/SQLite đã có trong `src/data/normalization/`, gồm checked
header/detail/master joins, continuation và sổ bán không mã đơn. QueryPlan, metric
contracts, temporal resolver, preflight và Decimal execution dùng chung S1/S2.
Entity mapping rộng/ranking vẫn chưa bật; bảng capability ghi đúng giới hạn thực tế.

## Kiểu chung và phạm vi S1

- `ID`: chuỗi không rỗng; entity key luôn gồm split, business, entity kind và source key.
- `Day`: ISO `YYYY-MM-DD`; `Timestamp`: giữ cả giờ và timezone/quy ước giờ nguồn.
- `Money`/`Quantity`: Decimal hoặc scaled integer khi tính; JSON number khi xuất.
  Raw text không được mất. Không parse tiền qua float; không lấy tolerance làm rounding policy.
- `Span`: `{start: Day, end: Day}`, gồm hai đầu, start ≤ end.
- `SourceRef`: `{split, business_id, file, sha256, table, row, column}`.
  File tương đối với split, không thoát root; SHA-256 là 64 hex của phiên bản file.
  CSV: `row` là physical line bắt đầu record, bổ sung `line_end` khi record nhiều dòng;
  `record_ordinal` 1-based tính cả header/blank records của parser, khác số physical lines.
  XLSX: `row` là số dòng worksheet 1-based, giữ cell address khi tham chiếu ô.
  SQLite: `row` là null, bổ sung `locator={kind, columns, values}`; kind là
  `primary_key` đã kiểm tra hoặc `rowid` khả dụng. Locator gồm source hash nên chỉ
  có hiệu lực cho đúng phiên bản file. Với bảng không có hai loại locator đó,
  cần chiến lược duyệt ổn định được kiểm chứng trước khi báo đã đọc thành công;
  không dùng thứ tự SELECT không ORDER BY làm định danh ổn định hoặc thứ tự event.
  `column` là identity theo vị trí 1-based trong bảng nguồn, tên hiển thị giữ riêng;
  `row`/`column` có thể null với tham chiếu cả bảng/file. Không dùng locator để đếm đơn.
- Phase 2 giữ raw value trước diễn giải: CSV là text; XLSX giữ raw numeric token,
  native type, style/date-system và formula/cached value riêng; SQLite giữ declared type
  cùng storage class thực. Số lưu REAL đã có giới hạn nguồn, không khẳng định khôi phục
  được precision trước khi lưu. BLOB nếu có dùng base64 + encoding/native-type tag,
  không decode tùy ý thành text. Không coi formula cache thiếu là blank hợp lệ hoặc 0.
- `source_id` định danh split/business/path/sheet-or-table/region; content hash là revision
  riêng. `source_row_id` gắn source revision và locator. Mỗi cột có `column_id` theo vị trí,
  tên nội bộ duy nhất và raw header refs; header rỗng/trùng không được ghi đè hoặc mất cột.
- Nullable chỉ dùng khi trường chưa biết/không áp dụng và có trạng thái/lý do đi kèm.
  Phân biệt `source_missing`, `inherited`, `parse_failed`, `ambiguous`, `not_applicable`.
  Không biến missing/error thành 0 hoặc bỏ dòng để xuất tổng thiếu.
- Trường ghi **S1** phải có trước khi bật revenue/count. Các extension chỉ đặc tả;
  không cần tạo class, reader hoặc facts chưa có nơi sử dụng.

## Hợp đồng dữ liệu và ngữ cảnh

| Contract | Trường bắt buộc, kiểu và nullability | Invariant / phạm vi |
|---|---|---|
| `SourceTable` **S1** | `source_id: ID`, `source_ref: SourceRef`, `format: csv/xlsx/sqlite`, `header_rows: int[]`, `columns: ColumnProfile[]`, `grain: string`, `key_candidates: string[][]`, `key_status: known/unknown`, `roles: string[]` | Header có thể nhiều dòng; candidate key chưa được coi là khóa đã xác minh. Không tự gộp/tách order theo tên file. Extension: sheet/year/date-system hints, continuation refs và FK statistics. |
| `SourceRow` **S1** | `source_row_id: ID`, `source_ref: SourceRef`, `cells: {column: {raw_value: JSON scalar/null, native_type: string}}`, `row_role: enum`, `group_key: ID/null`, `mapping_status: known/unknown/ambiguous`, `transformation_refs: ID[]` | Raw giữ nguyên cả STT/giờ/mã gốc. Group key thiếu phải có lý do; không dùng source_row_id để đếm đơn. Sau dedup vẫn tra được mọi raw ref. |
| `ColumnProfile` **S1** | `name: string`, `position: int`, `non_null_count: int`, `row_count: int`, `native_types: {type: count}`, `semantic_role: string/null`, `inferred_type: string/null`, `parse_status: not_checked/complete/partial/failed`, `issue_refs: ID[]` | Counts không âm, non-null ≤ row count; thống kê raw khác dữ liệu chuẩn. Date min/max sơ bộ không chứng minh coverage. Extension: distinct/sample values, native error/formula flags. |
| `EntityMapping` **S1 cho mã đơn** | `entity_kind: string`, `namespace: {split,business_id}`, `raw_key: scalar`, `canonical_key: ID/null`, `status: resolved/unknown/ambiguous`, `source_refs: SourceRef[]`, `rule_refs: string[]` | Giữ mã như chuỗi; khác mã vẫn khác entity dù cùng tên. Null key không được gán mã giả để tạo count. Extension: alias candidates, SĐT/IMEI, product/customer resolution. |
| `BusinessContext` **S1** | `split: ID`, `business_id: ID`, `rulebook_hash: sha256`, `readme_hash: sha256`, `calendar_hash: sha256`, `tables: SourceTable[]`, `coverage: Coverage[]`, `capabilities: Capability[]`, `issues: TransformationIssue[]` | Chỉ chứa nguồn của business hiện tại và authority đúng split. Không chứa answers/QA/annotations. Extension: source/parser/schema/config versions trong cache key. |
| `Coverage` **S1** | `source_refs: SourceRef[]`, `fact_type: string`, `time_axis: string`, `observed_span: Span/null`, `available_span: Span/null`, `availability_basis: string[]`, `snapshot_at: Timestamp/Day/null`, `history_available: true/false/null`, `parse_status: enum`, `uncertainties: string[]` | Chính sách export: min/max ngày order parse đầy đủ được dùng làm available span, có basis observed_order_dates và cảnh báo không chứng nhận completeness. README complete span ưu tiên, mâu thuẫn/parse thiếu giữ abstain, export rỗng chưa có authority giữ unknown. Snapshot chỉ áp dụng tại mốc được xác nhận. Extension: coverage từng partition/file và lịch sử tái lập. |
| `TransformationIssue` **S1** | `issue_id: ID`, `code: string`, `source_refs: SourceRef[]`, `raw_value: scalar/null`, `affected_fields: string[]`, `affected_metrics: string[]`, `severity: warning/blocking`, `resolution: string/null` | Lỗi cột phụ không loại cả hàng; lỗi ảnh hưởng tổng phải chặn metric. Không chỉ ghi warning rồi tính phần còn đọc được như tổng đầy đủ. |

Phase 2 bổ sung metadata có nơi sử dụng cho các contract trên:

- `SourceTable`: `read_status=read/empty/failed`, `structure_status=resolved/ambiguous/unsupported`,
  header/boundary candidates và evidence. File chỉ `read` khi mọi sheet/table trong phạm vi
  đã được duyệt; metadata nguồn rỗng vẫn có trong catalog. Grain/roles chưa phân giải giữ unknown.
- `SourceRow`: `structure_role` cho title/header/repeated-header/blank/summary-candidate/
  data-candidate/unknown và source accounting. Đây không phải `row_role` nghiệp vụ;
  Phase 2 chưa xác lập thì `row_role=unknown`, group key null có lý do, không forward-fill/dedup.
- `ColumnProfile`: scope/counts và rows scanned; mỗi statistic ghi exact/estimated/not_checked.
  `non_null_count` đếm raw value không null, gồm cả chuỗi rỗng và native error;
  blank-string/error counts là các thống kê riêng, có thể thuộc non-null, không cộng mù
  thành row count. Ô XLSX không có cell và cell blank giữ distinction trong raw metadata.
  `parse_status=not_checked` trước khi gọi parser chung; mẫu không chứng minh toàn bảng.
  Tối đa 5 sample values khác nhau/cột, chọn theo thứ tự nguồn ổn định.
- `BusinessContext`: references tới source manifest, reader/profile/schema/config versions,
  raw handles/cache và trạng thái completeness/issues; không cần nhét mọi raw row vào JSON.
  Cache raw/profile khác cache facts chuẩn hóa, không mang theo cutoff của câu hỏi đầu tiên.

API raw hiện tại: `SourceTable.iter_rows()` hoặc `BusinessContext.iter_rows(source_id)`
trả `SourceRow(ordinal, row, cells, locator, line_end)`. `cells` dùng vị trí cột 1-based,
mỗi `Cell` có `raw_value`, `native_type`, `metadata`; `row.reference(table, column)` và
`row.identity(table)` tạo provenance/ID theo revision. Đây là phần decoded raw của contract
S1; các trường entity/group/row-role nghiệp vụ do normalization bổ sung khi có căn cứ.
Profile chỉ đề xuất structural roles và giữ accounting; không tạo order/fact từ locator.

`read_status=not_read` chỉ dùng cho SQLite system tables/views được kiểm kê nhưng loại khỏi
đường đọc bảng nghiệp vụ với `exclusion_reason`; trạng thái này không chứng minh nguồn đầy đủ
cho metric phụ thuộc view. Distinct/key/relationship checks có giới hạn bộ nhớ; khi vượt giới hạn
phải giữ partial/not_checked. Runtime không coi `read_complete=true` là metric capability.

`row_role`: `sale_item`, `service_fee`, `order_adjustment`, `return_item`, `payment`,
`purchase_item`, `inventory_event`, `inventory_snapshot`, `repair_visit`, `warranty_visit`,
`catalog`, `unknown`; bổ sung `summary` cho tổng cộng nguồn, không tạo fact giao dịch.
Nhận diện summary trước grouping/forward-fill. Dòng unknown có thể ảnh hưởng số liệu phải chặn metric.
Âm ở voucher không tự là return; sổ không mã đơn giữ các bản ghi giống nhau.

S1/S2 baseline hiện tại đã kiểm chứng trong scope hữu hạn; kết quả tại
[JSON chung](../reports/stage_results.json); evidence kỹ thuật local tại
`.artifacts/s1-s2-full/verification-evidence.json`, section `s1_s2_final_baseline`. Summary phải bị nhận diện trước
header inheritance; bare “Cộng/Tổng” chỉ bị loại khi thoả điều kiện cấu trúc dòng,
không loại giao dịch thật chỉ vì trùng tên. Header revenue có filter kênh phải lọc
`order_scopes` trước reduction; missing channel của bản ghi liên quan giữ abstain.
Cache phải khớp toàn bộ normalizer/parser dependencies và checksum facts, không gắn
metadata revision mới lên facts cũ để chứng nhận. Report phân biệt unchanged-draft
replay có context proof với fresh LLM inference; gold chỉ thuộc evaluator sau workers.

## Hợp đồng truy vấn, metric và output

| Contract | Trường bắt buộc | Invariant / extension |
|---|---|---|
| `QueryPlan` **S1 subset** | `question_id`, `business_id`, `as_of_date`, `rulebook_version`, `rule_refs[]`, `entities[]`, `time_windows[]`, `requirements[]`, `aggregations[]`, `derived_measures[]`, `output`, `uncertainties[]` | S1 có một aggregate revenue hoặc order_count, explicit requested/effective window, axis ordered_at, source requirements, status policy và output number. IDs/refs có nghĩa, business không đổi. Extension: DAG nhiều kỳ/ratio/ranking theo Phụ lục E; operator chưa có → abstain. Parse được JSON không chứng minh plan đúng. |
| `Answer` **đã có validator** | `id: ID`, `type: OUTPUT_TYPES`, `answer: typed JSON` với type cần giá trị; `reason?: string` chỉ cho unanswerable | Dùng `src/contracts.py` và `src/jsonio.py`; không tạo schema output cạnh tranh. Percent là số phần trăm, bool khác number; ordered list khác unordered table rows. Aliases chỉ thuộc evaluator/gold, không được xuất trong submission. |
| `MetricContract` **S1** | `metric_id`, `rule_refs/version`, `required_facts/fields`, `input_grain`, `count_unit`, `status_policy`, `time_axis/window`, `unit/price_basis`, `formula`, `numerator/denominator_scope`, `empty/undefined_policy`, `rounding`, `output/order`, `acceptance_refs[]` | Trường không áp dụng ghi null + not_applicable. Không dùng một valid_orders chung cho mọi nghiệp vụ. Extension: JOIN cardinality, lịch sử, giá vốn, snapshot và derived metrics. |
| `Capability` **S1** | `capability_id`, `priority: P0/P1/P2`, `input_shape`, `required_sources/fields/rules/operators`, `status: planned/supported/unsupported/ambiguous`, `acceptance_refs`, `evidence_refs`, `fallback`, `limitations` | Theo cấu trúc/metric, không theo business/question ID. Supported cần execution thực của capability đạt test; chỉ đường abstain đã đạt không làm revenue supported. |

Hai metric S1:

| Thuộc tính | `revenue` | `order_count` |
|---|---|---|
| Authority | Train rulebook §1, §2, §7–9 + README nguồn, lưu hash runtime | Cùng authority; count distinct theo mã đã xác minh |
| Required facts/fields | Order key/date/status + line amount/role/provenance; join refs nếu tách bảng | Order key/date/status và bằng chứng group/role; không đòi sản phẩm khi nguồn header-only đủ |
| Status | completed/shipping; bỏ cancelled/returned cả đơn, kể cả partial return sau as-of | Cùng policy cho số đơn doanh thu |
| Time axis | ordered_at; kỳ gồm hai đầu, chặn theo as-of khi câu hỏi yêu cầu; không dùng ngày thu/giao | Cùng trục thời gian |
| Grain/formula | SUM line net amount sau giảm dòng; exclude source summaries; tính service fee khi luật yêu cầu; không trừ giảm cấp đơn/VAT/ship | COUNT DISTINCT canonical order key sau điều kiện metric; không dùng line count |
| Unit/price basis | VND, amount ghi trên dòng, không tự tính lại SL × đơn giá; numerator/denominator null | Đơn; price basis và numerator/denominator null |
| Dedup | Chỉ exact duplicate source fields trong cùng đơn sau group restoration; giữ lineage | Một đơn đếm một lần, kể cả qua nhiều file/sheet |
| Empty/coverage | Complete kỳ, filter rỗng → 0; ngoài available span → unanswerable; partial overlap theo luật; unknown/parse fail/unsupported → abstain | Cùng phân biệt; không suy thiếu order IDs thành tổng số dòng |
| Rounding/output | Decimal SUM chính xác, chỉ round khi luật yêu cầu, VND nguyên ở output number; chưa chốt rounding mơ hồ thì abstain | Integer number, stable serialization |
| Acceptance | C01–C06, C12, C16–C18, C30; C09 khi JOIN; C27 khi cache | C03–C06, C12, C16–C18, C30; C09 khi JOIN |

Không bật S1 khi có dòng unknown, đơn mâu thuẫn trạng thái/ngày, đơn thiếu liên kết hoặc tổng thiếu.
Case tiền/ngày ở fixture là yêu cầu cho parser tương lai, không phải parser đã tồn tại.

## Taxonomy và annotation

Schema `question-annotation-v1` được kiểm tra bởi `scripts/annotate_questions.py`.
Mỗi record có ID/business đúng questions, version, method, các chiều dưới,
`dimension_status`, `review_status`, `notes` và `source_verification=not_checked`.
Không lưu answer, alias, gold type, SQL hay plan giải sẵn trong record.

| Chiều | Định nghĩa |
|---|---|
| `intents[]` | aggregate: tính tổng/count/ratio/average; ranking: chọn theo cực trị/top-k; comparison: so các scope/kỳ; lookup: tra giá/thuộc tính; listing: liệt kê nhiều đối tượng/chỉ tiêu; unknown khi chưa phân giải. Có thể nhiều nhãn. |
| `metrics[]` | Họ metric dùng phân tích lỗi, không phải executable registry. Danh mục trong `METRICS`: revenue/order_count/average_order_value/customer_spend, quantity/customer/product counts, cancellation/returns, profit/cost, debt, inventory/stockout/dead_stock, purchase/supplier/price, expiry/lot/prescription, delivery, attributes/expense và unknown. Regex là đề xuất, có thể nhầm metric chỉ được nhắc trong câu. |
| `temporal_features[]` | explicit_period: ngày/tháng/quý/năm nêu rõ; relative_period: tương đối theo as-of; multiple_periods: cần nhiều scope/kỳ; snapshot: trạng thái tại mốc; lunar: dùng lịch âm; prior_history: cần dữ liệu trước cửa sổ hoặc tìm sự kiện; unknown khi chưa rõ. Có thể đồng thời nhiều đặc điểm. |
| `required_facts[]` | Nhu cầu ngữ nghĩa: sales/orders/units/customers/products/payments/inventory/lots/... theo `FACTS`. `sales` là nhóm facts bán hàng có liên kết orders–lines; chưa xác minh đủ nguồn. Không dùng làm bằng chứng missing data. |
| `expected_output_type` | Suy từ câu: các type hợp đồng output hoặc unknown. List/table không rõ giữ ambiguous. Không đoán unanswerable từ regex. Gold output type chưa trích; chỉ evaluator mới được đọc labels khi cần. |
| Trạng thái | Mỗi chiều proposed/reviewed/unknown/ambiguous; record reviewed chỉ khi có manual-question-review provenance. Unknown là một giá trị riêng, không trộn vào mảng nhãn known. Reviewed vẫn có thể chứa chiều ambiguous/unknown với lý do. |

Ví dụ synthetic: “Doanh thu tháng 6/2025 tăng bao nhiêu % so với tháng trước?”
→ intents aggregate/comparison, metric revenue, temporal explicit/relative/multiple,
required sales, output percent; source_verification vẫn not_checked.
“Ba món hot nhất?” → ranking/listing, metric ambiguous, không tự đổi thành quantity.

Pilot lấy 100 câu ở giao phần dev của cả hai protocol: round-robin business và dạng câu,
hash seed+ID để chọn trong bucket, không dùng gold. Rà nghĩa câu, sửa taxonomy chung rồi
khóa v1. Các đề xuất còn lại không được gọi gold annotation. Review cục bộ được giữ để
tái tạo output bằng `--reviews`; metadata này không thuộc runtime inference/Docker context.

## Prepared facts S1 — implementation boundary

`BusinessContext.prepared` trỏ tới `PreparedBusiness(path, summary, cache_hit, elapsed_seconds)`.
`path=null` cùng `status=unsupported` khi shape/authority chưa hỗ trợ; không có bảng rỗng giả.
`ready/blocked` là trạng thái preparation, độc lập available coverage và metric execution.
`connect()` mở database read-only/query-only; `iter_records()` chỉ đọc bảng facts/audit đã cho phép.

SQLite STRICT dùng một file `facts.sqlite`, không thêm dependency. Schema version là
`prepared-facts-v6`; dependencies/quality summary nằm trong bảng `metadata`. Money là TEXT
Decimal chính xác, qua API trả Decimal; không dùng SUM/AVG/CAST REAL cho tiền trong SQL.
Các bảng mở rộng bên dưới giữ numeric TEXT khi đọc records; executor chuyển trực tiếp
từ TEXT sang Decimal. Không chuyển qua float.
`orders` giữ raw ID/namespace key, ordered_at, canonical status, mapping_status.
`order_lines` giữ key riêng, order FK, net_amount nullable khi lỗi có issue, row_role,
source_values trước parse (sau phục hồi header), event_at cho return đã có nguồn ngày.
Return item nằm riêng role với sale item; event_at chưa biết giữ null, chưa chứng nhận stock history.
`lineage` map mỗi raw row tới line key hoặc disposition header/title/blank/summary/quarantined;
bản trùng trỏ về line giữ lại. `issues.detail` có code, raw value, source ref, affected metrics và severity.

Mapping mở trên các role/schema có authority README hoặc finite POS column labels.
Header-only giữ orders và order_revenues khi nguồn khai báo tiền hàng thuần; không
tạo order_lines giả hoặc suy revenue từ tổng payable. Header/detail join kiểm tra
cardinality, key conflicts và orphan trước khi tính. Nhóm hàng/khách join bằng mã;
tên không dấu chỉ được nối khi khớp duy nhất một mã danh mục theo quyết định người dùng
04/11, có basis/warning/count trong metadata. Đa ứng viên vẫn chặn.
Ledger giữ sales_events theo grain bản ghi, giữ hai dòng giống nhau là hai events;
không tạo mã đơn để count. Thiếu order identity có bằng chứng trả unanswerable.
Các sheet/phần tiếp nối cần authority; bảng phụ tồn/giá/thu/nhập không tự nhập sales.
Header inheritance/negative-return conventions cần README; dedup/whole-order return cần
operator rulebook đã nhận diện. Mapping/locale/coverage có căn cứ và hashes; luật chưa hiểu
hoặc shape khác trả unsupported. Day/timestamp giữ precision thời gian nguồn, không cắt theo as-of.
Available span ưu tiên khai báo đầy đủ hợp lệ trong README, nếu thiếu dùng min/max ngày đơn
đã parse đầy đủ theo chính sách export được người dùng chấp nhận ngày 03/11. Metadata giữ
observed_order_dates và cảnh báo missing interior records; không mở khi parse/đọc thiếu, rỗng
chưa có authority hoặc nguồn mâu thuẫn README. Không dùng min/max calendar làm coverage.
Ledger dùng observed_sales_record_dates riêng. Dấu nhóm tiền nghìn được chấp nhận khi
toàn cột nhất quán theo cụm ba chữ số, ghi user basis/warning; explicit README ưu tiên.
Ngày Excel/native clock giữ date system và precision nguồn. Return ghi âm cùng mã
đơn được liên kết với đơn gốc, không đổi ordered_at/clock sang ngày trả hàng.
Dòng tổng “Cộng”/“Tổng” không identity/quantity trong sales/ledger được loại trước
kế thừa header, giữ raw lineage; không đưa tổng sheet vào đơn cuối. Revenue header
có filter kênh phải lọc order_scopes trước reduction; missing channel không thành 0.

## Semantic metadata S1 — implementation boundary

`Split.rules` giữ rule profile, `Split.calendar` là tuple `CalendarDay` đã được loader
kiểm tra; contexts cùng split chia sẻ chúng. Industry override chỉ chọn từ khai báo
`Ngành: ...` trong README; scope chưa xác định khi có override liên quan giữ ambiguous.
Rule parser là tập clause/operator hữu hạn, không phải bộ hiểu rulebook tự nhiên tổng quát. Có aliases hữu hạn cho labels và guard paragraph/section nêu ngoại lệ rõ ràng; qualifier chưa hỗ trợ chặn policy liên quan với source refs. Không chứng nhận phát hiện mọi mâu thuẫn/paraphrase trong tài liệu.
Dedup/header inheritance/whole-order return ở preparation dùng cùng profile này.

`prepare_semantics(context)` tạo `.semantics`, lưu cùng payload catalog hiện có, ngoài
raw catalog cache và facts metadata. Có rules/source refs, contracts, schema facts,
coverage, versions và revision; không mang cutoff câu đầu hay labels. Metadata business
không tự cấp quyền trả số dù contract đã resolved; mỗi câu vẫn cần validated plan/preflight/executor/verifier.

`MetricContract` là dataclass tại `src/semantics/metric_registry.py`; `.summary()` trả JSON.
`revenue` và `order_count` có required fields/status policy riêng. Contract tiền ghi rõ
nearest đồng với tie chưa có luật thì abstain; không tự dùng ROUND_HALF_EVEN/HALF_UP
cho mọi khoản tiền. Executor Decimal và typed serializer đã được nối cho S1; các phép tính mở rộng thuộc S2 trở đi.

`TimeWindow` giữ requested bounds inclusive, as-of/cutoff và rule refs; `contains()` xét
ngày local của timestamp nguồn, giữ offset và bao trọn phần giây lẻ ngày cuối. Không
chuyển timezone theo máy hoặc hỗ trợ ca khi chỉ có date. Tết và trước Tết kiểm tra calendar
liên tục cho cả kỳ gốc và kỳ so, không suy từ cột holiday hoặc nguồn bên ngoài.

`preflight` nhận metric contract và biểu thức temporal có cấu trúc; các filter đã đăng ký
gồm weekday, product/customer group, channel và shift. Filter/metric/operator khác
dẫn tới abstain. QueryPlan tối đa ba windows/aggregations, một derived measure hoặc
list tối đa ba scalar; mỗi operand preflight độc lập. Kết quả gồm:
`decision`, `reason`, `contract`, `window`, `effective_window`, `evidence_refs`, `issue_refs`.
`eligible_to_execute` không có answer value và chưa chứng nhận answerability cuối.
Schema thiếu field canonical là engine chưa hỗ trợ, không là proof dữ liệu gốc thiếu.
Available span theo chính sách export ở trên; confirmed end riêng chỉ chứng minh kỳ sau
ngày kết thúc khi chưa xác định được available span. Source end mâu thuẫn hoặc parse ngày
chưa đủ giữ unknown/conflict; issue chỉ chặn đúng metric phụ thuộc.

Tests và bằng chứng nằm tại `tests/test_normalization.py` và roadmap; C01–C30 trong fixture
vẫn là specifications toàn capability, không đổi thành passed chỉ từ một phần preparation.

## Inventory snapshot — limited M1 dependency slice

`inventory_snapshot(product_code, product_name, base_unit, quantity, snapshot_at, source_ref)`
là grain product tại một mốc README xác nhận, quantity TEXT Decimal. Chỉ một XLSX table có
đúng bốn header POS; authority date/base-unit declaration cần rõ và không conflict nhận diện.
Đây không là transaction available_span: history vẫn unknown và không mở revenue/count.

Cùng QueryPlan dùng `inventory_on_hand`, một entity, scope product, explicit base unit và today
đúng declared snapshot. Nguồn phải có unique identity, giữ dấu/digits; case/punctuation/spacing
aliases được nhận, fuzzy/deaccent không là proof. Candidate metadata không có quantity.
Preflight giữ checks namespace/revision/quality, rồi snapshot date/entity/unit; executor SELECT
read-only đúng một row và giữ exact quantity, không tiền-rounding. Other dates, unit conversion,
JOIN/history/cost/lots/quantity ranking chưa có. Tests tại `tests/test_stock_snapshot.py`;
M1 chỉ chứng nhận wiring train-dev đã ghi trong roadmap/report, không toàn bộ capability.

## Phần mở rộng S3/S4 trong cùng prepared store

Đây là các bảng phụ thuộc vào mapping/authority đã nhận diện. Sự có mặt của bảng
trong schema không cấp quyền trả lời: `summary.extensions.issues`, required fields,
preflight và dữ liệu của các bản ghi liên quan vẫn phải qua gate. Lỗi payment/cost
không tự chặn revenue/count S1. Kết quả nghiệm thu đọc theo evaluation trong
[JSON chung](../reports/stage_results.json), không suy từ trạng thái preparation `ready`.

| Facts | Grain và ràng buộc chính |
|---|---|
| `entities` | `entity_key` gồm split/business/kind và source code hoặc tên chuẩn hóa giữ dấu; giữ `source_code`, tên, aliases, base unit, source ref. Hai mã khác nhau giữ hai entity dù cùng tên. |
| `order_parties` | Một hàng/đơn, FK tới orders và customer; payable và payment method riêng với line revenue. Named walk-in trong shop có mã khách vẫn không được đếm/xếp hạng như một khách. |
| `line_items` | Một hàng/line key đã qua dedup/lineage; product FK, base quantity, selling quantity và cost cho một selling unit tách riêng. Lỗi lượng bán/cost không làm mất base quantity hợp lệ. |
| `payments` | Một hàng/payment key, order FK, ngày thu, số tiền của lần thu. Không JOIN bảng này vào SUM doanh thu. Orphan, duplicate key, amount/date lỗi chặn payment metrics. |
| `purchases` | Một hàng/purchase header key, ngày phiếu, status và tổng tiền phiếu. Không cộng header lần nữa theo số dòng nhập; nhận cả phiếu nhập đầu kỳ hợp lệ. |
| `stock_balances` | Một hàng/event balance hoặc một hàng/product ở declared snapshot. Balance là trạng thái sau event, không phải movement để SUM. |
| `lots` | Lot ID duy nhất, product FK, purchase FK, expiry và lượng nhập cơ bản. Không suy giá vốn bình quân/FIFO từ bảng này. |
| `lot_allocations` | Khóa order–line–lot, lượng bán/đã trả cơ bản, ngày trả có evidence. Product phải khớp line/lot; tổng phân bổ khớp quantity của các dòng không huỷ có lô. |

Name matching cho truy vấn S3 dùng source code hoặc alias có trong nguồn, chuẩn hóa
hoa/thường, khoảng trắng và dấu câu, giữ dấu tiếng Việt. Nhiều ứng viên giữ ambiguity;
query không rõ entity không được biến thành tổng shop. Quyết định name join mất dấu
của S1/S2 tiếp tục giữ phạm vi cũ, không trở thành fuzzy query resolution.

Ranking quantity lấy `base_quantity`, bỏ fee/adjustment và đơn huỷ/trả theo rulebook.
Customer ranking/count áp rule source-code identity; whole-shop debt vẫn có walk-ins.
Top-k giảm theo metric, bằng nhau theo source code tăng dần, thiếu mã theo tên nguồn;
không dedup hai tên bằng nhau nếu chúng thuộc hai mã. Scalar series S2 và boolean
trend giữ thứ tự câu hỏi, dùng tổng chính xác trước rounding để so sánh.

Receivable giảm payments tới cutoff theo từng đơn, cộng phần dương, giữ đơn trả hàng,
loại đơn huỷ/COD và kênh marketplace đã nhận diện. Collection dùng ngày payment,
chỉ debt orders không huỷ, sau ngày mua; giữ đơn kỳ trước/đơn trả, không trừ refunds.
Payment methods/kênh ngoài vocabulary đã kiểm chứng giữ unsupported. Payment XLSX chỉ nhận serial theo workbook epoch có evidence và các format hiển thị whole-second đã hỗ trợ; metadata giữ basis/warning, không tự suy clock cho ô ngày thường. Gross-profit
ranking dùng `line_net_amount - selling_quantity * line_unit_cost`; phí phục vụ vẫn
thuộc whole-shop revenue, không thành product trong ranking.

Stock event lookup lấy balance cuối theo timestamp tới cutoff; timestamp bằng nhau
mà balance khác nhau giữ ambiguity. CSV snapshot cần README chỉ rõ cuối ngày kết thúc,
base units và product keys kiểm tra được; snapshot chỉ dùng đúng ngày đó. Nearest-lot
lookup chỉ bật tại declared export end, còn tồn và chưa quá hạn; allocations/returns
phải đối soát được. Không chứng nhận generic movement/history, đổi đơn vị lớn, giá trị
kho/lô, FIFO, weighted purchase price hoặc net-profit/expense workflow.

Planner v14/context v10 và QueryPlan v13 mở rộng cùng grammar hiện có bằng grouped
aggregations/limit và các metric trên; scalar decoder cũ không nhận nhầm business
operators. Server giữ identity, rule refs, unit/status/axis và required fields; LLM
không được xuất số đáp án, SQL hay code. Metric contracts v5 và semantics v6 đọc cùng
rulebook parser v8; parser hash tính cả finite business-rule clauses. Mỗi row/fact còn
source ref; cache checksum và toàn bộ normalizer dependencies vẫn được kiểm tra.

Phạm vi time coverage cho metrics mới còn bảo thủ: ngoài purchase dùng extent riêng,
một số preflight payment/balance vẫn phụ thuộc span sales của export. Chưa chứng nhận
payment-only/stock-only packages hoặc các kỳ tách khỏi span đó. Không dùng kết quả trên
subset hiện tại để tuyên bố đã tái lập đầy đủ lịch sử kho/công nợ trên mọi nguồn.
