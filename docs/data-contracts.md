# Data contracts — Phase 1

Contract version: `data-contracts-v1`. Taxonomy: `question-taxonomy-v1`.
Đây là đặc tả đầu vào cho S1; trạng thái triển khai nằm trong
[roadmap](../roadmap.md), không suy ra từ việc có tài liệu hoặc fixture.
Các hợp đồng C–F trong roadmap và rulebook/README/calendar đúng split là nguồn đối chiếu.
Readers, profile và business catalog đã có implementation theo Phase 2;
Prepared facts S1 cho một CSV order-line đã có trong `src/data/normalization/`;
rule profile, contracts revenue/count, temporal resolver và preflight đã có API.
Entity mapping rộng, QueryPlan và metric execution vẫn chờ các slice/phase tương ứng.

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
| `Coverage` **S1** | `source_refs: SourceRef[]`, `fact_type: string`, `time_axis: string`, `observed_span: Span/null`, `available_span: Span/null`, `availability_basis: string[]`, `snapshot_at: Timestamp/Day/null`, `history_available: true/false/null`, `parse_status: enum`, `uncertainties: string[]` | Unknown không trở thành full coverage. Available span cần căn cứ; quan sát một dòng đầu/cuối không chứng minh không thiếu giai đoạn. Snapshot chỉ áp dụng tại mốc được xác nhận. Extension: coverage từng partition/file và lịch sử tái lập. |
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
`prepared-sales-v1`; dependencies/quality summary nằm trong bảng `metadata`. Money là TEXT
Decimal chính xác, qua API trả Decimal; không dùng SUM/AVG/CAST REAL cho tiền trong SQL.
`orders` giữ raw ID/namespace key, ordered_at, canonical status, mapping_status.
`order_lines` giữ key riêng, order FK, net_amount nullable khi lỗi có issue, row_role,
source_values trước parse (sau phục hồi header), event_at cho return đã có nguồn ngày.
Return item nằm riêng role với sale item; event_at chưa biết giữ null, chưa chứng nhận stock history.
`lineage` map mỗi raw row tới line key hoặc disposition header/title/blank/summary/quarantined;
bản trùng trỏ về line giữ lại. `issues.detail` có code, raw value, source ref, affected metrics và severity.

Mapping chỉ mở trên một CSV resolved khi README xác nhận order-line grain và nghĩa các cột.
Header inheritance/negative-return conventions cần README; dedup/whole-order return cần
operator rulebook đã nhận diện. Mapping/locale/coverage có căn cứ và hashes; luật chưa hiểu
hoặc shape khác trả unsupported. Day/timestamp giữ precision thời gian nguồn, không cắt theo as-of.
Full available_span cần phát biểu coverage đầy đủ trong README và không mâu thuẫn nguồn;
observed min/max và README end hint riêng không chứng minh availability.

## Semantic metadata S1 — implementation boundary

`Split.rules` giữ rule profile, `Split.calendar` là tuple `CalendarDay` đã được loader
kiểm tra; contexts cùng split chia sẻ chúng. Industry override chỉ chọn từ khai báo
`Ngành: ...` trong README; scope chưa xác định khi có override liên quan giữ ambiguous.
Rule parser là tập clause/operator hữu hạn, không phải bộ hiểu rulebook tự nhiên tổng quát.
Dedup/header inheritance/whole-order return ở preparation dùng cùng profile này.

`prepare_semantics(context)` tạo `.semantics`, lưu cùng payload catalog hiện có, ngoài
raw catalog cache và facts metadata. Có rules/source refs, contracts, schema facts,
coverage, versions và revision; không mang cutoff câu đầu hay labels. Capability metric
vẫn unsupported khi chưa có executor/verifier, dù contract đã resolved.

`MetricContract` là dataclass tại `src/semantics/metric_registry.py`; `.summary()` trả JSON.
`revenue` và `order_count` có required fields/status policy riêng. Contract tiền ghi rõ
nearest đồng với tie chưa có luật thì abstain; không tự dùng ROUND_HALF_EVEN/HALF_UP
cho mọi khoản tiền. Các phép tính và serializer cuối còn thuộc Phase 6–7.

`TimeWindow` giữ requested bounds inclusive, as-of/cutoff và rule refs; `contains()` xét
ngày local của timestamp nguồn, giữ offset và bao trọn phần giây lẻ ngày cuối. Không
chuyển timezone theo máy hoặc hỗ trợ ca khi chỉ có date. Tết và trước Tết kiểm tra calendar
liên tục cho cả kỳ gốc và kỳ so, không suy từ cột holiday hoặc nguồn bên ngoài.

`preflight` chỉ nhận aggregate revenue/count toàn business và biểu thức temporal có
cấu trúc. Nonempty filters/metric/operator chưa hỗ trợ dẫn tới abstain. Kết quả gồm:
`decision`, `reason`, `contract`, `window`, `effective_window`, `evidence_refs`, `issue_refs`.
`eligible_to_execute` không có answer value và chưa chứng nhận answerability cuối.
Schema thiếu field canonical là engine chưa hỗ trợ, không là proof dữ liệu gốc thiếu.
Available span cần completeness có căn cứ; confirmed end riêng chỉ chứng minh kỳ sau
ngày kết thúc, không cấp quyền trả tổng trong kỳ. Source end mâu thuẫn hoặc parse ngày
chưa đủ giữ unknown/conflict; issue chỉ chặn đúng metric phụ thuộc.

Tests và bằng chứng nằm tại `tests/test_normalization.py` và roadmap; C01–C30 trong fixture
vẫn là specifications toàn capability, không đổi thành passed chỉ từ một phần preparation.
