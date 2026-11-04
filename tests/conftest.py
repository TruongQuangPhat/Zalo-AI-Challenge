from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def synthetic_split(tmp_path: Path) -> Path:
    root = tmp_path / "input"
    business = root / "data" / "shop_a"
    business.mkdir(parents=True)
    (business / "README.md").write_text("Dữ liệu synthetic, không có nhãn dùng inference.")
    (business / "sales.csv").write_text("order_id,amount\norder_1,100000\n")
    (root / "rulebook.md").write_text("Đúng +1, sai -1, abstain 0. Hôm nay theo câu hỏi.")
    with (root / "calendar.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["date", "weekday", "lunar_day", "lunar_month", "lunar_year", "lunar_leap", "holiday"]
        )
        for i in range(3):
            day = date(2026, 1, 1) + timedelta(days=i)
            writer.writerow([day.isoformat(), day.isoweekday(), 13 + i, 11, 2025, "false", ""])
    questions = [
        {
            "id": qid,
            "business_id": "shop_a",
            "question": "Hôm nay bán được bao nhiêu tiền?",
            "as_of_date": "2026-01-02",
        }
        for qid in ["q_one", "q_two"]
    ]
    (root / "questions.jsonl").write_text(
        "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions)
    )
    # Intentionally invalid: reading this for inference is a defect.
    (root / "answers.jsonl").write_text("LABEL_READ_IS_FORBIDDEN_NOT_JSON\n")
    return root


@pytest.fixture
def rulebook_text() -> str:
    # Synthetic rules; no dataset values or question/answer records.
    return (
        "Hôm nay của mỗi câu là trường `as_of_date` trong `questions.jsonl`. Mọi cách nói"
        " tương đối tính theo ngày này. Riêng trạng thái huỷ, trả hàng của đơn lấy theo "
        "trạng thái ghi trên đơn, kể cả khi phiếu trả ghi ngày sau hôm nay.\n"
        "## 1. Bán hàng\n"
        "| Luật | Mặc định |\n"
        "|---|---|\n"
        "| Đơn tính vào doanh thu | đơn không huỷ và không trả hàng. Đơn có trả hàng, kể "
        "cả chỉ trả một phần, là đơn trả hàng: bỏ cả đơn. Đơn đang giao vẫn tính |\n"
        "| Số đơn | chỉ tính đơn không huỷ, không trả; cả khi đếm số đơn có một mặt hàng,"
        " số đơn của một khách. Khách đã mua trong kỳ là khách có ít nhất một đơn như vậy"
        " trong kỳ |\n"
        "| Đơn thuộc kỳ nào | theo ngày đặt, không theo ngày giao hay ngày thu tiền |\n"
        "| Doanh thu một đơn | tổng thành tiền các dòng hàng (đã trừ giảm giá trên từng "
        "dòng). Không trừ giảm giá trên cả đơn (khuyến mãi theo tổng tiền đơn, voucher "
        "shop), không cộng thuế VAT, không gồm phí giao hàng. Tiền hàng, tiền bán của đơn"
        " bán cùng nghĩa với doanh thu |\n"
        "## 2. Thời gian\n"
        "| Chênh lệch, hơn / kém, tăng / giảm bao nhiêu (số, không phải %) | "
        "kỳ hỏi − kỳ so: ít hơn ra số âm, giữ đơn vị của số đo |\n"
        "| Phần trăm thay đổi, tăng trưởng | so với kỳ trước; giảm thì ra số âm |\n"
        "| Tỉ lệ đơn huỷ trong kỳ | phần trăm đơn huỷ trong mọi đơn đặt trong kỳ "
        "(theo ngày đặt, mọi trạng thái: cả đơn huỷ, đơn trả, đơn đang giao). "
        "Làm tròn 2 chữ số thập phân. |\n"
        "| Giá trị trung bình một đơn | làm tròn về đồng, nửa đồng làm tròn lên |\n"
        "| Doanh thu theo nhóm khách | nhóm theo cột nhóm khách. Đơn không ghi khách "
        "(khách vãng lai) không thuộc nhóm nào, kể cả nhóm lẻ. Tỉ trọng của nhóm chia "
        "cho doanh thu cả shop cùng kỳ, tính cả đơn vãng lai |\n"
        "| Mặt hàng | Món, hàng, sản phẩm cùng nghĩa với mặt hàng. "
        "Dòng Phí phục vụ không phải mặt hàng: "
        "không xếp hạng, không đếm. Vẫn tính vào doanh thu, kể cả mẫu số tỉ trọng nhóm hàng |\n"
        "| Nhóm hàng | theo cột nhóm hàng, đúng tên nhóm câu hỏi nêu; không gộp nhóm "
        "khác tên dù tên gần nhau |\n"
        "| Từ X đến Y, nhóm X–Y | gồm cả X và Y |\n"
        "| N ngày gần nhất | N ngày liền nhau kể cả hôm nay, ngày cuối là hôm nay |\n"
        "| Ca sáng, ca chiều | theo giờ tạo đơn: ca sáng 06:00–13:59, ca chiều 14:00–21:59. "
        "So ca chiều với ca sáng: mốc là ca sáng |\n"
        "| Tuần | bắt đầu thứ Hai. Tuần này: từ thứ Hai của tuần chứa hôm nay đến hết hôm"
        " nay. Tuần trước, tuần rồi: cả tuần liền trước, thứ Hai đến Chủ nhật |\n"
        "| Tháng M/Y | cả tháng, từ ngày 1 đến hết ngày cuối tháng |\n"
        "| Tháng này | từ ngày 1 đến hết hôm nay. So với tháng trước thì lấy cùng số ngày"
        " của tháng trước: từ ngày 1 đến hết ngày trùng số với hôm nay; tháng trước không"
        " có ngày đó thì đến hết tháng trước |\n"
        "| Từ đầu năm, từ đầu năm tới nay | từ 01/01 của năm nay đến hết hôm nay |\n"
        "| Tháng M, quý Q không ghi năm | của năm nay nếu không sau hôm nay; sau hôm nay "
        "thì của năm trước. Quý theo dương lịch, mỗi quý 3 tháng tính từ tháng 1 |\n"
        "| Tết năm Y, dịp Tết, mùa Tết | từ ngày dương của 23 tháng Chạp (năm âm Y − 1) "
        "đến hết ngày dương của mùng 10 tháng Giêng (năm âm Y). So với trước Tết = cùng "
        "số ngày ngay trước 23 tháng Chạp. Tra lịch âm trong `calendar.csv` cùng thư mục."
        " |\n"
        "## 7. Không trả lời được\n"
        "- Không có dữ liệu khác với bằng 0: kỳ nằm hoàn toàn ngoài khoảng ngày có dữ "
        "liệu của shop, hoặc shop không ghi loại thông tin câu hỏi cần, thì không trả lời"
        " được, không phải 0.\n"
        "- Kỳ chỉ có một phần nằm trong khoảng ngày có dữ liệu thì tính trên phần có dữ "
        "liệu.\n"
        "## 8. Cách ghi\n"
        "| Hai dòng của cùng một đơn giống nhau ở mọi ô | lỗi xuất file, tính một lần. "
        "Hai dòng giống nhau của hai đơn khác nhau là hai lần bán, tính cả hai |\n"
        "| Ô để trống ở các dòng sau của cùng một nhóm dòng | giống ô đó ở dòng đầu nhóm:"
        " khách, ghi chú, trạng thái của đơn áp cho mọi dòng của đơn. Không phải thiếu dữ"
        " liệu, không có nghĩa là khách lẻ hay đơn hoàn thành |\n"
        "## 9. Output\n"
        "Câu hỏi nhiều số thì ghi list theo đúng thứ tự câu hỏi nêu.\n"
        "- Làm tròn: tiền ra số lẻ làm tròn về đồng; trung bình của một khoản tiền cũng "
        "là tiền, làm tròn về đồng. Phần trăm và số trung bình khác làm tròn 2 chữ số "
        "thập phân, trừ khi một luật khác trong rulebook ghi khác.\n"
    )


@pytest.fixture
def semantic_source(synthetic_split, tmp_path, rulebook_text):
    from src.data.catalog import prepare_business
    from src.data.normalization import prepare_facts
    from src.data.split import load_split
    from src.semantics.answerability import prepare_semantics

    revision = 0

    def prepare(
        *,
        rows=None,
        complete=True,
        rules=None,
        readme_extra="",
        cache_dir=None,
        coverage_end="03/01/2026",
        extra_columns=(),
    ):
        nonlocal revision
        revision += 1
        business = synthetic_split / "data/shop_a"
        readme = f"""One order line per row.
- **order_id**: order id
- **date**: order date
- **status**: order status
- **amount**: line net amount
- **item**: product name
Tiền dùng dấu chấm thập phân, không nhóm nghìn.
Dữ liệu có đến hết {coverage_end}.
"""
        if complete:
            readme += f"Dữ liệu đầy đủ từ 01/01/2026 đến hết {coverage_end}.\n"
        (business / "README.md").write_text(readme + readme_extra)
        (synthetic_split / "rulebook.md").write_text(rulebook_text if rules is None else rules)
        with (business / "sales.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["order_id", "date", "status", "amount", "item", *extra_columns])
            writer.writerows(
                rows
                if rows is not None
                else [
                    ["001", "01/01/2026", "Xong", "100.25", "Item A"],
                    ["002", "03/01/2026", "Đang giao", "50.25", "Item B"],
                    ["003", "03/01/2026", "Đổi trả", "30", "Item C"],
                ]
            )
        context = prepare_business(load_split(synthetic_split), "shop_a", cache_dir=cache_dir)
        prepare_facts(context, tmp_path / f"semantic-prepared-{revision}", cache_dir=cache_dir)
        prepare_semantics(context)
        return context

    return prepare
