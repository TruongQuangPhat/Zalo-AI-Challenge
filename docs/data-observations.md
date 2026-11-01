# Phase 1 — nguồn dữ liệu và giới hạn bằng chứng

Nguồn khảo sát: `dataset/explore/explore.md`, inventory/profile cũ và README train;
đối chiếu lại bằng `scripts.audit_dataset`. Chỉ đọc thêm dữ liệu train.
Không mở thêm giá trị đáp án hoặc gold types; không profile lại toàn bộ dòng.
Output cục bộ: `.artifacts/data_preparation/`; kết quả nghiệm thu nằm trong roadmap/report.

## Inventory và validation

Train có 162 file và 340 bảng vật lý, khớp cả đường dẫn, kích thước/SHA-256 và
tên sheet/table của inventory cũ. CSV được kiểm kê một bảng; XLSX đọc workbook.xml;
SQLite chỉ truy vấn sqlite_master read-only. Phép đối chiếu này không xác nhận lại số dòng,
header, chất lượng cell hoặc quan hệ khóa. Không thay đổi raw inputs.

Fingerprint tập nguồn: `1b069cd54bb66a08e67db2d1b68bcc4a0943e65c775ab8efbbaad6bc0e105aff`.
SHA từng file và mọi question IDs nằm trong [manifest](../config/validation_split.json).
Manifest không chứa câu trả lời; answers.jsonl chỉ được hash như opaque bytes.

| Protocol | Dev shop/câu | Validation shop/câu | Final shop/câu |
|---|---|---|---|
| Business | 16 / 1.658 | 7 / 671 | 7 / 671 |
| Structure | 27 / 2.645 | 2 / 237 | 1 / 118 |

Business protocol ưu tiên so shop mới trong các họ cấu trúc đã có. Structure protocol
giữ cả handwritten_events (105/117) cho validation và mixed_sources (124) cho final.
Đây là hai phép đánh giá riêng: model đã tune/fit trên dev của protocol này không mặc nhiên
hợp lệ với holdout của protocol kia. Phát triển chung ban đầu và pilot chỉ dùng giao hai dev
(15 shop); không dùng 105 để phát triển nếu muốn báo structure validation còn độc lập.
Không dùng feedback final-held-out sau freeze để sửa rồi vẫn gọi là final.

Các họ template được nhóm bảo thủ theo README, grain và cách tách bảng; chưa có chứng cứ
độc lập về generator. Invoice-only và SQLite là singleton, chưa đo same-family unseen-business;
final structure chỉ có một shop, không đủ kết luận rộng về schema generalization.
Không thay split theo score. Nếu về sau bổ sung calibration/train-fit, khóa vai trò trước khi fit;
hiện chưa có calibration set riêng, final không được dùng cho calibration.

EDA cũ đã đọc cả train/public và load toàn bộ labels bằng script. Đã xem phân bố nhãn
và 9 gold POS; mức độ con người xem các gold khác là unknown. POS biz_123 ở dev trong cả
hai protocol. Holdout hiện tại là không dùng thêm feedback để tuning sau freeze, không phải
dữ liệu chưa từng được quan sát. Không dùng public observations/score để tối ưu parser/model.

## Tập đại diện và S1

Mọi nguồn dưới thuộc giao dev. Tên shop là chỉ dẫn bằng chứng offline, không là routing rule
hoặc điều kiện hard-code trong inference. Source paths tương đối với `dataset/train/`.
Profile cũ chỉ được tái dùng sau khi file hash khớp; các kiểm tra mới ghi riêng bên dưới.

| Cấu trúc / nguồn | Grain, khóa và liên kết | Row roles / rủi ro | Coverage và acceptance |
|---|---|---|---|
| **S1: CSV phẳng** — `data/biz_101/ban_hang.csv`, README | Một dòng hàng; `Mã hóa đơn` gắn nhiều dòng; không có STT nên khóa dòng không mặc nhiên là order+product. Đã kiểm tra consistency ngày/status theo order. | sale_item; ngày text `thg`, tiền có dấu phẩy nhóm nghìn; exact dedup trong cùng đơn, không gộp hai đơn giống tiền. | Observed 01/06/2025–24/08/2026; README xác nhận cuối 24/08. Chưa coi observed start là available start. C01–C06, C12, C16–C18, C30. |
| Excel tháng — `data/biz_102/DonHang_ChiTiet.xlsx`, `DanhSachQuyDoi.xlsx` | Dòng hàng có mã đơn, tên sản phẩm nối quy đổi; order có thể qua phần xuất. 18 sheet bán; khóa quy đổi là tên+unit, cần xác minh match/collision. | sale_item/catalog; đọc đủ tháng; cột Tổng tiền là thành tiền dòng theo README, không phải tổng đơn. | README cuối 16/06/2026; ngày thiếu năm cần sheet/year; available start chưa xác nhận. C01–C04, C13, C17, C27. |
| POS Excel nhiều phần — `data/biz_106/DanhSachHoaDon.xlsx`, `DanhSachHoaDonChiTiet_1.xlsx`, `_2.xlsx` | Header order, detail order+STT ứng viên; detail N:1 header; payment mỗi lần thu. Match/orphan/cardinality chưa tái tính. | order header + sale_item + payment; profile header dòng 3, 2.496/734 duplicate candidates trong hai detail; tổng/thu bị nhân sau JOIN. | README cuối 14/09/2026; snapshot kho đúng cuối kỳ, không thay lịch sử. C03–C04, C09–C10, C13, C27. |
| Sổ không mã đơn — `data/biz_107/SoBanHang.xlsx` | Dòng ghi nhận hàng, không có order/customer key; giữ dòng giống nhau (5.756 duplicate candidates raw). | sale_item và **summary**: B62 tiền tổng, E62 “Tổng ngày 01/03”; không fill số/tiền từ summary. 526 dòng trống các trường giao dịch trong profile cần phân loại trước aggregate. | README cuối 22/07/2026; ngày thiếu năm, chưa chứng minh coverage đầy đủ. Count orders unsupported. C02, C05, C18, C30; P1 summary fixture. |
| Header-only — `data/biz_110/danh_sach_hoa_don.csv` | Một invoice với `Mã bill`; `Tien hang` đúng nghĩa tiền hàng, khác `TongTien` phải trả. 28.888 mã trong profile; không có detail để ranking sản phẩm. | order header và **summary**: CSV row 86 có Bên mua “Cộng ngày 01/05/2025”, mã/ngày/status trống. 429 dòng thiếu mã/ngày/status theo profile cần phân loại, không tạo order giả. | README cuối 03/07/2026; nợ snapshot không làm lịch sử. C03–C05, C10, C17–C18, summary fixture. |
| CSV relational — `data/biz_111/{don_hang,chi_tiet_don,san_pham,khach_hang,thanh_toan}.csv` | Order `Mã chứng từ` → detail `Ma don` N:1; detail `ID SP` → product `SKU`; payment `Số HĐ` → order; tên cột khác nhau, chưa tái tính match rate. | Header/detail/catalog/payment; detail 106.585 dòng, header 66.469 theo profile, STT riêng; pre-aggregate payment/detail trước JOIN tổng. | README cuối 19/08/2026; as-of payment khác order date. C04, C09, C12–C15, C18. |
| Nhà thuốc lô — `data/biz_119/` (invoice/detail/lot/allocation/drug XLSX) | Lot ID; sale-lot qua order+STT; một sale dùng nhiều lô. Inventory snapshot khác lịch sử lot. FK/cardinality ứng viên chưa tái xác minh. | sale_item/catalog/purchase_item; lot allocation; 1.908 duplicate detail candidates; không cộng lượng bán theo từng allocation sau JOIN. | README cuối 13/07/2026; tồn snapshot cùng mốc. C09–C15, C25–C27, fallback khi history thiếu. |
| Export kế thừa nhóm — `data/biz_121/DonHang_ChiTiet.xlsx`, `DuLieuXuat.xlsx` | Header order chỉ ghi dòng đầu; tên sản phẩm ↔ master SKU; order-lot nhiều-nhiều. Phải restore group trước dedup; chưa xác minh uniqueness của tên. | Sale/catalog/purchase/allocation. Header dòng 3; raw row 6–7 chỉ có E:I là dòng tiếp cùng nhóm. 38.878 ô trống header mỗi cột, 35.380 duplicate candidates không tự là lỗi. | README cuối 20/09/2026; ngày trả/lô khác ngày bán. C03–C04, C06, C09, C12–C14. |
| POS SQLite — `data/biz_123/pos.sqlite`, README | Orders MaHoaDon; details MaHoaDon+SoThuTu; payment MaPhieuThu; stock MaGiaoDichKho; lot MaLo. Khóa luôn có split/business namespace. | Các event/fact tách riêng; 1.208 duplicate details theo EDA. TonCuoi là balance sau event, không SUM; COGS theo unit bán. | README cuối 28/06/2026; EDA đã parse observed order 01/01/2025–28/06/2026; history ordering/cardinality khác chưa chứng minh toàn bộ. C01–C06, C09–C15, C18, C27, C30. |

**Kiểm tra mới trên S1:** quét đúng các cột cần để kiểm tra format/khóa, không tính doanh thu:
56.501 dòng, 22.428 mã đơn, 0 trường order/date/status/amount trống, 0 đơn mâu thuẫn raw
ngày/status. Mọi ngày dạng `d thg m yyyy`; mọi amount dạng nhóm nghìn bằng dấu phẩy.
Raw statuses Xong/Đã hủy/Đổi trả/Đang giao ánh xạ dự kiến completed/cancelled/returned/shipping,
phải qua fixtures trước khi dùng runtime. Không xem nhãn để quyết định mapping.
Output `.artifacts/data_preparation/sales_source_evidence.json` ghi hash nguồn và phạm vi kiểm tra.

Observed span S1 chưa đủ chứng minh available start; contract phải giữ uncertainty.
S1 chưa hỗ trợ trả lời thật. Việc chọn nguồn không cho phép hard-code số đơn/tổng/dates vào runtime.
Các quan hệ chưa tái đo ở bảng trên được giữ candidate, không nâng thành capability supported.

## Từ vựng lỗi và xử lý dự kiến

| Code | Ví dụ synthetic | Ảnh hưởng / policy / fixture |
|---|---|---|
| `date_format` | `1 thg 6 2025`, serial 45658, `01/06` trong sheet 2025 | Parse theo date role/date-system/year; chưa đủ căn cứ → abstain metric phụ thuộc. C02. |
| `money_locale` | `1.600đ`, `4,32tr`, `1tr340`, `10,000.0000` | Parse theo source convention, Decimal; `1,234` thiếu convention giữ ambiguous. C01. |
| `unit_basis` | 2 thùng × 24 lon, giá vốn/thùng | Giữ sale/base unit; không nhân base quantity với sale cost. C13–C14. |
| `native_type` | ID `00123`, phone `0900000000`, bool true | Chuỗi định danh không qua float; native bool khác number. C28. |
| `group_inheritance` | Order header dòng đầu, dòng hàng sau để trống khách/status | Chỉ fill trường header trong ranh giới group có căn cứ; không fill SL/tiền. C03. |
| `duplicate_candidate` | Hai bản xuất cùng order+STT và raw fields; order khác cùng tiền | Exact within-order dedup có lineage; không gộp sổ no-order. C04–C05. |
| `status_note` | “đã trả một phần” trong ghi chú, khác dòng voucher âm | Phân giải theo rule/README; conflict → ambiguous, chặn metric. C06–C07. |
| `source_summary` | Hàng “Tổng ngày” có tiền và thiếu order/date | Nhận diện summary trước group fill; loại khỏi facts, giữ source ref. P1-SUMMARY. |
| `excel_error` | Cột phụ `#N/A`, cột amount native error | Lỗi phụ không bỏ hàng; amount error không thành 0/tổng thiếu. P1-ERROR, C18. |
| `identity_collision` | Hai mã khách cùng tên; tên không dấu khớp nhiều mã | Không fuzzy merge; ambiguous có candidates và fallback. P1-IDENTITY. |

Không biến tên “cùng nghĩa” thành quan hệ khóa đã xác minh. Danh sách case và fixture
synthetic ở [acceptance-cases](acceptance-cases.md); mọi expected value nghiệp vụ chưa có engine
vẫn là spec planned. Không lấy các tổng/source statistics ở tài liệu này làm câu trả lời runtime.
