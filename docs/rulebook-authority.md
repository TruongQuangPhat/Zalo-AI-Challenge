# Rulebook authority S1–S5

Catalogue này là diễn giải và chỉ mục nguồn; không thay rulebook phát hành. Không có
model run, native evaluation, gold, public questions hoặc feedback predictions trong
phiên này. `resolved` chỉ có nghĩa clause được nhận diện trong **scope đang xét**.
`understood` mô tả khả năng diễn giải toàn clause, độc lập scope; `execution` mô tả
operator hiện có. Eligibility của business vẫn cần fields/grain/keys/units/coverage,
quality, validated QueryPlan và executor/verifier. Đọc catalogue không bật capability.

## Nguồn và cách đếm

T = `dataset/train/rulebook.md`, P = `dataset/public_test/rulebook.md`; các số ở bảng
là line 1-based trong đúng source hash dưới đây; đoạn soft-wrap giữ thêm `line_end`
và quote nguyên các dòng vật lý. Mỗi **hàng bảng** (kể cả 8 format rows
§9) hoặc **đoạn/bullet không rỗng ngoài bảng** là một đơn vị; bỏ heading, bảng header,
separator. Câu nhiều vế giữ nguyên một đơn vị và toàn bộ quote, không bỏ phủ định,
ngoại lệ, lượng từ hay tham chiếu. Một đơn vị có thể sinh nhiều rule IDs; nhiều nguồn
có thể cùng rule ID. Đây là số đơn vị kiểm kê, không phải phần trăm coverage/accuracy.

- train: **87 units**, 91 rule IDs có mặt; rulebook SHA-256 `740267bccba29df70f745bc39968bbec5b3e62dada2c858c45314f3ec723d113`.
  Calendar SHA-256 `c2579905e8b4eb152a4833b05f1f72de3e3d2c517b5d610d7664aeeb2d6a75b3`; chỉ tra Tết từ calendar cùng split, không suy coverage.
- public_test: **109 units**, 113 rule IDs có mặt; rulebook SHA-256 `959ebb63b709fad68f7e8a339cde955c96b6e9af2e48141db3ad5a2429510e10`.
  Calendar SHA-256 `c2579905e8b4eb152a4833b05f1f72de3e3d2c517b5d610d7664aeeb2d6a75b3`; chỉ tra Tết từ calendar cùng split, không suy coverage.

Có **116 rule IDs** trong hợp hai tài liệu. Bảng dưới giữ cả clause chưa có
operator. Hai calendars byte-identical tại audit này không tạo quyền dùng chung luật.
Public bổ sung return-after-period cho `time_axis`: cùng ordered_at + recorded whole
order return, giữ cả câu giải thích. `delivery_batches` thực sự khác: public có cutoff
đến hết as-of; train không tự thừa hưởng cutoff đó. `weighted_purchase_price`,
`large_unit_stock`, `day_sale_price` chỉ ở train. Các luật chỉ ở P không thành T default.

Matching dùng toàn value sau chuẩn hóa hoa/thường/dấu Markdown/khoảng trắng bằng
vocabulary hữu hạn hoặc SHA-256 của toàn canonical clause trong `business_rules.py`.
Signature là bộ nhận diện wording đã rà, không chứng minh paraphrase bất kỳ tương đương.
Mọi signature tra được từ quote/line/hash trong audit; sửa chữ nghĩa/ngoại lệ không tự
khớp signature cũ. Clause lạ còn trong `RuleProfile.clauses` với `understood=false`;
guard hữu hạn chặn amendments liên quan, chưa là entailment checker cho mọi văn bản.

## Precedence và provenance

- Điều kiện câu hỏi được ưu tiên khi operator/source đủ; server không cho câu hỏi tạo
  field/coverage, bỏ quality gate hoặc sinh SQL/code. Điều kiện chưa hỗ trợ giữ abstain.
- Ngành xác minh từ `Ngành:` hoặc mô tả thuốc/lô tường minh trong README. Không suy từ
  business ID/tên hàng. Ngành chưa biết: ambiguity; ngành khác: not_applicable. Không
  lấy luật split khác để lấp thiếu. Rulebook là normative; README là descriptive của
  encoding/grain/fields. Mâu thuẫn chưa phân giải chặn, không chọn kết quả thuận tiện.
- `user_override` 03/11: coverage sales từ min/max ngày **mọi đơn** đã parse đủ, kể cả
  huỷ/trả/đang giao; README full span ưu tiên. Ledger có basis riêng. Rỗng/parse thiếu/
  conflict không cấp span; không lấy lịch. Không sửa evidence lịch sử business.
- `user_override` 04/11: grouping tiền từng cụm 3 nhất quán khi thiếu locale/README;
  explicit README ưu tiên, mixed/error chặn, giữ basis/warning.
- `user_override` 04/11: name join mất dấu chỉ khi unique catalog code; tên có dấu sai
  không bị bỏ dấu ép khớp, dòng có mã chỉ join mã. Không mở thành fuzzy query matching.
- `policy` có sẵn: purchase export không có full span chỉ dùng extent ngày phiếu đã
  đọc đủ, ghi `observed_purchase_export_dates_policy`; đây không phải luật BTC hoặc
  quyết định mở rộng sales override sang payment. Payment theo kỳ cần README full span;
  chưa có authority thì `payment_period_coverage_unknown`, không mượn sales min/max.

## Rule → semantics → operator → requirements → tests

Bảng ghi canonical operator/parameters theo từng source (không chép lại rulebook).
`core` là required fields/grain/coverage trong MetricContract tương ứng và quality
mapping tại normalization. `bounded` không có nghĩa mọi câu thuộc clause đều chạy được.

Consumer/test legend:

- **N**: `src/data/normalization/{sales,relational,entities,stock,values,store}.py`;
  `tests/test_{normalization,sales_sources,stock_snapshot,catalog}.py`.
- **T**: `src/temporal/resolver.py`; `tests/test_temporal.py`.
- **M**: `src/semantics/{rulebook,business_rules,metric_registry,answerability}.py`;
  `tests/test_semantics.py`, `tests/test_rulebook_authority.py`.
- **Q/X**: `src/agent/{context,plan,entities,execution,business_execution}.py`;
  `tests/test_planner_execution.py`, `tests/test_business_metrics.py`.
- **O**: `src/contracts.py`, `src/jsonio.py`, `src/evaluation/scorer.py` (code only);
  `tests/test_contracts.py`, `tests/test_evaluator.py` trên synthetic fixtures.
- **M/gap**: catalogue và scope/signature tests; chưa có executor hoặc behavioral numeric
  test của nghiệp vụ đó. C23–C25 trong acceptance fixture vẫn planned, không đổi trạng
  thái từ việc hiểu luật. Không dùng safe abstention để gọi domain supported.

| Rule ID | Scope; source lines | Canonical meaning (variants giữ riêng) | Operator boundary; requirements | Consumers/tests |
|---|---|---|---|---|
| `active_ingredient` | nha thuoc; T:89; P:97 | active_ingredient {"match":"exact_catalog_ingredient","combination":"separate_product_not_components","measure":"revenue"} | unavailable; verified pharmacy; exact drug catalog ingredient | M/gap |
| `answerability` | general; T:98; P:131 | answerability {"source":"released_only","insufficient_data":"unanswerable"} | bounded; distinguish missing source information from unavailable operator | M; Q/X |
| `as_of` | general; T:5; P:5 | question_as_of | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `as_of_recording` | general; T:5; P:5 | as_of_recording {"anchor":"question_as_of_date","future_events":"excluded_when_asof","returns":"recorded_status_even_if_return_after_asof"} | bounded; source event date and recorded order status | M; Q/X |
| `average_order_value` | general; T:48; P:50 | revenue_over_eligible_orders {"decimal_places":0,"mode":"half_up"} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `baby_size` | me va be; P:109 | baby_size {"source":"catalog_size","blank":"unclassified_even_if_name_has_number","share_denominator":"all_diaper_package_quantities_same_period"} | unavailable; verified mother/baby; size/brand/category/unit catalog | M/gap |
| `base_unit_total` | general; T:44; P:46 | base_unit_total {"sum":"base_quantity","filter":"catalog_base_unit","include_large_unit_sales":true} | bounded: one grounded product only; unique product identity; base quantity; conversion authority | M; Q/X |
| `busiest_day` | general; P:56 | busiest_day {"measure":"order_count","group":"calendar_day"} | unavailable; eligible orders; daily group/ranking | M/gap |
| `cancellation_rate` | general; T:18; P:19 | cancelled_over_all_placed_percent | bounded: only registered contracts and validated plans; core | M; Q/X |
| `cancelled_status` | general; T:13; P:13 | cancelled_status {"status":"cancelled","returned_is_cancelled":false} | bounded; recorded status | M; Q/X |
| `coffee_size` | ca phe; T:82; P:89 | coffee_size {"match":"name_suffix_(M)_or_(L)","case_sensitive":false} | unavailable; verified cafe industry; source name suffix | M/gap |
| `combined_topping` | ca phe; P:90 | combined_topping {"grain":"separate_product","allocate":["quantity","amount"],"same_as":"separate_topping_row","subtract_from_dish":true} | unavailable; verified cafe; README grammar and included amount; no double count | M/gap |
| `coverage` | general; T:101; P:134 | intersect_available | bounded: only registered contracts and validated plans; core | M; Q/X |
| `current_month` | general; T:32; P:33 | month_to_date {"comparison":"previous_month_same_days"} | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `customer_aov_ranking` | general; P:52 | customer_aov_ranking {"measure":"revenue/order_count","minimum_orders":3,"inclusive":true} | unavailable; customer identity; eligible orders; per-customer qualification | M/gap |
| `customer_cancellations` | general; P:53 | customer_cancellations {"measure":"cancelled_order_count"} | unavailable; customer identity; cancelled orders in order-date period | M/gap |
| `customer_identity` | general; T:46; P:48 | customer_identity {"coded_shop":"code_identity; uncoded_orders_excluded","uncoded_shop":"source_name_identity; different_letters_or_digits_distinct","walkins":"excluded","money_ranking":"revenue"} | bounded: source identities; customer revenue/order-count ranking; core | M; Q/X |
| `customer_loyalty` | general; P:51 | customer_loyalty {"measure":"distinct_purchase_days","identity_ref":"customer_identity"} | unavailable; eligible orders; customer identity; distinct day operator | M/gap |
| `customer_share` | general; T:17; P:17 | group_over_whole_business_percent | bounded: only registered contracts and validated plans; core | M; Q/X |
| `day_flow` | general; T:25; P:26 | day_flow {"kind":"flow","bounds":"one_calendar_day"} | bounded; source-local event date; relative anchor | M; T; Q/X |
| `day_sale_price` | general; T:70 | day_sale_price {"axis":"ordered_at","statuses":"all","basis":"before_line_discount_per_base_unit"} | unavailable; sale-line price/unit conversion; explicit date | M/gap |
| `dead_stock` | general; T:69; P:77 | dead_stock {"prior_sales":true,"period_sales":false,"statuses":["completed","shipping"]} | unavailable; all prior qualified sales and period completeness | M/gap |
| `debt_age` | general; T:57; P:66 | debt_age {"axis":"ordered_at","formula":"cutoff_day-order_day","today_age":0,"not":"due_date"} | unavailable; debt balance; order date; age operator | M/gap |
| `debt_balance` | general; T:54; P:63 | debt_balance {"basis":"order_payable_after_order_discount_including_shipping","payments":"through_end_cutoff_day","reduce":"sum(max(payable-paid,0)_per_order)"} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `debt_cod` | general; T:56; P:65 | debt_cod {"exclude":["cod","marketplace"],"also_exclude_from":"debt_age"} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `debt_collection` | general; T:58; P:67 | debt_collection {"method":"debt","exclude":["cancelled"],"new_debt":"placed_in_period; payable_minus_same_day_payments","collection":"payment_in_period_after_purchase_day; include_prior_orders_and_returns","refunds":"do_not_subtract"} | bounded: collections only; new debt and refund source mapping unavailable; core | M; Q/X |
| `debt_customer` | general; T:55; P:64 | debt_customer {"anonymous":"included_in_shop_total_excluded_from_customer_table"} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `debt_status` | general; T:53; P:62 | debt_status {"exclude":["cancelled"],"include_returned_if_positive":true} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `dedup` | general; T:111; P:145 | exact_within_order | bounded: only registered contracts and validated plans; core | M; N; Q/X |
| `delivery_batches` | vat lieu xay dung; T:95; P:103 | delivery_batches {"axis":"delivered_at","grain":["order","batch"],"include_later_returned":true,"cutoff":"explicit_period"}; delivery_batches {"axis":"delivered_at","grain":["order","batch"],"include_later_returned":true,"cutoff":"as_of"} | unavailable; verified construction industry; delivered batch key/date; no sales-status substitution | M/gap |
| `difference` | general; T:20; P:21 | current_minus_baseline | bounded: only registered contracts and validated plans; core | M; Q/X |
| `end_day_state` | general; T:26; P:27 | end_day_state {"kind":"state","history":"all_prior_events","cutoff":"end_of_day","not":"current_month"} | bounded; complete relevant history or exact authorized snapshot | M; Q/X |
| `expiry_horizon` | nha thuoc; T:88; P:96 | expiry_horizon {"bounds":"[D,D+N]","remaining":">0","cost":"own_lot_purchase_cost_per_base_unit"} | unavailable; verified pharmacy; remaining lots; dated returns; lot cost; units | M/gap |
| `explicit_month` | general; T:31; P:32 | whole_month | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `format_first` | general; T:131; P:165 | format_first {"validation":"before_value_comparison"} | bounded; strict answer validator | M; O |
| `generic_cost` | general; T:72; P:79 | generic_cost {"basis":"weighted_import_lots_at_sale","fifo":false,"last_purchase":false} | unavailable; weighted cost history; not permission to recompute gross-profit line cost | M/gap |
| `gross_profit` | general; T:73; P:80 | gross_profit {"formula":"revenue-sum(selling_quantity*line_cost_per_selling_unit)","recompute_from_purchases_or_lots":false} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `header_inheritance` | general; T:109; P:142 | inherit_order_headers | bounded: only registered contracts and validated plans; core | M; N; Q/X |
| `inclusive_range` | general; T:27; P:28 | inclusive | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `industry_precedence` | general; T:77; P:84 | industry_precedence {"priority":"industry_over_general","requires":"verified_industry"} | bounded; README industry declaration or explicit domain description | M; Q/X |
| `installment` | cua hang dien thoai; P:126 | installment {"kind":"debt","payments":"debt_collection_not_revenue","upfront":"not_installment","include_separate_debt_ledgers":true,"overdue":"due_date<cutoff AND paid<due"} | unavailable; verified phone; all contracts and linked payment histories; exact due schedule | M/gap |
| `invoice_adjustments` | me va be; P:108 | invoice_adjustments {"sign":"positive_output","axis":"invoice_date","statuses":["completed","shipping"],"roles":["voucher","points","shipping"]} | unavailable; verified mother/baby; nonmerchandise roles; invoice boundaries | M/gap |
| `item_group` | general; T:42; P:44 | exact_source_group | bounded: only registered contracts and validated plans; core | M; Q/X |
| `item_role` | general; T:41; P:43 | service_fee_in_revenue_not_product_group | bounded: only registered contracts and validated plans; core | M; Q/X |
| `large_unit_stock` | general; T:66 | large_unit_stock {"output":["whole_large_units","remainder_base_units"],"intermediate_remainder":false,"zero_remainder":0} | unavailable; stock at cutoff; conversion chain; ordered two-number output | M/gap |
| `last_n_days` | general; T:28; P:29 | last_n_including_today | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `ledger_grain` | general; T:112; P:146 | ledger_grain {"grain":"recorded_event","keep_identical_records":true,"infer_order_identity":false} | bounded; README confirms no order grain | M; N; Q/X |
| `list_order` | general; T:135; P:169 | list_order {"topk":"measure_desc_code_asc","expiry":"earliest_product_expiry_then_code","no_measure":"code_asc","compare":"set_and_order"} | bounded: ranking only; measure/expiry and source code; listing operators | M; Q/X |
| `lot_expiry` | nha thuoc; T:90; P:98 | lot_expiry {"remaining":">0","expiry":">=as_of","select":"earliest"} | bounded: nearest nonexpired remaining lot at declared export end only; core | M; Q/X |
| `mechanic_visits` | sua xe; P:118 | mechanic_visits {"measure":"visit_count","include_warranty":true} | unavailable; verified repair; mechanic identity and visits | M/gap |
| `membership_new` | me va be; P:110 | membership_new {"axis":"registration_date","include_never_purchased":true} | unavailable; verified mother/baby; complete member register | M/gap |
| `missing_year` | general; T:34; P:35 | latest_started_period | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `net_profit` | general; T:74; P:81 | net_profit {"formula":"monthly_gross_profit-all_monthly_expenses","include":["repairs","equipment_replacement"]} | unavailable; expense ledger; gross profit; monthly coverage | M/gap |
| `no_inference` | general; T:99; P:132 | no_inference {"missing_field":"no_substitution_unless_rule_authorizes"} | bounded; source fields; rule dependency evidence | M; Q/X |
| `note_status` | general; P:143 | note_status {"source":"order_note","no_status_mention":"completed","condition":"no_status_column"} | unavailable; README note role; order inheritance; explicit note/status parser | M/gap |
| `numeric_comparison` | general; T:132; P:166 | numeric_comparison {"mode":"exact","money_places":0,"percent_places":2,"float_noise_only":true} | bounded; Decimal tokens and exact executor; scorer unit fixtures | M; O |
| `order_count_status` | general; T:14; P:14 | include_statuses {"statuses":["completed","shipping"]} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `ordered_results` | general; T:118; P:152 | ordered_scalar_results {"ordering":"question_order"} | bounded: at most three scalar results in question order; core | M; Q/X |
| `output_boolean` | general; T:125; P:159 | output_boolean {"format":"JSON_boolean"} | bounded; strict typed output serializer/validator | M; O |
| `output_date` | general; T:124; P:158 | output_date {"format":"ISO_day_or_month"} | bounded; strict typed output serializer/validator | M; O |
| `output_list` | general; T:127; P:161 | output_list {"format":"ordered_JSON_array"} | bounded; strict typed output serializer/validator | M; O |
| `output_number` | general; T:122; P:156 | output_number {"format":"JSON_number"} | bounded; strict typed output serializer/validator | M; O |
| `output_percent` | general; T:123; P:157 | output_percent {"format":"percentage_number"} | bounded; strict typed output serializer/validator | M; O |
| `output_string` | general; T:126; P:160 | output_string {"format":"source_name"} | bounded; strict typed output serializer/validator | M; O |
| `output_table` | general; T:128; P:162 | output_table {"format":"columns_and_rows_numeric_cells"} | bounded; strict typed output serializer/validator | M; O |
| `output_unanswerable` | general; T:129; P:163 | output_unanswerable {"format":"optional_string_reason"} | bounded; strict typed output serializer/validator | M; O |
| `output_values` | general; T:118; P:152 | output_values {"number":"no_unit_or_percent_symbol","name":"source_value_without_label","multi_number_order":"question_order"} | bounded; typed output and result refs | M; O |
| `outside_coverage` | general; T:100; P:133 | unanswerable_outside | bounded: only registered contracts and validated plans; core | M; Q/X |
| `payment_revenue` | general; T:16; P:16 | payment_revenue {"axis":"ordered_at","method":"recorded_on_order","exclude_debt_from":["cash","transfer"],"cod_is_cash":false} | unavailable; order payment-method mapping; no payment fanout | M/gap |
| `peak_hour` | general; P:57 | peak_hour {"axis":"order_clock","interval":"[h,h+1)","measure":"order_count","output":"starting_hour_number"} | unavailable; order clock; eligible distinct orders; hour grouping | M/gap |
| `percent_change` | general; T:19; P:20 | current_minus_baseline_over_baseline_percent | bounded: only registered contracts and validated plans; core | M; Q/X |
| `phone_model_brand` | cua hang dien thoai; P:128 | phone_model_brand {"model":"name_before_capacity_GB_else_whole_name","4G_is_capacity":false,"brand":"first_word","iphone_brand":"Apple","tradein_is_sale":false} | unavailable; verified phone; resolved full product name and sold-device identity | M/gap |
| `phone_tradein` | cua hang dien thoai; P:125 | phone_tradein {"payable":"after_discount_and_tradein_including_debt","deduct_tradein_from_revenue":false,"period_statuses":["completed","shipping"]} | unavailable; verified phone industry; invoice and tradein keys; payment/debt separation | M/gap |
| `phone_warranty` | cua hang dien thoai; P:127 | phone_warranty {"numerator":"warranty_receipt_visits_in_quarter","denominator":"devices_sold_in_quarter_excluding_cancelled_returned","prior_sale_allowed":true} | unavailable; verified phone; IMEI; received_at; sold device quantities | M/gap |
| `precedence` | general; T:3; P:3 | precedence {"order":["explicit_question","verified_industry","general"],"question_creates_missing_facts":false} | bounded; supported question conditions; verified scope; quality gates | M; Q/X |
| `prescription_share` | nha thuoc; T:87; P:95 | prescription_share {"prescription":"catalog_flag","others":"nonprescription","measure":"revenue"} | unavailable; verified pharmacy; drug catalog flag; revenue and denominator | M/gap |
| `price_effect` | general; T:71; P:78 | price_effect {"increase":"new>previous","repeat_same_new_price":"first","after":"[D,D+N-1]","before":"[D-N,D-1]","statuses":["completed","shipping"]} | unavailable; ordered price history; quantity; both windows; conversion | M/gap |
| `product_order_ranking` | general; P:54 | product_order_ranking {"measure":"distinct_orders_containing_product"} | unavailable; eligible orders; unique product/order association | M/gap |
| `profit_ranking` | general; T:45; P:47 | profit_ranking {"measure":"total_gross_profit","ref":"gross_profit"} | bounded; selling-unit line cost; product identity | M; Q/X |
| `purchase_amount` | general; T:63; P:72 | purchase_amount {"grain":"purchase_header","axis":"purchased_at","exclude":["cancelled"],"include_opening_import":true,"supplier_ranking":"purchase_money"} | bounded: explicit purchase headers only; supplier ranking unavailable; core | M; Q/X |
| `purchase_sales_quantity` | general; T:64; P:74 | purchase_sales_quantity {"sold_statuses":["completed","shipping"],"unit":"same_as_purchase","formula":"purchased-sold"} | unavailable; purchase lines; sold base quantity; source conversions | M/gap |
| `quantity_ranking` | general; T:43; P:45 | quantity_ranking {"measure":"base_quantity","statuses":["completed","shipping"],"mixed_base_units_rank_together":true} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `ranking_ties` | general; T:136; P:170 | ranking_ties {"order":"measure_desc_code_asc_else_name","single":"first"} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `reason_type` | general; T:103; P:136 | reason_type {"reason":"optional_string","invalid_reason":"unreadable_line"} | bounded; typed output validator | M; O |
| `recorded_status` | general; T:5; P:5 | recorded_order_status | bounded: only registered contracts and validated plans; core | M; Q/X |
| `repair_tasks` | sua xe; P:116 | repair_tasks {"count":"one_per_visit_with_task","bare_oil":"engine_oil_not_gear_oil","bare_tube":"replace_not_patch","electrical_excludes":["bulb","battery"],"exclude_warranty":true} | unavailable; verified repair; task grammar; warranty classification; no per-task money inference | M/gap |
| `repair_visits` | sua xe; P:115 | repair_visits {"grain":"one_row_one_visit","include_warranty_in_count":true,"average_denominator":"charged_visits_only"} | unavailable; verified repair; visit grain; warranty classification; receipt total | M/gap |
| `repair_warranty` | sua xe; P:119 | repair_warranty {"numerator":"warranty_visits_in_period","denominator":"charged_visits_in_period","prior_original_visit_allowed":true} | unavailable; verified repair; visit date; charged/warranty classification | M/gap |
| `repeat_vehicle` | sua xe; P:120 | repeat_vehicle {"identity":"license_plate","minimum_charged_visits":2,"period":"same","output":"plate","exclude_warranty":true} | unavailable; verified repair; charged visits; plate identity | M/gap |
| `return_policy` | general; T:10; P:10 | whole_order_return | bounded: only registered contracts and validated plans; core | M; N; Q/X |
| `revenue_basis` | general; T:15; P:15 | line_net_amount {"excluded":["order_discount","vat","shipping_fee"]} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `revenue_status` | general; T:10; P:10 | include_statuses {"statuses":["completed","shipping"]} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `revenue_terms` | general; T:11; P:11 | revenue_terms {"metric":"revenue","not":"cash_received"} | bounded; net merchandise; sales status/order date | M; Q/X |
| `rounding` | general; T:133; P:167 | output_rounding {"money_places":0,"other_places":2,"unspecified_tie":"abstain"} | bounded: undefined ties abstain; AOV has explicit half-up policy; core | M; Q/X |
| `sheet_year` | general; T:113; P:147 | sheet_year {"year":"containing_sheet"} | bounded; verified sheet year; date role; native epoch | M; N; Q/X |
| `shift` | general; T:36; P:37 | order_clock_intervals {"morning":[21600,50400],"afternoon":[50400,79200]} | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `shift_cash` | general; P:18 | shift_cash {"axis":"paid_at","method":"cash","include_debt_payments":true,"sales_source":"orders_not_shift_report"} | unavailable; cash report grain; payment clock; refunds; no revenue substitution | M/gap |
| `source_alias` | general; T:108; P:141 | source_alias {"identity":"code_first","aliases":"source_evidence"} | bounded; split/business/kind namespace; unique source alias | M; N; Q/X |
| `staff_revenue` | general; T:47; P:49 | staff_revenue {"measure":"revenue","not":"order_count"} | unavailable; staff identity; order attribution | M/gap |
| `staff_workdays` | general; P:55 | staff_workdays {"measure":"distinct_workdays","qualifies":"at_least_one_sold_or_worked_order","include_warranty":true} | unavailable; staff identity; work/order/warranty attribution and date | M/gap |
| `status_domain` | general; T:110; P:144 | status_domain {"values":["completed","cancelled","returned","shipping"],"one_per_order":true} | bounded; source status decoding and consistency | M; N; Q/X |
| `stock_valuation` | general; T:67; P:75 | stock_valuation {"cutoff":"end_today","quantity_filter":">0","cost":"catalog_cost_per_base_unit"} | unavailable; stock and catalog cost at required basis; no lot-cost substitution | M/gap |
| `stockout` | general; T:68; P:76 | stockout {"cutoff":"end_day","balance":0,"products":"purchased_for_resale","exclude":"prepared_on_site"} | unavailable; end-day balances; catalog product type; no intraday-only inference | M/gap |
| `string_comparison` | general; T:134; P:168 | string_comparison {"ignore":["case","whitespace","punctuation"],"accept":["all_source_names","source_codes","name_with_source_phone"],"other_accents":"exact"} | bounded: source identity and scorer; not generic fuzzy query matching; source alias evidence; exact accent-preserving entity matching | M; O |
| `submission` | general; T:116; P:150 | submission {"record":["id","type","answer"],"score":{"correct":1,"wrong":-1,"abstain":0}} | bounded; strict JSON serialization; evaluator is separate | M; O |
| `supplier_count` | general; P:73 | supplier_count {"measure":"distinct_purchase_headers","exclude":"cancelled","not":"purchase_amount"} | unavailable; supplier identity; purchase key/status/date | M/gap |
| `table_semantics` | general; T:137; P:171 | table_semantics {"grain":"one_object_per_row","first_column":"object_name","measures":"question_order","compare_columns":"name_else_position","row_order":"ignored"} | bounded; typed table; named measure refs | M; O |
| `tet` | general; T:35; P:36 | lunar_tet {"start_month":12,"start_day":23,"end_month":1,"end_day":10} | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `threshold` | general; T:29; P:30 | threshold {"over":">","under":"<","from":">="} | unavailable; typed quantity/age/cost basis and threshold operator | M/gap |
| `time_axis` | general; T:12; P:12 | ordered_at {"return_status_cutoff":"recorded_even_after_period_and_as_of"} | bounded: only registered contracts and validated plans; core | M; Q/X |
| `unit_conversion` | general; T:142; P:176 | unit_conversion {"basis":"per_business_per_product","source":["conversion_table","README"],"absent_conversion":"base_unit_factor_1"} | bounded: preconverted quantity and declared base units; chain conversion unavailable; complete conversion authority; checked chain; no cross-shop factor | M; N; Q/X |
| `vehicle_identity` | sua xe; P:117 | vehicle_identity {"model_alias":"source_case_or_abbreviation","vehicle_count":"distinct_plates","include_warranty":true} | unavailable; verified repair; model alias evidence; plate parser | M/gap |
| `week` | general; T:30; P:31 | calendar_week {"first_weekday":0} | bounded: only registered contracts and validated plans; core | M; T; Q/X |
| `weekday_average` | general; P:38 | weekday_average {"denominator":"all_calendar_weekdays_in_period","include_zero_event_days":true} | bounded; same effective window and weekday on numerator/denominator | M; Q/X |
| `weighted_purchase_price` | general; T:65 | weighted_purchase_price {"axis":"purchased_at","status":"not_cancelled","unit":"purchase_unit","formula":"sum(quantity*price)/sum(quantity)"} | unavailable; purchase lines; month coverage; unit conversion | M/gap |
| `year_to_date` | general; T:33; P:34 | year_to_date | bounded: only registered contracts and validated plans; core | M; T; Q/X |

## Audit 44 README

Mỗi source dưới đây đã đọc toàn văn. Lines chỉ các đoạn/cột/notes của README tại đúng
hash, không phải suy từ giá trị giao dịch. File list, cột/key/grain, đơn vị, dates,
payable/cost/snapshot đều là declarations; mapper chỉ dùng khi nhận diện và kiểm được.
Không coi table empty/field blank/missing operator là proof không có dữ liệu nguồn.
Audit JSON giữ từng dòng README không rỗng/non-heading với section/quote/line và
`kind=descriptive` (2.119 descriptive units, không dùng số đó làm số luật normative).

Trong bảng: sales = N+Q/X; payment = checked per-order events/payable; purchase = chỉ
explicit headers (line totals/repeated headers chưa được gộp tùy ý); stock = exact snapshot
hoặc checked last balance; units = preconverted quantity/declared base unit, chưa chain
conversion. Nhóm domain còn gap không được ánh xạ sang công thức gần giống.

| Source README | SHA-256 | Lines audited | Declarations và mapping/gap |
|---|---|---|---|
| train/data/biz_101/README.md | `c71cf95a70eb10d08546bbf7755d55509d6410e009119579c4528720dadb125a` | 3–99 (91 units) | CSV order lines; preconverted quantity + per-product conversion; end-only coverage (sales policy). |
| train/data/biz_102/README.md | `6d857ce98b60e60a31147247c7339a5d9ac935987da3042faa14fb4022e04551` | 3–30 (19 units) | XLSX continuation/month sheets; product name join; conversion table. |
| train/data/biz_103/README.md | `2dcc4d7968fd2d7b74bf4ee2aae5f2bfca9dbdac69346148325e2ae8d82b3622` | 3–30 (19 units) | XLSX month sheets; line grain; conversion table. |
| train/data/biz_104/README.md | `70955b88b93c4d4d4b5de1806ed212a9599b9cf9fb959afdc3a042c301af3ab2` | 3–28 (18 units) | XLSX continuation; product name join; date format; units. |
| train/data/biz_105/README.md | `a73db6b16c8768c2fca49d10e6e8dcb6cc1054881c63c8afcbef487ae025a326` | 3–71 (53 units) | Monthly handwritten parts; linked negative return; snapshot; purchase line cost; price intervals. Purchase lines/price/large-unit gaps. |
| train/data/biz_106/README.md | `f9a2ed4a39f784cc16180f013a2f56ae44b7aab33e5fbaa4899af8c54bbb3515` | 3–23 (17 units) | POS separate headers/details parts; payment events/payable; selling-unit cost; snapshot; purchase headers; units. |
| train/data/biz_107/README.md | `8f1bb9e115bffdad77e112e42642ec86ab806af7f13bcfdb24dfcfa5e8c964df` | 3–97 (89 units) | No-order/no-customer ledger; identical records retained; preconverted quantities; order count missing identity. |
| train/data/biz_108/README.md | `47a853ffb79a0449190a5e331b209977a3a4b323943521c0d28037d01397e9c9` | 3–162 (142 units) | Coded POS; net merchandise vs payable; snapshot debt; no payment history declaration; purchase headers/details; units. |
| train/data/biz_109/README.md | `77fad305fc6460c804107b5d928570d1c3abf4d1cd3c0e5a27a3053fb5f3c0d3` | 3–17 (10 units) | POS lists share workbook, separate detail; payment/payable, line cost, snapshot, purchase headers, units. |
| train/data/biz_110/README.md | `85a5aecce97f14d3841cb259d375962131b0b04d1e6b009c343c39c3dcde265f` | 3–20 (12 units) | Header-only CSV; explicit merchandise vs payable; debt export-end snapshot; no detail/history inference. |
| train/data/biz_111/README.md | `f8915c1d420a59ecad992ce62a0afe4ba1d017baf9ccf336d554ea5e9ce101a8` | 3–59 (43 units) | Coded relational CSV; method/channel and customer group; payment events/payable; units. |
| train/data/biz_112/README.md | `358a56a6030c5ec3ae1507c1f771d66d61c08c3521fcbbe32df58f343d9d5878` | 3–46 (33 units) | Monthly line parts; name product join; catalog cost is not selling-line cost; units. |
| train/data/biz_113/README.md | `b4049fceb2bc428c311fceb768e68fd448739cb58136f682f591b6f7ca1e894c` | 3–34 (26 units) | No-order/no-customer construction ledger; preconverted quantity; conversion declarations. |
| train/data/biz_114/README.md | `f4b00c37c2c8e21cef94b2bbe6d2dc97752c6a8035b784cf9f5b507c7d01bc03` | 3–35 (27 units) | CSV construction order lines; preconverted quantity; units. |
| train/data/biz_115/README.md | `b30a5c7e5fa7c1d95cbd228a4ee95c9109c7a2e51f98b3011e37edd57edb9bb8` | 3–65 (50 units) | Monthly sales; name product join; delivery batches and credit terms. Delivery/age operators unavailable; no cash-history invention. |
| train/data/biz_116/README.md | `e0faa6edace43bad6e8db0a90dfe716eb09cdd38abb89ecdce3a926850cc3f1c` | 3–62 (47 units) | Sales/catalog/delivery/credit terms in sheets; delivery/age operators unavailable; units. |
| train/data/biz_117/README.md | `0e237a73fe29dcd5a8e4aab110e8d9b6abf2fd5be9555e1f25a2a242ee463e26` | 3–77 (57 units) | Monthly handwritten sales with linked negative returns; stock snapshot; delivery batches; credit terms; purchase lines; price intervals. Non-sales domain gaps. |
| train/data/biz_118/README.md | `823953d5c57f774e437c05b2a1f9115007c82fdf5de875dcef8f44e0532de371` | 3–64 (49 units) | Monthly sales/catalog; delivery and credit terms; preconverted quantity; units. Delivery/age gaps. |
| train/data/biz_119/README.md | `67ff934147166289e20602783fc5dfa2368fc4593f272d5154c20ae5a1af30f2` | 3–183 (156 units) | POS pharmaceutical flags/ingredients and unique lots, allocations/returns; snapshot; purchase headers; units. Drug metrics/lot value gaps. |
| train/data/biz_120/README.md | `4e6629c7ebc7470383ffeccdbffb5454ff5e14759cdb887c98d5e9fa64bd6cab` | 3–191 (164 units) | POS pharmaceutical flags/ingredients/lots; cost in catalog is not COGS; snapshot; purchase headers; units. |
| train/data/biz_121/README.md | `f1f925a1e0ae764824cab5aa55e1a4c91cafb65814af8e92ae7742974bf670d3` | 3–61 (40 units) | Sales + lookup workbook, name join, purchase base quantities and lot allocations without line number; mapping/key gaps retained. |
| train/data/biz_122/README.md | `2ea579b13f08f31fcf21f6dca27ca35c4671bab764f4e435c593b3375e2abe51` | 3–85 (60 units) | Monthly sales parts; multiple allocation files; purchase quantities/lots/drug catalog; unit table; mapping/key gaps. |
| train/data/biz_123/README.md | `75ca6deca11dcb39d204080a4b262cbdbfd0e229415dd1cccab9110160231240` | 3–24 (15 units) | SQLite coded POS: line net/preconverted quantities; payment events/payable; line cost; event stock; unique lots/allocations; purchase header; price intervals; drug flags/units. |
| train/data/biz_124/README.md | `a84ebff9b52edcc0c5e68793e8e15cdf8852d63d6b116617b082febc4846aa54` | 3–30 (24 units) | Mixed POS and handwritten sources; names vs codes; payment/payable + debt snapshot; line cost, expenses, stock snapshot, lots, purchase line totals, price/drug/unit declarations. Purchase-line/lot mapping gaps. |
| train/data/biz_125/README.md | `cf97c5b9193c16d138c1ed72b1c1933503e3a1c4fd5b289260fb721af0d729ad` | 3–31 (23 units) | CSV line grain; recorded service fee is revenue, not product; conversions. |
| train/data/biz_126/README.md | `f4891e2a70845c86afcdb27c8f115b6f66f69b1dbd193d965778959e91a1a441` | 3–29 (21 units) | No-order/no-customer ledger; service fee and preconverted quantity; conversions. |
| train/data/biz_127/README.md | `5870360d102d5a33b113a6a42432aad9ceac93d0c46834e380065bf4c091246b` | 3–65 (49 units) | Relational CSV; method/channel; payment/payable; service fees; units. |
| train/data/biz_128/README.md | `4a3bf5e4ad896fda5a8a3949c0ffb0cdc77c235579beb41dc4475a3372c97066` | 3–100 (77 units) | Monthly POS; debt/stock snapshots; purchase headers; order/table/guest grain differs; service fee. Table/guest operators unavailable. |
| train/data/biz_129/README.md | `7fcf56e75836f707a6041995158a4c309caa828f704cbc46c23685e873b7a348` | 3–65 (49 units) | Relational CSV; order method; payment/payable; service fee and units. |
| train/data/biz_130/README.md | `0793980b1f0ed2d6b7810ed340071d0b511b65c63610f23d7f8c3b1d0cb7cb42` | 3–50 (38 units) | Sales parts and catalog; preconverted quantities; catalog cost not line cost; service fee. |
| public_test/data/dev_001/README.md | `8e197003cef404f44a549d6840c4b136adaee1aaa0860156773ef6670df9c6db` | 3–108 (98 units) | Monthly sales lines and catalog; preconverted quantity and conversions; catalog cost not line cost. |
| public_test/data/dev_002/README.md | `f0521de9620274abc11ed648c317667bc806143cfe9c785ae36b5e74a1dd464e` | 3–31 (20 units) | Monthly files; name join; shared-cell topping grammar with included money and number of portions. Topping allocation/chain parsing gap, cannot silently rank dish+topping as one. |
| public_test/data/dev_003/README.md | `8d45483cea461c3dd2d95c284ce75dd140dee5d06c2712ed549a0a442c09dc13` | 3–24 (17 units) | Consolidated repeated invoice fields across year files; customer phone; payment/payable, cost, expense, snapshot, purchase lines and prices; cash-shift receipts/refunds/net not revenue. Cash/expense/line-import gaps. |
| public_test/data/dev_004/README.md | `bf4beeba546322af4f82f1be89de60ae5d0da540091f436eef51fdbad1301881` | 3–79 (59 units) | Handwritten monthly order lines; repeated total; returns/snapshot; delivery batches, terms, purchase line costs, price intervals; bundle conversion exception. Conversion/delivery/age gaps. |
| public_test/data/dev_005/README.md | `607867c478db2bbc3957051ea2d71f5b3f5ae6f790907a5f576ae15ed2cb519c` | 3–66 (45 units) | Monthly sales; drug/lot allocations; conversion ratios may name next lower unit. Chain conversion gap; numeric-only coefficient cannot be assumed base. |
| public_test/data/dev_006/README.md | `4fdcfdb174b0f28d0473e7ca870d8be34f8e754a99c7fc99ef441520c7729040` | 3–24 (15 units) | SQLite POS; same declared grains as pharmaceutical SQLite family; payment, line cost, event balances, unique lots, purchases, prices/drugs/units; no cross-split defaults. |
| public_test/data/dev_007/README.md | `82de5408cb80231a7f18b904251c306de12baa228b49ca782ad8791d79bdea79` | 3–82 (70 units) | Consolidated CSV invoice lines; repeated header paid/debt/net amounts; supplier header totals repeat across import lines. Snapshot debt not payment history; purchase collapse not yet implemented. |
| public_test/data/dev_008/README.md | `7294aef0ad498fa8b543b745bbbae05b70e981c4d6e3671936f779b93f5b8fe3` | 3–119 (105 units) | XLSX invoice parts; notes represent export-end debt; payable/method/header fields repeat; snapshot; repeated purchase headers. No debt history from notes; purchase collapse gap. |
| public_test/data/dev_009/README.md | `f0422ab8578dd0ad0fea1477de87214dbc69f39ed0e4868ecd0a8fae26cc31a5` | 3–20 (12 units) | Header-only CSV; explicit merchandise/payable; export-end debt snapshot; no detail/payment-history inference. |
| public_test/data/dev_010/README.md | `a42e1757f8e758ef53ed37c945e2e93f66df991989f6d6454c4f67823ce542fa` | 3–80 (64 units) | Coded mother/baby relational CSV; order method/payable/discount/shipping; payment events; group/unit catalog. No implicit membership/size facts. |
| public_test/data/dev_011/README.md | `fc4d0a959214863c4f72883dde2000add9fa58b332f899b8a80e7896b9a80a09` | 3–26 (18 units) | Phone handwritten invoice lines/STT; note returns; IMEI list; full-name/type abbreviations; repeated payable after discount/tradein. Domain/alias/IMEI mapping unavailable. |
| public_test/data/dev_012/README.md | `7bf1f0af647b509a88e1e9db9b5612a35cc40f8ce32a70ec74ac008049f524b5` | 3–23 (13 units) | One row/repair visit, multiple tasks and receipt total; paid vs warranty; mechanic aliases/commission. No amount per task; repair operators unavailable. |
| public_test/data/dev_013/README.md | `1a9c05fa623e79069b014240744c30bfb4c9210df44c155efb2b617049921c19` | 3–70 (52 units) | Two header rows; nonmerchandise voucher/points/ship lines; note statuses; name joins; source aliases, catalog size/category and conversion; membership dates/balances; promotion inclusive dates. Domain/role/loyalty mapping unavailable. |
| public_test/data/dev_014/README.md | `5953f87fc360fec7d5363e7b75cebb482ace9a2a7072754308ad89edebb4555b` | 3–19 (13 units) | Phone STT groups; IMEI/name/phone joins; tradein, separate debt/installment ledgers and warranty receipt events. Upfront vs payment schedule; warranty not sale. Domain operators unavailable. |

## Tests và giới hạn thực thi

`test_rulebook_authority.py` kiểm tra authority-only inventory, toàn quote, split/scope,
cosmetic row permutations/Markdown soft-wrap, negation/exceptions (kể cả cùng đoạn
với fragment đã nhận diện), missing source, parser-helper invalidation,
unknown-unit retention và label isolation. `test_business_metrics.py` kiểm tra numeric
state/payment dates ngoài sales span, quality/authority failures, status policy lãi gộp,
namespace/business/code/file/row permutations. S1/S2 boundary/Decimal/threshold/MTD/Tết/
weekday-denominator/list/table regressions ở các test suites trong legend.

Các chữ nghĩa đã hiểu nhưng operator chưa có gồm threshold tổng quát, customer loyalty/
qualified AOV/cancellations, product order popularity, staff/day/hour ranking, supplier
counts/weighted import price, large-unit/valuation/stockout/dead-stock/price effects,
net profit, expired-lot horizons/value, topping allocation và các ngành domain mới.
Nearest-lot chỉ ở export end và required allocations/dated returns; không tự suy FIFO.
COGS dùng selling-unit line cost; không thay bằng generic weighted cost/catalog/lot cost.

Không có owner decision mới để tự chốt công thức. Thiếu source/industry/encoding hoặc
mâu thuẫn cụ thể giữ unresolved/abstain và evidence; mở mapping/operator cần phiên riêng.
Standalone payment-only/stock-only packages chưa có general preparation entrypoint;
independent preflight áp dụng facts đã chuẩn bị hợp lệ trong pipeline hiện có.
Báo tests/gates và diff của phiên trong roadmap; không cập nhật native result report.

Rerun inventory (không đọc questions/answers, không tạo facts hoặc gọi model):

```bash
uv run --no-sync python -m scripts.audit_rulebook \
  --data-dirs dataset/train dataset/public_test --include-sources \
  --output .artifacts/rulebook-s1-s5/authority.json
```

Audit JSON có raw-source hashes khi dùng `--include-sources`, cho phép đối chiếu input
trước/sau quality gates; normalizer/parser fingerprints vẫn do code thật tính. Không
sửa tay cache/manifest. Registry/preflight/resolver software hashes vào semantic revision;
parser hash gồm `business_rules.py`; normalizer và mapping code vào prepared dependency.
