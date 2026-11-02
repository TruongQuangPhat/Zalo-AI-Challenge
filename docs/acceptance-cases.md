# Acceptance specifications — Phase 1

Fixture nguồn: [data_acceptance_cases.json](../tests/fixtures/data_acceptance_cases.json), version `data-acceptance-v1`.
33 case synthetic, không chép dữ liệu train/public. Mỗi case có input, expected/intermediate,
rule refs, capability, priority và implementation_status. Các mã C01–C30 giữ nghĩa ở Phụ lục F;
ba mã P1 bổ sung từ kiểm tra grain/summary/error/identity.

Tất cả fixture nghiệp vụ hiện **planned**: Phase 1 kiểm tra đặc tả đầy đủ/hợp lệ,
không chạy một engine giả rồi ghi metric passed. Các tests output/evaluator/runtime đã có
kiểm tra những hành vi liên quan C28–C30; việc đó không tự chuyển toàn bộ fixture thành passed.

| Case | Input / điều cần phân biệt | Expected chính / gate |
|---|---|---|
| C01 | Locale, hậu tố tiền và chuỗi ambiguous | 1.600; 4.320.000; 1.340.000; 10.000 VND; thiếu convention → abstain |
| C02 | Text/serial/thiếu năm/timestamp vs numeric ID | Date cùng nghĩa; giữ giờ; không đổi ID thành ngày |
| C03 | Header kế thừa ba dòng, đơn kế tiếp | Fill đúng group, không fill SL/amount; missing dependent value chặn tổng |
| C04 | Exact copies trong đơn, khác STT và khác đơn | Giữ 3 facts/2 đơn; lineage giữ bản sao; revenue 300 synthetic |
| C05 | Hai ledger records giống nhau, không order key | Giữ cả hai; không suy order_count bằng row count |
| C06 | Sale 100.000, partial return 20.000 | Cả đơn returned; revenue/count = 0, giữ return event |
| C07 | Voucher/points âm, ship dương | Không là returned; không trừ adjustments khỏi revenue hàng |
| C08 | Topping đã nằm trong tiền món, fee riêng | Bảo toàn tiền, không cộng topping lần hai; fee không xếp hạng sản phẩm |
| C09 | Ba detail, hai payments, một header | Payable 300.000, paid 150.000, balance 150.000; no fan-out |
| C10 | Snapshot tại mốc khác, không history | unanswerable nếu xác nhận nguồn thiếu; unsupported reader → abstain |
| C11 | Hai stock balances 10 rồi 7 | Lấy 7 theo thứ tự có căn cứ; ambiguity → abstain |
| C12 | Return event sau ngày hỏi | Loại revenue cả đơn; kho chưa nhận return tương lai |
| C13 | Hai thùng, 24 lon/thùng, cost/thùng | Base qty 48, COGS 360.000 |
| C14 | Catalog cost khác lot purchase cost | Inventory value 80.000, expiring lot cost 28.000 |
| C15 | Thu nợ đơn kỳ trước, COD và returned | Không cắt order về kỳ thu; loại COD khỏi nợ khách |
| C16 | Cancelled/returned/shipping/completed khác tập đếm | Cancel 20% trên 10; revenue orders 7 |
| C17 | Calendar rộng hơn available span, kỳ partial | Ngoài kỳ → unanswerable; partial giữ requested/effective windows |
| C18 | Complete empty filter vs amount parse error | 0 có căn cứ vs abstain, không xuất tổng thiếu |
| C19 | MTD 31/03, lịch âm theo calendar | Chặn 28/02; Tết gồm đủ hai đầu window |
| C20 | Bốn thứ Hai, chỉ ba ngày có tiền | Chia 4, average 100 VND synthetic |
| C21 | Ties và nhiều chỉ tiêu theo thứ tự hỏi | Tie theo mã; series không sort theo value |
| C22 | Group 20, toàn shop gồm walk-ins 100 | Share 20%, mẫu số không giữ filter nhóm |
| C23 | Một paid visit ba tasks và một warranty | Không nhân tiền/lượt theo tasks; denominator paid visits riêng |
| C24 | Sale/trade-in/down-payment/installments | Revenue một lần; payment không tạo revenue; due=today chưa overdue |
| C25 | Tên có số nhưng size null, member chưa mua | Không đoán size; vẫn đếm đăng ký đúng kỳ |
| C26 | Baseline missing vs baseline thực sự 0 | Coverage decision riêng; không chia 0 ra Infinity |
| C27 | Nhiều as-of và source/rule hash đổi | Prepared facts không cắt ngày; invalidation/key đủ scope |
| C28 | Table row permutation, ordered list, bool/number | Table có thể bằng; list khác thứ tự sai; bool ≠ number |
| C29 | 90 correct/10 wrong/2.900 abstain | PA 80/3.000; coverage 100/3.000; all-abstain accuracy null |
| C30 | Answers/QA/annotations ở cạnh input, unsupported op | Không mở metadata nhãn; abstain và log |
| P1-SUMMARY | Summary có tiền nằm giữa hai đơn | Revenue 150, count 2; summary có provenance, không forward-fill thành transaction |
| P1-ERROR | Error ở cột phụ vs amount | Giữ dòng có lỗi phụ, chặn tổng khi amount lỗi |
| P1-IDENTITY | Hai mã khách cùng tên | Hai customers; tên-only lookup ambiguous |

Trước khi bật S1: chạy C01–C06, C12, C16–C18, C30 và các case summary/error liên quan;
thêm C09 cho JOIN, C27 cho cache. C05 cần xác nhận fallback trên no-order shape.
Mọi case phải chạy qua code thật của capability, so cả intermediate khi có nguy cơ grain/unit.
Không dùng assertion chỉ kiểm tra chính expected value của fixture để nghiệm thu phép tính.

Phase 1 tests tập trung validator manifest/annotation, inventory, provenance và isolation:
chặn business/structure leakage, thay fingerprints, duplicate/missing IDs, metadata có answer,
review giả, source verification không bằng chứng, output trong dataset và source symlink.
Script `audit_dataset` kiểm tra fixture schema/case refs; không thực thi expected metrics.

Normalization S1 có behavioral tests riêng tại `tests/test_normalization.py` cho parse ngày/tiền,
header inheritance, exact dedup và lineage, summary/error, whole-order return với event tương lai,
coverage evidence, cache invalidation/round-trip và label isolation. Đây là bằng chứng của phần
preparation liên quan C01–C06/C12/C17–C18/C27/C30, chưa là toàn bộ acceptance metrics; chưa có
cancellation/temporal/stock executor hoặc câu qua LLM. Fixture `implementation_status=planned`
vẫn đúng cho nghiệm thu toàn capability; validator spec không được đổi thành chứng nhận engine.
