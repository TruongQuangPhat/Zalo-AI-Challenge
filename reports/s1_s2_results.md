# Báo cáo S1/S2 — đã có kết quả số S2 trên câu train thật

JSON riêng theo yêu cầu: [S1 results](s1_results.json) và [S2 results](s2_results.json).
Hai file là bản đối chiếu trực tiếp từ evidence mới nhất, gồm từng câu và actual của hai lượt.

## Kết quả mới nhất: native revenue growth

**S2 đã có kết quả số native:** cả hai lượt độc lập qua local LLM CUDA → QueryPlan → validation → preflight từng kỳ → read-only SQLite/Decimal → exact-rational percentage/verifier → typed output → evaluator đều đạt **8 correct / 0 wrong / 0 abstain**. Hai lượt predictions giống nhau. Không sửa draft, SQL hoặc output bằng tay.

Đây là 8 câu thật thuộc common-dev của hai protocol, không phải câu synthetic đổi wording từ train. Sáu giá trị gold không được xem/dùng cho implementation trước khi khóa selection/inference; hai giá trị đã hiển thị trong báo cáo trước. Các nhãn train có lịch sử EDA/streaming như protocol đã khai báo, nên **không gọi đây là blind holdout hoặc PA đại diện toàn dataset**. Gold cho lượt mới chỉ do evaluator host đọc sau khi cả hai inference containers/model workers đã thoát.

Bộ mới có **36 câu/case khác nhau mỗi lượt**: 13 S1 native regression + 8 S2 native growth + 15 synthetic. Hai repetitions không được cộng thành 72 câu độc lập. Runtime source hashes, image ID và toàn bộ per-question gold/actual nằm trong `native_growth_continuation` của [JSON evidence](s2_verification.json); các trường root cũ giữ đúng revision lịch sử.


| Nhóm | Mỗi lượt: correct / wrong / abstain | Đúng/N | Accuracy khi đã trả lời | Ý nghĩa |
| --- | --- | --- | --- | --- |
| S2 native growth (8 câu) | 8 / 0 / 0 | 100% (8/8) | 100% (8/8) | Đã có numeric S2 train-dev; 2 lượt giống nhau |
| 6 câu chưa dùng giá trị gold để triển khai | 6 / 0 / 0 | 100% (6/6) | 100% | Dev selection trước inspection; không phải blind holdout |
| 2 câu đã exposed trước đó | 2 / 0 / 0 | 100% (2/2) | 100% | Khai báo riêng, không giả là unseen |
| S1 regression (13 câu) | 12 / 0 / 1 | 92,31% (12/13) | 100% (12/12) | Câu filter sản phẩm vẫn abstain, 0 điểm |
| Tổng native (21 câu) | 20 / 0 / 1 | 95,24% (20/21) | 100% (20/20) | Chỉ subset đã khóa, không toàn dataset |
| Synthetic có gold xác định (11 case) | 9 / 0 / 2 | 81,82% (9/11) | 100% (9/9) | Giữ 2 unsupported abstentions trong mẫu số |
| Synthetic behavior (15 case) | 15/15 đạt hành vi; không gọi 15 correct | Không dùng làm QA accuracy | Không áp dụng | Gồm 4 diagnostic không có gold xác định |


343 tests, lint/format và lockfile đã đạt trên source mới. Không missing/malformed hoặc per-question runtime errors trong cả hai lượt. Model thực offload 29 layers trên GTX 1650. Đây là kết quả LLM CUDA; **chưa nghiệm thu model CPU cho growth, chưa hoàn thành toàn S2 mọi shape/operator**.

## Từng câu S2 native: câu hỏi → gold → actual của hai lượt

Tất cả thuộc `biz_104`, `as_of_date=2026-06-02`. Đáp án `percent` ghi số phần trăm, ví dụ `-31.57` nghĩa là −31,57%, không phải −0,3157. Mục tiêu runtime là trả đúng percent cho cả 8 câu này; không còn kỳ vọng abstain như checkpoint trước.


| ID / exposure | Câu hỏi nguyên văn | Gold | Actual lượt 0 | Actual lượt 1 | Verdict mỗi lượt |
| --- | --- | --- | --- | --- | --- |
| `q_0872d9f34f31`<br>Đã exposed | Cho mình hỏi so với tháng trước đó, doanh thu tháng 2/2026 tăng hay giảm mấy phần trăm? | `percent:-31.57` | `percent:-31.57` | `percent:-31.57` | correct / +1 |
| `q_35983db97b94`<br>Đã exposed | Doanh thu tháng 4 năm 2026 tăng trưởng bao nhiêu % so với tháng 4 năm 2025? | `percent:-5.83` | `percent:-5.83` | `percent:-5.83` | correct / +1 |
| `q_36d4c28e5079`<br>Không dùng giá trị gold để triển khai | so vs th truoc do, dthu thang 1/2026 tang hay giam may phan tram | `percent:0.14` | `percent:0.14` | `percent:0.14` | correct / +1 |
| `q_3b29ba188ca3`<br>Không dùng giá trị gold để triển khai | Tháng 3/2026 doanh thu thay đổi bao nhiêu % so với tháng liền trước? | `percent:42.46` | `percent:42.46` | `percent:42.46` | correct / +1 |
| `q_6b684641ac51`<br>Không dùng giá trị gold để triển khai | Tháng 10/2025 doanh thu thay đổi bao nhiêu % so với tháng liền trước? | `percent:1.17` | `percent:1.17` | `percent:1.17` | correct / +1 |
| `q_852a88cb4c90`<br>Không dùng giá trị gold để triển khai | So với tháng liền trước, doanh thu tháng 9 năm 2025 thay đổi bao nhiêu %? | `percent:-5.84` | `percent:-5.84` | `percent:-5.84` | correct / +1 |
| `q_908c0f903c5f`<br>Không dùng giá trị gold để triển khai | so vs th trước đó, doanh thu tháng 12/2025 tăng hay giảm mấy % | `percent:-0.35` | `percent:-0.35` | `percent:-0.35` | correct / +1 |
| `q_a383091ec4c7`<br>Không dùng giá trị gold để triển khai | so với th trước đó, doanh thu tháng 6/2025 tăng hay giảm mấy % | `percent:-4.62` | `percent:-4.62` | `percent:-4.62` | correct / +1 |


## Kỳ hỏi/kỳ so và các tổng tiền executor thực sự dùng

Công thức duy nhất đã bật: **(doanh thu kỳ hỏi − doanh thu kỳ so) / doanh thu kỳ so × 100**, làm tròn 2 chữ số ở output. Tổng tiền giữ Decimal chính xác; tỷ số dùng Fraction từ Decimal để không bị sai ở sát half-tie. Baseline phải dương; 0/âm hoặc undefined/unknown thì abstain, không gán 0 hoặc vô cực. Half-tie chưa có luật được xác nhận cũng abstain.

Đặc biệt “tháng 1/2026 so với tháng trước” là 01/2026 so 12/2025; không phải tháng trước của as-of 06/2026. Câu cùng tháng năm trước giữ nguyên 04/2026 và 04/2025. Cả requested và effective windows đều được kiểm tra độc lập trước khi tính.


| ID | Kỳ hỏi inclusive | Kỳ so inclusive | Revenue kỳ hỏi (VND) | Revenue kỳ so (VND) | Chênh lệch (VND) | Percent cuối |
| --- | --- | --- | --- | --- | --- | --- |
| `q_0872d9f34f31` | 2026-02-01…2026-02-28 | 2026-01-01…2026-01-31 | 135511000 | 198041000 | -62530000 | -31.57 |
| `q_35983db97b94` | 2026-04-01…2026-04-30 | 2025-04-01…2025-04-30 | 185781000 | 197293000 | -11512000 | -5.83 |
| `q_36d4c28e5079` | 2026-01-01…2026-01-31 | 2025-12-01…2025-12-31 | 198041000 | 197772000 | 269000 | 0.14 |
| `q_3b29ba188ca3` | 2026-03-01…2026-03-31 | 2026-02-01…2026-02-28 | 193051500 | 135511000 | 57540500 | 42.46 |
| `q_6b684641ac51` | 2025-10-01…2025-10-31 | 2025-09-01…2025-09-30 | 195454000 | 193201000 | 2253000 | 1.17 |
| `q_852a88cb4c90` | 2025-09-01…2025-09-30 | 2025-08-01…2025-08-31 | 193201000 | 205193000 | -11992000 | -5.84 |
| `q_908c0f903c5f` | 2025-12-01…2025-12-31 | 2025-11-01…2025-11-30 | 197772000 | 198458500 | -686500 | -0.35 |
| `q_a383091ec4c7` | 2025-06-01…2025-06-30 | 2025-05-01…2025-05-31 | 194246000 | 203645500 | -9399500 | -4.62 |


Hai tổng VND, hai kỳ và percent thực tế của cả 8 câu khớp reference độc lập đọc trực tiếp worksheet XML bằng stdlib ZipFile/XML, không dùng reader/normalizer/SQL của ứng dụng. Reference này và fixed-plan wiring 8/8 được kiểm tra trước khi gọi LLM/đọc thêm gold; **chỉ hai lượt model sinh plan thực tế mới được tính là LLM acceptance**.

## Preparation và ranh giới được mở

- Một sheet XLSX order-line được README xác nhận mỗi dòng một sản phẩm, header đơn lặp đủ; mappings gắn với đúng filename/section README. Bảng quy đổi đơn vị được nhận diện riêng và không JOIN vào revenue/count. Multi-sheet hoặc nguồn khác chưa phân loại sẽ abstain.
- Source đã đọc/parse đầy đủ: **39.006 orders, 62.451 order lines, 443 summary rows, 0 issues**. Coverage mọi đơn là **01/04/2025–02/06/2026** (`observed_order_dates`), kể cả huỷ/trả/active; README end được đối chiếu. Chính sách này không phát hiện được bản ghi thiếu bên trong export.
- Topping có dòng sản phẩm, số lượng/thành tiền riêng theo README được giữ là sale item của shape này. Không mở parsing embedded topping, voucher, phí hoặc ranking/quantity. Các dòng tổng chỉ chứng minh định dạng dấu phân cách ở cột tiền, không được cộng thành giao dịch.
- Alias `Đã hoàn hàng` được phân loại returned; `Đang giao dịch` là trạng thái active không huỷ/trả cho chính sách revenue/count. Raw status được giữ trong source values/lineage; alias này không chứng nhận một lần giao hàng vật lý cho metric shipment chưa bật.
- Growth chỉ mở revenue toàn business, cùng unit/scope giữa hai kỳ. Difference revenue/count và S1 được giữ regression. Không mở average/share/ranking/entity/JOIN/cost hoặc Excel tổng quát.

## S1 regression và toàn bộ 15 synthetic trên source mới

Hai lượt mới đều có S1 **12 correct / 0 wrong / 1 abstain**. Đã đối chiếu đủ 13 actual outputs của cả hai lượt mới với từng dòng bảng S1 trong phụ lục: giá trị/type giống nhau. Câu Coca Cola vẫn có gold 2726000 VND nhưng actual abstain vì product filter chưa bật, nhận 0 điểm; không phải correct hoặc thiếu dữ liệu.

Bảng sau giữ đủ 15 synthetic behavior cases. Có 11 case với gold xác định để chạy evaluator (9 correct, 2 abstain); 4 case unknown/undefined/ambiguous không được tạo placeholder gold. Cả 15/15 behavior checks đều đạt ở cả hai lượt. Các case không có gold vẫn được liệt kê đầy đủ; không trộn behavior pass vào correct/PA.


| ID / as-of | Câu hỏi nguyên văn | Gold có căn cứ | Runtime mong đợi | Actual 0 | Actual 1 |
| --- | --- | --- | --- | --- | --- |
| `synthetic_money`<br>2026-01-03 | Doanh thu hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:1` | `number:1` | `number:1` | `number:1` |
| `synthetic_reverse`<br>2026-01-03 | Doanh thu hôm qua so với hôm nay chênh lệch bao nhiêu? | `number:-1` | `number:-1` | `number:-1` | `number:-1` |
| `synthetic_count`<br>2026-01-03 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:1` | `number:1` | `number:1` | `number:1` |
| `synthetic_mtd`<br>2026-01-03 | Doanh thu tháng này so với tháng trước chênh lệch bao nhiêu? | `number:0` | `number:0` | `number:0` | `number:0` |
| `synthetic_zero`<br>2026-01-05 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:0` | `number:0` | `number:0` | `number:0` |
| `synthetic_negative_count`<br>2026-01-04 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:-2` | `number:-2` | `number:-2` | `number:-2` |
| `synthetic_outside`<br>2025-12-01 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `unanswerable` | `unanswerable` | `unanswerable` | `unanswerable` |
| `synthetic_unknown`<br>2026-01-03 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | Chưa có gold xác định; diagnostic | `abstain` | `abstain` | `abstain` |
| `synthetic_growth`<br>2026-01-03 | Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm? | `percent:1.26` | `percent:1.26` | `percent:1.26` | `percent:1.26` |
| `synthetic_filter`<br>2026-01-03 | Doanh thu riêng sản phẩm A hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:50` | `abstain` | `abstain` | `abstain` |
| `synthetic_absolute`<br>2026-01-03 | Doanh thu hôm nay so với hôm qua chênh lệch tuyệt đối bao nhiêu? | `number:1` | `abstain` | `abstain` | `abstain` |
| `synthetic_share`<br>2026-01-03 | Tỉ trọng doanh thu hôm nay bao nhiêu? | Chưa có gold xác định; diagnostic | `abstain` | `abstain` | `abstain` |
| `synthetic_growth_zero`<br>2026-01-05 | Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm? | Chưa có gold xác định; diagnostic | `abstain` | `abstain` | `abstain` |
| `synthetic_growth_unknown`<br>2026-01-03 | Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm? | Chưa có gold xác định; diagnostic | `abstain` | `abstain` | `abstain` |
| `synthetic_growth_outside`<br>2025-12-01 | Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm? | `unanswerable` | `unanswerable` | `unanswerable` | `unanswerable` |


`synthetic_growth` giờ trả đúng `percent:1.26` thay vì abstain tại checkpoint trước. `synthetic_growth_zero` abstain vì baseline 0; `synthetic_growth_unknown` vì coverage unknown; `synthetic_growth_outside` trả unanswerable vì baseline window nằm ngoài nguồn. Filter sản phẩm/absolute difference/share chưa bật vẫn không bị sửa nghĩa thành tổng hoặc growth mặc định.

## Verification, isolation và giới hạn


| Mục | Kết quả |
| --- | --- |
| Image source mới | `sha256:ba57590dbc0d3116cea82983588115d32ab9c0453e1bba26bcddfdccc1ce3d24` (`datamate-s2-growth-cuda:local`) |
| Plan source SHA-256 | `1290b6ce30b9d4c4f82b4e30540241bda1db3bb990f063713bb405e97210a87d` |
| Model | Qwen2.5-1.5B-Instruct GGUF Q4_K_M, cùng model SHA-256 đã khóa; llama-cpp-python 0.3.16, 29 layers offloaded |
| Workers/budgets | Hai fresh workers; reuse mỗi run; seed 0, temperature 0, context 4096, batch 128, threads 4, question budget 120 s |
| Cache | Lượt 0 cold catalog/facts; lượt 1 cache hit cả catalog/facts; predictions 36/36 giống nhau |
| Wall time quan sát | 396.23342800200044 s và 305.4017185849989 s; gồm startup/preparation/generation, không phải SLA |
| Isolation | Network none; root/input read-only; chỉ mount raw inputs và thư mục output/cache; labels/EDA/reports không có trong inference |
| Gold/evaluator | Gold chỉ evaluator host đọc sau cả hai workers/containers thoát; 8 gold gốc được đối chiếu riêng, không đưa vào planner |
| Hashes | Source/image/model khớp; original/staged inputs bất biến; source không đổi giữa hai lượt |
| Artifacts | Runs/facts/gold/staging tạm tự dọn; giữ image + manifest nhỏ + report; không commit/push |


**Đã đạt yêu cầu có kết quả S2 native trong lát cắt này.** Chưa có PA/accuracy đại diện toàn dataset, blind holdout, toàn S2/Phase 5–7, model CPU growth hoặc Blackwell/BTC certification. Hai repetitions chứng minh tính lặp lại của subset đã khóa, không biến 8 câu thành 16 mẫu độc lập. Tải host/cache không được kiểm soát; không suy throughput/SLA từ các timings này. Parser/guards vẫn hữu hạn.

Phần phụ lục dưới giữ nguyên các bảng câu hỏi/gold/actual ở checkpoint **trước khi bật growth/XLSX** để truy vết vì sao lúc đó S2 native là 0 correct/2 abstain. Các kết luận “chưa có numeric S2 train” trong phần lịch sử này đã được thay bằng kết quả 8/8 ở phần mới nhất phía trên; không dùng source hashes cũ chứng nhận source mới.

---

## Phụ lục — checkpoint trước khi mở native growth (lịch sử)


Bản đối chiếu sau inference ngày 03/11/2026. Báo cáo này bao phủ **đủ 27 câu/case khác nhau của phiên S2**: 13 câu S1 train-dev, 2 câu S2 train-dev và 12 case synthetic S2. Đây không phải đánh giá toàn bộ train hoặc toàn bộ S1/S2. Các lượt lặp lịch sử không được cộng thành câu hỏi mới.

Bản trước có hashes, traces và tổng điểm nhưng thiếu bảng gold–thực tế theo từng câu để đọc thuận tiện. Bản này bổ sung giá trị gold từ **15 câu dev đã khóa**, đối chiếu bằng evaluator hiện có; không chạy lại model, không sửa source runtime, prompt, config, selection hoặc predictions. Gold chỉ dùng trong bước đánh giá/lập báo cáo sau inference.

### 1. Đọc kết quả chính xác như thế nào?

- **Đáp án chuẩn/gold:** mục tiêu nghiệp vụ của câu hỏi. Câu train lấy từ `dataset/train/answers.jsonl`; không suy gold từ prediction.
- **Runtime mong đợi:** hành vi của capability đã bật. Một câu có gold là số vẫn có thể được thiết kế `abstain` vì filter/operator chưa hỗ trợ.
- **Correct:** đáp án thực tế khớp gold và format, được **+1**. **Wrong:** **−1**. **Abstain:** **0**, kể cả khi đó là fallback đúng dự kiến.
- **Tỷ lệ đúng toàn subset = C/N**. **Độ chính xác khi đã trả lời = C/(C+W)**; không xác định nếu không trả lời câu nào.
- **Tỷ lệ trả lời = (C+W)/N**. **PA chuẩn hóa nội bộ = (C−W)/N**. Các bảng này có W=0 nên PA tình cờ bằng C/N; khi có câu sai, hai đại lượng sẽ khác nhau.
- **Đạt hành vi trong phạm vi** khác **correct so với gold**. `13/13` hoặc `12/12` behavior checks không được diễn giải thành PA 100%.

Ví dụ S1 `12 correct / 0 wrong / 1 abstain`: đúng **12/13 = 92,31% toàn subset**, trả lời **12/13 = 92,31%**, và đúng **12/12 = 100% trong phần đã trả lời**. Một câu còn lại có gold thật nhưng engine chưa trả lời; không được tính là correct.

### 2. Tổng hợp kết quả


| Nhóm | N | Correct | Wrong | Abstain | Đúng/N | Trả lời/N | Đúng/đã trả lời | Điểm / tối đa |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 train-dev, source cuối CUDA | 13 | 12 | 0 | 1 | 92,31% | 92,31% | 100,00% | 12/13 |
| S2 train-dev, 2 câu growth chưa bật | 2 | 0 | 0 | 2 | 0,00% | 0,00% | — | 0/2 |
| Toàn 15 câu train-dev đã khóa | 15 | 12 | 0 | 3 | 80,00% | 80,00% | 100,00% | 12/15 |
| S2 synthetic CUDA, 12 case kỹ thuật (*) | 12 | 7 | 0 | 5 | 58,33% | 58,33% | 100,00% | 7/12 |
| S2 synthetic CUDA, 10 case có gold xác định (*) | 10 | 7 | 0 | 3 | 70,00% | 70,00% | 100,00% | 7/10 |
| S2 synthetic CPU, source cuối | 12 | 0 | 0 | 12 | 0,00% | 0,00% | — | 0/12 |


Tất cả các nhóm trên không có missing/malformed. `—` là không có mẫu số để tính accuracy, không phải 100% hay 0% accuracy khi đã trả lời.

(*) Synthetic là fixture tự dựng, không phải câu/gold BTC. Trong 12 case có **2 case không xác lập được gold nghiệp vụ duy nhất** (`synthetic_unknown`, `synthetic_share`); nhãn `unanswerable` cũ chỉ là placeholder diagnostic để chạy evaluator. Vì vậy **7/12 = 58,33% là kết quả chấm engineering được giữ để tái lập, không phải ước lượng accuracy S2 trên dữ liệu thật**. Dòng 10 case chỉ đối chiếu những fixture có gold xác định; vẫn giữ cả 3 câu có đáp án nhưng operator/filter chưa bật và thực tế abstain. Hai case còn lại vẫn có mặt trong bảng 12 case và kiểm tra behavior, không bị giấu khỏi báo cáo.

**Kết quả có như mong đợi không?** Có trong capability đã khóa trên CUDA: S1 12 câu supported trả đúng số, 1 câu filter abstain; S2 synthetic 6 number + 1 unanswerable đúng và 5 abstain đúng hành vi; 2 native S2 phần trăm abstain. Nhưng mục tiêu trả lời đầy đủ chưa đạt: S1 còn 1/13 câu chưa trả lời, native S2 0/2 câu được trả lời. CPU synthetic không đạt numeric wiring do timeout.

Không gộp synthetic với train thành một tỷ lệ accuracy chung. Tỷ lệ 80% của 15 câu dev cũng không đại diện toàn dataset hoặc toàn S2. Chưa có bằng chứng numeric S2 train trên slice này.

### 3. S1 — đầy đủ 13 câu train-dev, source cuối CUDA

Gold có `type=number`; tiền là VND, count là số đơn. Câu hỏi giữ nguyên văn, kể cả viết tắt/không dấu. Ngày tham chiếu là `as_of_date`, không phải ngày máy. `number = gold` nghĩa là vừa đúng type vừa đúng giá trị ở cột gold.


| ID / business / as-of | Câu hỏi nguyên văn | Gold gốc | Runtime mong đợi | Thực tế CUDA | Verdict / điểm |
| --- | --- | --- | --- | --- | --- |
| `q_0cb7c9b93048`<br>biz_101<br>2025-09-24 | Cho tôi biết số đơn hôm qua. | `number:37` | `number` = gold | `number:37` | correct / 1 |
| `q_1af02cd5fe06`<br>biz_101<br>2026-04-14 | Cho tôi biết số đơn hqua | `number:44` | `number` = gold | `number:44` | correct / 1 |
| `q_b90743fdcce6`<br>biz_101<br>2026-07-06 | số bill hôm qua là bao nhiêu | `number:35` | `number` = gold | `number:35` | correct / 1 |
| `q_2a3019504577`<br>biz_101<br>2026-01-12 | doanh thu hqua cua cua hang la bao nhiu | `number:3530400` | `number` = gold | `number:3530400` | correct / 1 |
| `q_ed4fe0895229`<br>biz_101<br>2026-08-24 | Cho tôi biết doanh thu tháng 1/2026. | `number:245184900` | `number` = gold | `number:245184900` | correct / 1 |
| `q_c893acce3ef3`<br>biz_101<br>2026-08-24 | Xem giúp tổng doanh thu tháng 7 năm 2026 là bao nhiêu? | `number:236132200` | `number` = gold | `number:236132200` | correct / 1 |
| `q_05f70ec18809`<br>biz_110<br>2026-07-03 | cho hỏi số đơn quý 2/2026 là nhiêu | `number:5772` | `number` = gold | `number:5772` | correct / 1 |
| `q_09f475fddfec`<br>biz_110<br>2025-06-25 | xem giúp số đơn hôm nay là bao nhiêu | `number:74` | `number` = gold | `number:74` | correct / 1 |
| `q_3790a635bd18`<br>biz_110<br>2026-02-10 | Cho tôi biết số đơn hqua. | `number:63` | `number` = gold | `number:63` | correct / 1 |
| `q_5d6431886ab9`<br>biz_110<br>2026-06-11 | hnay bán được mấy đơn hàng? | `number:89` | `number` = gold | `number:89` | correct / 1 |
| `q_99a0f6d709bd`<br>biz_110<br>2026-02-21 | Tính riêng ngày hôm nay, cửa hàng bán được bao nhiêu đơn hàng? | `number:31` | `number` = gold | `number:31` | correct / 1 |
| `q_c86e099eb394`<br>biz_110<br>2025-05-14 | Tính riêng ngày hôm nay, cửa hàng bán được bao nhiêu đơn hàng? | `number:79` | `number` = gold | `number:79` | correct / 1 |
| `q_62a9a454f35b`<br>biz_101<br>2026-08-24 | Doanh thu của Coca Cola 330ml trong tháng 5 năm 2026 là bao nhiêu? | `number:2726000` | `abstain` (filter sản phẩm chưa bật) | `abstain` | abstain / 0 |


Câu `q_62a9a454f35b` có gold **2726000 VND**, nên dữ liệu có đáp án cho câu này. `abstain` xuất phát từ **product/entity filter chưa được hỗ trợ**, lỗi guard lưu là `question_owner_filter_unsupported`; không được diễn giải thành thiếu dữ liệu hoặc gold `unanswerable`. Fallback đúng thiết kế nhưng mất cơ hội +1 điểm. Vì vậy **92,31%** là tỷ lệ đúng toàn bộ 13 câu, còn **100%** chỉ áp dụng 12 câu đã trả lời.

Các kỳ requested/effective của 12 câu trả số và lý do guard của câu abstain được giữ theo ID trong [JSON đối chiếu](s2_verification.json), phần `question_review.native_cases` và observations của backend CUDA. Revenue/count dùng coverage export/README theo chính sách hiện tại; không khôi phục blocker unknown của report lịch sử.

### 4. S2 trên train-dev — đầy đủ 2 câu ngoài capability hiện bật

Đây là câu thật có gold phần trăm từ train; không sửa wording hoặc tự tạo gold thay cho câu train. Cả hai thuộc dev của hai protocol. Growth chưa bật; source Excel tương ứng cũng chưa được mở numeric normalization cho slice này.


| ID / business / as-of | Câu hỏi nguyên văn | Gold gốc | Runtime mong đợi | Thực tế CUDA | Verdict / điểm |
| --- | --- | --- | --- | --- | --- |
| `q_0872d9f34f31`<br>biz_104<br>2026-06-02 | Cho mình hỏi so với tháng trước đó, doanh thu tháng 2/2026 tăng hay giảm mấy phần trăm? | `percent:-31.57` | `abstain`: growth/source ngoài phạm vi | `abstain` | abstain / 0 |
| `q_35983db97b94`<br>biz_104<br>2026-06-02 | Doanh thu tháng 4 năm 2026 tăng trưởng bao nhiêu % so với tháng 4 năm 2025? | `percent:-5.83` | `abstain`: growth/source ngoài phạm vi | `abstain` | abstain / 0 |


Gold `percent:-31.57` nghĩa là −31,57%, không phải −0,3157; tương tự `percent:-5.83` là −5,83%. Hai actual là `abstain`, không phải các giá trị này. Evaluator: **0 correct / 0 wrong / 2 abstain**, tỷ lệ đúng toàn subset và coverage đều **0%**; accuracy khi đã trả lời **không xác định**. Behavior gate có thể đạt 2/2 nhưng không có câu nào được cộng điểm.

Hai shape CSV đang bật (`biz_101`, `biz_110`) không có câu comparison trong common-dev đã rà. Vì vậy chưa có numeric S2 native acceptance; không lấy synthetic difference thay bằng chứng train hoặc bật thêm shape/operator chỉ để có số đẹp.

### 5. S2 synthetic — đầy đủ 12 case, source cuối CUDA

Gold có căn cứ được tính độc lập từ fixture trong JSON; không phải số do LLM tự tính. Dữ liệu đầy đủ được fixture khai báo từ **01/12/2025 đến 05/01/2026**. Bảng sales có 8 orders/8 lines, gồm cả huỷ/trả; hai status này không được cộng vào revenue/count. `synthetic_empty` có cùng schema nhưng không có rows hoặc authority completeness.

#### 5.1. Câu hỏi, gold, kỳ vọng và kết quả


| ID / business / as-of | Câu hỏi nguyên văn | Gold xác định từ fixture | Runtime mong đợi | Thực tế CUDA | Đối chiếu |
| --- | --- | --- | --- | --- | --- |
| `synthetic_money`<br>synthetic_sales<br>2026-01-03 | Doanh thu hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:1` | `number:1` | `number:1` | correct / +1; behavior đạt |
| `synthetic_reverse`<br>synthetic_sales<br>2026-01-03 | Doanh thu hôm qua so với hôm nay chênh lệch bao nhiêu? | `number:-1` | `number:-1` | `number:-1` | correct / +1; behavior đạt |
| `synthetic_count`<br>synthetic_sales<br>2026-01-03 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:1` | `number:1` | `number:1` | correct / +1; behavior đạt |
| `synthetic_mtd`<br>synthetic_sales<br>2026-01-03 | Doanh thu tháng này so với tháng trước chênh lệch bao nhiêu? | `number:0` | `number:0` | `number:0` | correct / +1; behavior đạt |
| `synthetic_zero`<br>synthetic_sales<br>2026-01-05 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:0` | `number:0` | `number:0` | correct / +1; behavior đạt |
| `synthetic_negative_count`<br>synthetic_sales<br>2026-01-04 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:-2` | `number:-2` | `number:-2` | correct / +1; behavior đạt |
| `synthetic_outside`<br>synthetic_sales<br>2025-12-01 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | `unanswerable` | `unanswerable` | `unanswerable` | correct / +1; behavior đạt |
| `synthetic_unknown`<br>synthetic_empty<br>2026-01-03 | Số đơn hôm nay so với hôm qua chênh lệch bao nhiêu? | Chưa xác lập gold nghiệp vụ | `abstain` | `abstain` | abstain / 0; behavior đạt |
| `synthetic_growth`<br>synthetic_sales<br>2026-01-03 | Doanh thu hôm nay so với hôm qua tăng bao nhiêu phần trăm? | `percent:1.26` | `abstain` | `abstain` | abstain / 0; behavior đạt |
| `synthetic_filter`<br>synthetic_sales<br>2026-01-03 | Doanh thu riêng sản phẩm A hôm nay so với hôm qua chênh lệch bao nhiêu? | `number:50` | `abstain` | `abstain` | abstain / 0; behavior đạt |
| `synthetic_absolute`<br>synthetic_sales<br>2026-01-03 | Doanh thu hôm nay so với hôm qua chênh lệch tuyệt đối bao nhiêu? | `number:1` | `abstain` | `abstain` | abstain / 0; behavior đạt |
| `synthetic_share`<br>synthetic_sales<br>2026-01-03 | Tỉ trọng doanh thu hôm nay bao nhiêu? | Chưa xác lập gold nghiệp vụ | `abstain` | `abstain` | abstain / 0; behavior đạt |


#### 5.2. Phép tính/lý do cho từng case


| Case | Căn cứ kỳ vọng |
| --- | --- |
| `synthetic_money` | 100.50 − 99.25 = 1.25 → 1 VND; không làm tròn từng operand. |
| `synthetic_reverse` | 99.25 − 100.50 = −1.25 → −1 VND; đảo kỳ phải đổi dấu. |
| `synthetic_count` | 2 − 1 = 1 đơn; không dùng số dòng CSV làm số đơn. |
| `synthetic_mtd` | 01–03/01: 199.75; 01–03/12: 199.50; chênh .25 → 0 VND. Dòng 20/12 trị giá 777 không thuộc cùng kỳ. |
| `synthetic_zero` | 04/01 và 05/01 đều nằm trong coverage đầy đủ, không có đơn: 0 − 0 = 0. |
| `synthetic_negative_count` | 04/01 có 0 đơn, 03/01 có 2 đơn: 0 − 2 = −2. |
| `synthetic_outside` | Kỳ so 30/11/2025 nằm ngoài complete span bắt đầu 01/12/2025; không được thay kỳ thiếu bằng 0. |
| `synthetic_unknown` | Export rỗng, không authority completeness: unknown → abstain. Không có gold nghiệp vụ duy nhất được chứng minh; unanswerable trong lần chấm cũ chỉ là nhãn diagnostic. |
| `synthetic_growth` | Gold suy từ fixture: (100.50 − 99.25)/99.25 × 100 → 1.26%. Engine chưa mở growth: mong đợi abstain, nhận 0 điểm. |
| `synthetic_filter` | A: hôm nay 50.25, hôm qua 0; chênh → 50 VND. Engine chưa mở product filter: mong đợi abstain, nhận 0 điểm. |
| `synthetic_absolute` | \|100.50 − 99.25\| = 1.25 → 1 VND. Engine chỉ mở signed difference: mong đợi abstain, nhận 0 điểm. |
| `synthetic_share` | Không nêu mẫu số/phạm vi tỉ trọng: không xác lập gold nghiệp vụ duy nhất. Mong đợi abstain; nhãn unanswerable cũ chỉ phục vụ diagnostic. |


Sáu câu number supported đúng **6/6**; một câu outside trả đúng `unanswerable` **1/1**. Năm abstain gồm unknown coverage, growth, filter sản phẩm, absolute difference và share thiếu mẫu số. Ba ca growth/filter/absolute có gold xác định nhưng chưa trả lời được; hai ca unknown/share không có gold nghiệp vụ duy nhất được chứng minh. Không gọi 5 abstain là 5 correct.

MTD là phép so sánh **01–03/01/2026 với 01–03/12/2025**. Tổng chưa làm tròn lần lượt 199.75 và 199.50; kết quả .25 chỉ làm tròn ở cuối thành 0. Cả tháng 12 sẽ lấy nhầm dòng 20/12 trị giá 777. Report giữ trace generated draft → requested/effective windows → exact operand values → output cho MTD và money.

#### 5.3. CPU — kết quả thực tế đầy đủ, cùng 12 câu/fixture ở trên

Câu hỏi và gold được tra đúng ID ở bảng 5.1. Cột bên dưới là output CPU thực tế, không phải output CUDA gán cho CPU.


| ID | Runtime mong đợi | Thực tế CPU | Lý do | Evaluator / điểm |
| --- | --- | --- | --- | --- |
| `synthetic_money` | `number:1` | `abstain` | timeout 120 s; worker stopped | abstain / 0 |
| `synthetic_reverse` | `number:-1` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_count` | `number:1` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_mtd` | `number:0` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_zero` | `number:0` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_negative_count` | `number:-2` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_outside` | `unanswerable` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_unknown` | `abstain` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_growth` | `abstain` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_filter` | `abstain` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_absolute` | `abstain` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |
| `synthetic_share` | `abstain` | `abstain` | worker unavailable sau timeout; không có generation | abstain / 0 |


CPU source cuối: **0 correct / 0 wrong / 12 abstain**, đúng/N = **0%**, coverage **0%**, accuracy khi đã trả lời **không xác định**. Câu đầu timeout, worker bị dừng; 11 câu sau không được model xử lý thành công. Output abstain ở 5 case guard tuy trùng kỳ vọng nhưng **không chứng minh guard được thực thi**. Behavior của CPU chưa được nghiệm thu. Revision trước cũng có một attempt timeout tương tự, giữ riêng trong JSON; không gộp thành 24 câu độc lập và không tăng budget để đổi kết quả.

### 6. S1 lịch sử, revision và tính tái lập

Đã đối chiếu predictions của 6 lượt S1 lịch sử dưới đây với 13 đáp án thực tế ở bảng 3: **các giá trị/type đều giống nhau**, gồm cùng một abstain sản phẩm. Bảng 3 vì vậy cũng cho biết gold/actual từng câu của các lượt này; không cần sao chép bảng 13 câu sáu lần. Mỗi lượt vẫn chỉ có cùng 13 câu, không phải tổng 78 câu độc lập.


| Bằng chứng | Backend / lượt | Correct / wrong / abstain | Câu đúng/N | Actual so với bảng 3 | Phạm vi revision |
| --- | --- | --- | --- | --- | --- |
| planner_verification.json | host CPU / 0 | 12/0/1 | 92,31% | Giống 13/13 outputs | Source hashes của report gốc; lịch sử S1 |
| planner_verification.json | host CPU / 1 | 12/0/1 | 92,31% | Giống 13/13 outputs | Source hashes của report gốc; lịch sử S1 |
| docker_llm_verification.json | Docker CPU / 0 | 12/0/1 | 92,31% | Giống 13/13 outputs | Source hashes của report gốc; lịch sử S1 |
| docker_llm_verification.json | Docker CPU / 1 | 12/0/1 | 92,31% | Giống 13/13 outputs | Source hashes của report gốc; lịch sử S1 |
| docker_llm_verification.json | Docker GPU / 0 | 12/0/1 | 92,31% | Giống 13/13 outputs | Source hashes của report gốc; lịch sử S1 |
| docker_llm_verification.json | Docker GPU / 1 | 12/0/1 | 92,31% | Giống 13/13 outputs | Source hashes của report gốc; lịch sử S1 |


Các probe synthetic S1 cũ, M1 inventory snapshot và lịch sử unknown-coverage vẫn ở [planner report](planner_verification.json); không được nhập vào điểm của 27 case hiện tại. Báo cáo này không tuyên bố đã tái dựng đầy đủ nguyên văn mọi probe lịch sử đã dọn artifact. [Docker S1 report](docker_llm_verification.json) giữ packaging/CPU/GPU của S1 cũ, không tự chứng nhận S2 source cuối.

Revision S2 trung gian có CUDA MTD abstain vì model chọn `previous_month`; validator đã từ chối trước coverage. Sau khi grammar sinh giới hạn đúng hai literal periods, source cuối đã qua MTD. Diagnostic này và CPU S1 ở revision trung gian được giữ tại `prior_revision_summary`; không thay kết quả source cuối bằng kết quả lịch sử.

### 7. Bằng chứng vận hành và điều chưa được chứng nhận


| Mục | Giá trị / phạm vi |
| --- | --- |
| Git | feature/phase5-s2; base 698f9dc; working tree chưa commit |
| Hash plan source cuối | `84e91e1f8bb779bdbafe76ed8da61fda9655caf64854a3e80ef266138d4c5cd4` |
| Model | Qwen2.5-1.5B-Instruct GGUF Q4_K_M; SHA-256 `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` |
| CUDA image source cuối | `sha256:039af5ee0f71f27315a12f79eb626b550df1514cfcc147280f2c78ca873a807c` |
| CPU image source cuối | `sha256:f2f72f35c1423c4a5460327010cb9f1f6148d471d7fa6bec28e459471b24faa9` |
| Model runtime | llama-cpp-python 0.3.16; ChatML; seed 0; temperature 0; context 4096; batch 128; threads 4 |
| Budgets | S1 output ≤256 tokens; S2 ≤384; tối đa 2 attempts, budget câu 120 s; không semantic repair |
| Thiết bị | GTX 1650 4 GiB; offload thực 29 layers; CPU i5-10300H; WSL RAM khoảng 3,8 GiB |
| Quality code | 318 tests, lint/format và lockfile đã đạt ở lượt implementation; không phải 318 câu train được chấm |
| Lượt source cuối | CUDA: 1 synthetic run + 1 dev run; CPU: 1 synthetic attempt; không có final-source CPU S1 native run |
| Input/isolation | Source/model hashes khớp; raw/staged inputs bất biến; network none; root/input read-only; labels/EDA/reports không mount vào inference |
| Đánh giá | Evaluator host chỉ đọc nhãn sau inference; lần bổ sung này chỉ re-score saved predictions và lập báo cáo |
| Tạm/cache | Runs/facts/gold/staging đã dọn; bổ sung report dùng TemporaryDirectory và tự dọn |


- **Đạt:** S1 hẹp trên 13 câu đã chọn; S2 signed difference hẹp trên synthetic với LLM CUDA → validated QueryPlan → preflight từng window → Decimal executor/verifier → typed output; guard/fallback đúng kỳ vọng trong 12 synthetic và 15 native dev.
- **Chưa đạt/chưa biết:** LLM numeric S2 CPU; numeric S2 trên train; toàn bộ S1 mọi shape và toàn bộ S2; PA/accuracy đại diện toàn dataset; growth/ratios/average/entity/JOIN; latency SLA; Blackwell/BTC/model eligibility.
- Host load, OS cache và swap không được kiểm soát. CPU timeout là kết quả quan sát, không đủ chứng minh nguyên nhân chỉ do RAM hoặc model. Không dùng số latency hiện tại làm benchmark phần cứng có kiểm soát.
- Parser/rule/scope/time guards là hữu hạn; source quotes/hashes chứng minh provenance, không chứng minh hiểu mọi ngoại lệ/paraphrase. Evaluator hiện là `internal-rulebook-v0`, chưa chứng minh tương đương hoàn toàn scorer BTC.

### 8. Dữ liệu đối chiếu và kiểm tra báo cáo

[JSON evidence và đối chiếu từng câu](s2_verification.json) giữ source/model/input hashes, traces, metric gốc và phần `question_review` mới. Gold train được trích đúng 15 ID common-dev, có hash toàn file và hash subset để truy vết; không xuất nhãn các câu khác. Giá trị gold được bổ sung **sau inference**, không được mô tả là model đã biết trước đáp án.

Khi lập bảng đã chạy lại evaluator trên saved predictions cho S1 13 câu, S2 native 2 câu, toàn 15 native, synthetic 12 diagnostic case, synthetic 10 case có gold xác định và CPU 12 case; đối chiếu đủ ID, không missing/malformed, không đổi predictions. Source runtime vẫn khớp toàn bộ hashes của evidence trước lượt lập báo cáo. Đây là cập nhật báo cáo, không phải một lượt inference/model acceptance mới; không chạy lại Docker/CUDA hoặc unit tests cho thay đổi tài liệu.
