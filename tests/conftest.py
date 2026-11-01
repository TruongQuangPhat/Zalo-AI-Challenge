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
