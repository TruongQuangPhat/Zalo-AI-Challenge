# Data contracts — Phase 1

Contract version: `data-contracts-v1`. Taxonomy: `question-taxonomy-v1`.
Đây là đặc tả đầu vào cho S1; trạng thái triển khai nằm trong
[roadmap](../roadmap.md), không suy ra từ việc có tài liệu hoặc fixture.
Các hợp đồng C–F trong roadmap và rulebook/README/calendar đúng split là nguồn đối chiếu.

## Kiểu chung và phạm vi S1

- `ID`: chuỗi không rỗng; entity key luôn gồm split, business, entity kind và source key.
- `Day`: ISO `YYYY-MM-DD`; `Timestamp`: giữ cả giờ và timezone/quy ước giờ nguồn.
- `Money`/`Quantity`: Decimal hoặc scaled integer khi tính; JSON number khi xuất.
  Raw text không được mất. Không parse tiền qua float; không lấy tolerance làm rounding policy.
- `Span`: `{start: Day, end: Day}`, gồm hai đầu, start ≤ end.
- `SourceRef`: `{split, business_id, file, sha256, table, row, column}`.
  File tương đối với split, không thoát root; row là tọa độ vật lý 1-based, không phải order ID.
  `row`/`column` có thể null với tham chiếu cả bảng/file; SHA-256 là 64 hex.
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
