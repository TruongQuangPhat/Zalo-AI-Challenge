# Capability matrix — Phase 1

Mỗi dòng là cấu trúc + metric, không phải allowlist business IDs.
Authority phải lấy từ rulebook/README/calendar runtime, có hash.
`planned` là đặc tả; runtime chưa hỗ trợ thì **abstain**, không tự kết luận unanswerable.
Chỉ khi có bằng chứng dữ liệu gốc thiếu mới trả unanswerable.

| ID / ưu tiên | Input shape và metric | Required facts/fields/rules/operators | Trạng thái / evidence | Acceptance / gate bật | Fallback và giới hạn |
|---|---|---|---|---|---|
| OUT / P0 | Typed answer mọi input shape | Existing output validator + exact JSON/Decimal serializer; evaluator riêng | supported ở output/evaluation; Phase 0 report và tests/test_contracts.py, test_evaluator.py | C28–C29; tests hiện có; không chứng minh metric | Dòng lỗi theo hợp đồng hiện có; chưa xác minh evaluator BTC tương đương hoàn toàn |
| SAFE / P0 | Operator/schema chưa hỗ trợ | MockPlanner mặc định; local S1 validator/preflight khi bật model; per-question error boundary | supported ở safe abstention; tests/test_split_runtime.py | C30; label isolation và failure continuation | abstain cho unsupported intent/schema/timeout; finite scope/time guards, không chứng minh hiểu mọi câu |
| INPUT / P0 | CSV/XLSX/SQLite raw ingestion và catalog | Strict readers, raw types/locators, structural profiles, source hashes, context/business và cache có checksum | supported ở raw ingestion/profile có giới hạn; tests/test_ingestion.py, test_catalog.py; reports/ingestion_verification.json | Gate A–E Phase 2; đọc nguồn không bật metric | Semantic roles/coverage chưa xác lập giữ unknown; distinct/relations vượt budget giữ not_checked; views chưa đọc |
| PREP / P0 | Một CSV order-line hoặc header-only count có README/grain/fields và rulebook được nhận diện | orders/lines, scalar parsers, status/group/dedup/return evidence, lineage/issues, SQLite exact Decimal và cache | supported ở preparation S1; tests/test_normalization.py và reports/normalization_verification.json; query-time executor tách riêng | Partial preparation coverage của C01–C06/C12/C27/C30 và summary/error; không chứng nhận toàn bộ metric cases | unsupported shape/authority không tạo facts; lỗi có affected metrics; coverage theo observed export khi parse đủ, README complete span ưu tiên; Excel/SQLite/JOIN/units/domain chưa bật |
| SEM / P0 | Rules/contracts/preflight cho CSV order-line S1 | Runtime rule profile có source refs; canonical schema, metric-specific quality và coverage; structured windows | supported ở API semantic/temporal/preflight; tests/test_semantics.py, tests/test_temporal.py và reports/semantics_verification.json; không phải numeric execution | Gate A–E Phase 4 trong phạm vi S1; eligibility/outside/unknown/partial/error và source/rule invalidation | Default mock; local S1 nối validator/resolve/preflight/executor; entity filters/operator lạ abstain; unknown chỉ khi chưa đủ nguồn/parse; observed_order_dates là basis được chấp nhận |
| S1-CSV-REV / P0 | CSV phẳng có order key, line amount/status/date, revenue | orders+sales; role gate; source-specific date/money/status; grouping; exact dedup; SUM Decimal; per-metric coverage | supported hẹp; 3 câu revenue train-dev đúng qua LLM thật ở cả hai lượt; preflight theo export/README, giữ quality/scope gates | tests/test_planner_execution.py: Decimal/status/dedup/coverage/scope/isolation/future facts; model evidence tại reports/planner_verification.json; chưa nghiệm thu toàn bộ C01–C06; kết quả train numeric theo revision trong report | abstain khi đọc/parse thiếu, mâu thuẫn hoặc unknown role; kỳ ngoài export unanswerable, partial lấy giao |
| S1-CSV-COUNT / P0 | Cùng shape, order_count | Order key/date/status có consistency; distinct/group restoration; không dùng source row count | supported hẹp; 3 câu count order-line train-dev đúng qua LLM thật ở cả hai lượt; preflight theo export/README | tests/test_planner_execution.py: distinct order/status/metric-specific quality; model evidence có repeated count/zero/future facts tại reports/planner_verification.json; numeric train và latency được ghi riêng trong report theo revision | abstain; không mở count cho ledger không mã |
| SNAPSHOT-POINT / P0 M1 | Một bảng POS XLSX bốn cột, một product tại declared snapshot | README snapshot/date/base unit; unique accent-preserving identity; exact Decimal quantity; read-only SELECT; explicit requested base unit | supported hẹp tại today=declared snapshot; model train-dev M1 hai lượt đúng giữ phạm vi lịch sử, snapshot regression trong 271 tests; reports/planner_verification.json; không là S4 đầy đủ | C12/C17/C18/C30 trong scope lookup; không chứng nhận stock history, cost, conversions hoặc lots | abstain sai date/unit, conflict/ambiguity/invalid source; historical coverage vẫn unknown; không fuzzy/deaccent, không JOIN |
| S1-HEADER / P0 mở rộng | Một CSV header-only, order_count | README xác nhận một dòng/một đơn không detail; order key/date/status nhất quán; count distinct canonical order; observed coverage | supported hẹp cho count: 6 câu train-dev đúng ở cả hai lượt LLM thật; tests/test_planner_execution.py và planner report | Không tạo line facts giả; Cộng/Tổng ngày với ID/date/status trống là summary, không thành order; conflict/date/status lỗi chặn count; revenue vẫn unsupported trên shape này | Không suy tiền hàng từ payable; JOIN/header revenue và source summaries chưa nhận diện vẫn abstain |
| S1-RELATIONAL / P0 mở rộng | Header/detail CSV/XLSX/SQLite, revenue/count | Reader tương ứng; orders N:1 detail; keys/orphan/cardinality checks; preaggregation, no fan-out | planned, runtime unsupported; key mappings hiện là candidates ngoại trừ phạm vi EDA POS cũ | S1 cases + C09; multi-source reconciliation | abstain; đọc được bảng chưa đủ bật JOIN |
| S1-EXCEL / P0 mở rộng | Nhiều sheet/tháng/phần, header nhiều dòng/kế thừa | Header/group/continuation recognition; all sheets; year hints; native cell types; summary/role gate | planned, runtime unsupported | C01–C06, C09 khi JOIN, C18, C27 khi cache; P1-SUMMARY/P1-ERROR | abstain nếu thiếu sheet/unknown boundary; không fill tiền/SL |
| LEDGER / P1 | Sổ no-order, revenue/quantity | Ledger-record grain, giữ raw copies, bỏ summary; amount/date/status rõ; unit conversion cho quantity | planned, runtime unsupported | C02, C05, C13, C18, P1-SUMMARY | Revenue abstain tới khi kiểm chứng; count order/customer chỉ unanswerable sau xác nhận thiếu thông tin gốc |
| S2-DIFF / P0 | Cùng hai shape CSV S1; chênh lệch hai kỳ revenue hoặc order_count toàn business | Cùng QueryPlan, hai independent windows/aggregates, difference current_ref/baseline_ref; runtime difference rule, same metric/unit/scope; exact Decimal và round-once | supported hẹp ở deterministic executor và local LLM CUDA; 318 tests; reports/s2_verification.json: 12/12 expected behaviors synthetic (evaluator 7 correct/0 wrong/5 abstain), S1 regression 12 correct/0 wrong/1 abstain; numeric CPU/native train S2 chưa nghiệm thu | Signed/zero/large precision/MTD/future facts, refs/reversal/scope/unit, per-window partial/outside/unknown, unresolved rule và S1/snapshot regression | Growth hẹp xem S2-GROWTH; ratio/average/entity/JOIN chưa bật; guard directional và literal periods hữu hạn; explicit month + relative previous month abstain; không missing→0 |
| S1-XLSX-LINE / P0 hẹp | Một sheet XLSX order-line header đơn lặp, README mappings theo file; whole-business revenue/count | Existing reader, exact native numeric/text parsing, all rows/statuses, summary exclusion, dedup, lineage; optional conversion lookup unused, no raw JOIN | supported hẹp; 343 tests; source native 39.006 orders/62.451 lines/443 summary/0 issues, native growth evidence ở reports/s2_verification.json.native_growth_continuation | Filename/section authority, grain/required fields, role/mapping/status/coverage gates; numeric tiền từ summary chỉ là format evidence; Topping có line amount riêng theo product-row README | Multi-sheet/unclassified sources/unknown roles không bỏ qua; không header revenue/quantity conversion/ranking/physical-shipment inference |
| S2-GROWTH / P0 hẹp | Hai kỳ revenue toàn business, baseline dương; prior-month hoặc hai tháng/năm tường minh | Cùng QueryPlan percent_change + current/baseline refs; runtime growth/rounding rules, preflight mỗi kỳ; Decimal sums + exact rational division; output percent | supported native CUDA hẹp: **8 correct/0 wrong/0 abstain mỗi lượt, hai fresh workers**; 6 giá trị không dùng triển khai trước selection/inference + 2 exposed; predictions bằng nhau; S1 regression 12/0/1 | 343 tests; raw XML reference/wiring trước gold; native operands/windows và gold/actual tại reports/s1_s2_results.md; synthetic 15/15 behaviors, 9/0/2 trên 11 fixture có gold | Baseline 0/âm/undefined/half-tie hoặc authority thiếu abstain; unknown/outside riêng; không ratio/share/average/JOIN/CPU/Blackwell certification hoặc PA toàn dataset |
| TEMPORAL / P0 mở rộng | Nhiều kỳ, MTD, ratios, weekday/lunar | Per-window coverage, runtime calendar/as-of, independent scopes, DAG + denominator/undefined policy | Structured resolver day/week/month/MTD/N days/quarter/year/Tết đã kiểm chứng; signed difference hai kỳ xem S2-DIFF; growth hẹp xem S2-GROWTH; ratios/weekday average vẫn planned | C17, C19–C20, C22, C26–C27; tests/test_temporal.py chỉ chứng nhận phần resolver | abstain cho ambiguity/divide-by-zero chưa có rule; không missing→0; chưa có ca hoặc D…D+N |
| RANK / P1 | Product/customer/staff ranking/list | Identity resolution, metric-specific scope, base units, stable code tie-break, output ordering | planned, runtime unsupported | C13, C21–C22, C25, P1-IDENTITY | abstain khi aliases collision hoặc không đủ metric; không lấy mọi số trong tên làm attribute |
| DEBT / P1 | Payment history, receivable/collection | Orders/payments separate; cutoff by payment date; COD/status policy riêng; per-order positive balance; earlier orders retained | planned, runtime unsupported | C09, C12, C15, C18, C26 | Source missing proof → unanswerable; engine/parser thiếu → abstain |
| STOCK-COST / P1 | Stock event/snapshot, COGS/inventory value | Snapshot timestamp or ordered stock events; catalog vs line cost bases; unit conversions | SNAPSHOT-POINT đã có lookup hẹp; event/history/cost/valuation/conversion vẫn planned | C10–C14, C17–C18, C27 | Không dùng snapshot khác mốc, không SUM balances, không FIFO tự suy |
| LOTS / P1/P2 | Pharmacy lots/expiry | Lots/purchases/sale allocations/returns; lot price and base-unit quantity; no many-to-many fan-out | planned, runtime unsupported | C09, C12–C14, C26 | abstain nếu chưa tái lập history; snapshot không đủ lịch sử → unanswerable khi đã chứng minh |
| MIXED-ROLES / P2 | Embedded topping, vouchers/points/ship/fees | Per-row subtype, source-specific split/allocation and reconciliation; metric-dependent role policy | planned, runtime unsupported | C07–C08, C18 | Giữ unknown và abstain metric phụ thuộc; không đưa mọi dòng vào parser sale_item |
| DOMAIN / P2 | Repair, trade-in, installments, warranty, membership | Domain facts/keys/time axes and denominators theo runtime authority; task/visit/machine/contract tách grain | planned, runtime unsupported; không có khảo sát test mới | C23–C25 | abstain; các synthetic cases chỉ đặc tả nguy cơ đã có trong roadmap |

Input shape chưa được nhận diện là unknown. Muốn mở capability phải lưu test/run evidence,
required fields đủ, coverage/units/grain đúng, kết quả thực thi đạt và output hợp lệ.
Một metric cấp shop có thể dùng header độc lập khi kiểm chứng đúng nghĩa, dù item parser chưa có;
phải ghi chính xác giới hạn, không nâng cả ngành thành supported.

Ngày 03/11: theo quyết định mới của người dùng, min/max order dates đã parse đầy đủ được dùng làm available_span của export; README complete span ưu tiên, mâu thuẫn/đọc thiếu vẫn chặn. Metadata ghi observed_order_dates và giới hạn completeness. Decoder S1
chỉ cho một aggregate/window, có hint hữu hạn cho metric và relative-time literals; validator bác
requested bounds sai (gồm quý/tháng/ngày/range tường minh) trước khi coverage clipping che lỗi, cùng scope cues bị model bỏ. Một số aliases label/paragraph exceptions được kiểm tra hữu hạn; không phải full-document policy interpretation.
Không phải chứng nhận mọi paraphrase/alias/negation hoặc latency toàn dataset.

Nghiệm thu S1 hẹp trên host: mỗi lượt subset dev 12 correct/0 wrong/1 abstain (13 câu), hai lượt giống nhau; 42 model calls gồm synthetic, 0 timeout, generation median 25,47 s/p95 48,81 s. Bằng chứng lịch sử theo revision tại planner report.

Đóng gói ngày 03/11/2026: Docker LLM CPU và CUDA/GTX 1650 đã đạt hai lượt/backend
trên cùng 13 câu đã khóa, mỗi lượt 12 correct/0 wrong/1 abstain, không missing/malformed;
predictions khớp cả bốn lượt, source/model/input hashes giống nhau, GPU offload 29 layer.
Labels không có trong container; evaluator host chỉ đọc sau inference. 285 tests và
lint/format/lockfile đạt. [Docker LLM report](../reports/docker_llm_verification.json)
ghi latency/RSS/VRAM và giới hạn so sánh sau laptop reset. Đây là bằng chứng vận hành
các capability S1 đã bật, không mở thêm metric/shape hoặc chứng nhận PA đại diện,
SLA toàn dataset hay Blackwell.

S2 lát cắt đầu (source cuối trong S2 report): hai windows/aggregates cùng metric và một
signed difference; authority/requirements/time bounds/refs/scope/unit phải đạt, preflight
từng window trước numeric query. Model CUDA offload 29 layers; full subset dev 15 gồm
13 S1 regression và 2 S2 percent unsupported, không phải numeric train S2 acceptance.
Hai CPU synthetic attempts timeout câu đầu; worker-unavailable abstentions không chứng minh
scope handling. Chỉ nghiệm thu S2 LLM trên CUDA + Decimal executor, không CPU/GPU parity,
SLA hoặc Blackwell. Source/model/input hashes và isolation đã kiểm tra; labels chỉ evaluator
host đọc sau inference. Reports S1 ở trên giữ phạm vi revision lịch sử.

Đối chiếu câu hỏi/gold/runtime mong đợi/actual và ý nghĩa các tỷ lệ tại
[reports/s1_s2_results.md](../reports/s1_s2_results.md). Behavior pass không bằng correct:
S1 đúng 12/13 (92,31%) toàn subset, native S2 0/2 được trả lời. Bộ synthetic 12 case có
2 nhãn diagnostic unknown/share chưa phải gold nghiệp vụ; không dùng tỷ lệ này chứng nhận
accuracy S2 train. Các giá trị gold được bổ sung sau inference, chỉ trong luồng đánh giá/báo cáo.

Cập nhật ưu tiên mới nhất: S2 revenue growth đã có numeric trên **8 câu native common-dev**,
hai lượt CUDA đều 8/0/0; S1 giữ 12/0/1. Các đoạn S2 0/2 abstain hoặc chỉ synthetic ở trên
là checkpoint trước growth. Đọc continuation/source hashes và bảng mới nhất ở đầu
[reports/s1_s2_results.md](../reports/s1_s2_results.md); không dùng 100% của 8 dev đã chọn
để suy PA đại diện, model CPU growth hoặc toàn S2 đã hoàn tất.

Report JSON đọc trực tiếp: [S1](../reports/s1_results.json),
[S2](../reports/s2_results.json). `readiness` của S2 là verified narrow native revenue growth
CUDA; không đồng nghĩa full S2 đã hoàn tất. `questions[]` và synthetic giữ gold/expected/actual
cả hai lượt; không tính abstain đúng fallback thành correct.
