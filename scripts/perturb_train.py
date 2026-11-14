"""Write train variants whose README/file layout the fixed README mappers no longer read.

Each variant is a complete split (rulebook, calendar, questions, answers, data) under
``--out-dir/<variant>`` with the original business IDs, so ``scripts.eval_rule_ops`` scores
it against the train answers unchanged. Unchanged source files are hard-linked (copied if
linking fails); raw inputs under ``dataset/`` are never written.

    .venv/bin/python -m scripts.perturb_train --data-dir dataset/train \
        --out-dir .artifacts/perturbed

Variants (V2-V4 build on V1):
- ``no_decl``: README keeps its title, file list, column sections and convention bullets,
  but loses every prose declaration (grain, repeated headers, monthly sheets, POS export,
  ledger/header-only grain) and the negative-quantity return bullet.
- ``synonyms``: column meanings rewritten with fixed synonyms outside the mapper vocabulary.
- ``no_columns``: every ``## Cột trong`` section removed; only headers and data remain.
- ``monthly_csv``: single-CSV shops split byte-for-byte into one file per sale month.
- ``status_words``: README untouched; order-status cells (CSV/XLSX columns whose header reads
  as a status) rewritten with fixed everyday wordings outside the status vocabulary.
- ``vn_numbers``: README untouched; integer money cells (header reads as money) written as
  Vietnamese dot-grouped text, e.g. ``1.234.000``.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import shutil
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path

from src.data.normalization.entities import EXTRA_MEANINGS
from src.data.normalization.relational import MEANINGS, token
from src.data.normalization.sales import is_walk_in
from src.data.normalization.stock import FIELDS as stock_fields
from src.data.normalization.values import (
    STATUSES,
    ParseError,
    normalize_status,
    parse_date,
    summary_label,
)
from src.data.profiling import normalized
from src.data.profiling.profiler import HEADER_WORDS
from src.semantics.rulebook import plain

VARIANTS = (
    "no_decl",
    "synonyms",
    "no_columns",
    "monthly_csv",
    "status_words",
    "vn_numbers",
    "short_headers",
    "summary_rows",
    "padded_codes",
    "qty_with_unit",
    "repeated_headers",
    "mixed_dates",
    "unaccented_names",
    "chat_questions",
    "status_in_notes",
    "non_item_rows",
    "template_names",
    "abbreviations",
    "footer_rows",
    "phrasing_questions",
)
_BULLET = re.compile(r"^(\s*-\s+\*\*)(.+?)(\*\*\s*:\s*)(.+)$")
_SECTION = re.compile(r"^## Cột trong `")
# Plain-text column meaning -> wording a shop might use instead. Written from everyday
# Vietnamese retail wording, never from public/private README files.
SYNONYMS = {
    "ma don": "số phiếu bán",
    "ngay ban": "ngày lập phiếu",
    "trang thai don": "tình trạng phiếu",
    "thanh tien sau giam": "số tiền của dòng, đã trừ chiết khấu",
    "ten hang": "mặt hàng",
    "ten": "mặt hàng",
    "ten hang (file chi co mat hang co don vi lon)": "mặt hàng (chỉ mặt hàng có đơn vị lớn)",
    "so luong": "SL bán",
    "ma hang": "mã SP",
    "ma khach": "mã KH",
    "ma khach (trong neu khach le)": "mã KH (bỏ trống nếu khách vãng lai)",
    "ten khach (khach le neu khong luu)": "người mua (ghi khách lẻ nếu không lưu)",
    "nhom hang": "loại hàng",
    "nhom khach": "phân loại khách",
    "kenh ban": "nguồn bán",
    "don vi tinh": "ĐVT",
    "don vi co ban": "ĐVT nhỏ nhất",
    "don vi lon (thung, hop, tan...)": "ĐVT lớn",
    "so luong da doi ra don vi co ban": "SL quy về ĐVT nhỏ nhất",
    "he so: so don vi co ban trong 1 don vi lon": "1 ĐVT lớn bằng bao nhiêu ĐVT nhỏ nhất",
    "tong tien don": "khách phải trả cho cả phiếu",
    "hinh thuc thanh toan cua don: cash tien mat, transfer chuyen khoan, "
    "ewallet vi dien tu, cod giao hang thu tien, debt ghi no": (
        "cách trả tiền (tiền mặt, chuyển khoản, ví, COD, ghi nợ)"
    ),
    "thoi diem dat don (ngay gio)": "lúc tạo phiếu",
    "gio dat don (dong tra hang: luc tra)": "giờ tạo phiếu (dòng trả hàng: giờ trả)",
    "so luong khach tra lai (don vi co ban); lon hon 0: don do co tra hang": (
        "SL khách trả lại (ĐVT nhỏ nhất)"
    ),
    "tien hang cua don: cong thanh tien cac mat hang, truoc giam gia cua don, chua gom phi giao": (
        "tiền hàng của phiếu, chưa trừ giảm giá phiếu, chưa có phí ship"
    ),
    "ma phieu thu": "số phiếu thu",
    "thoi diem thu": "lúc thu tiền",
    "so tien": "số tiền thu",
}
# Canonical status -> everyday wordings a shop might export instead (not from public/private).
STATUS_SYNONYMS = {
    "completed": ["Giao thành công", "Đã chốt", "Hoàn tất", "Đã bán"],
    "cancelled": ["Bị hủy", "Hủy bỏ", "Đơn hủy"],
    "returned": ["Khách trả lại", "Hoàn đơn", "Trả lại hàng"],
    "shipping": ["Đang chuyển hàng", "Chờ lấy hàng", "Đang đi giao"],
}
assert not {
    normalized(w).strip() for words in STATUS_SYNONYMS.values() for w in words
} & set().union(*STATUSES.values()), "status synonym is vocabulary"
_STATUS_HEADER = re.compile(r"trang thai|tinh trang|status|ket qua|xu ly")
_MONEY_HEADER = re.compile(r"\b(?:tien|gia|tong|cong no|no|amount|price|total)\b")
_CELL = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
_KNOWN = {m for values in [*MEANINGS.values(), *EXTRA_MEANINGS.values()] for m in values}
assert not {plain(s).rstrip(".") for s in SYNONYMS.values()} & _KNOWN, "synonym is vocabulary"


def drop_declarations(readme: str) -> str:
    """Remove prose declarations; headings, bullets and backtick-only lists survive."""
    out = []
    for line in readme.splitlines():
        stripped = line.strip()
        if line.startswith("# ") and not out:
            out += [line, "", "Dữ liệu bán hàng của tiệm."]
            continue
        if stripped and not stripped.startswith(("#", "-")):
            if not re.fullmatch(r"(`[^`]+`,?\s*)+", stripped):
                continue
        elif "so luong am" in plain(line):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def rename_meanings(readme: str) -> str:
    out, in_section = [], False
    for line in readme.splitlines():
        if line.startswith("#"):
            in_section = bool(_SECTION.match(line))
        match = _BULLET.match(line) if in_section else None
        if match:
            synonym = SYNONYMS.get(plain(match[4]).strip().rstrip("."))
            if synonym:
                line = match[1] + match[2] + match[3] + synonym
        out.append(line)
    return "\n".join(out) + "\n"


def drop_column_sections(readme: str) -> str:
    out, skip = [], False
    for line in readme.splitlines():
        if line.startswith("#"):
            skip = bool(_SECTION.match(line))
        if not skip:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def template_sections(readme: str) -> str:
    """Sections of numbered parts (``X_1.xlsx``, ``X_2026.xlsx``) with identical bodies
    become one section named by a template, ``X_<phần>.xlsx``."""
    parts = re.split(r"(?m)^(?=## )", readme)
    groups: dict[str, list[int]] = {}
    for i, part in enumerate(parts):
        match = re.match(r"## Cột trong `(.+)_(\d+)(\.[a-z]+)`\s*\n", part)
        if match:
            groups.setdefault(match[1] + match[3], []).append(i)
    drop = set()
    for key, indexes in groups.items():
        bodies = {parts[i].split("\n", 1)[1].strip() for i in indexes}
        if len(indexes) < 2 or len(bodies) != 1:
            continue
        stem, suffix = key.rsplit(".", 1)
        first = parts[indexes[0]]
        parts[indexes[0]] = f"## Cột trong `{stem}_<phần>.{suffix}`\n" + first.split("\n", 1)[1]
        drop.update(indexes[1:])
    return "".join(p for i, p in enumerate(parts) if i not in drop)


def replace_file_list(readme: str, names: list[str]) -> str:
    listing = "## File\n" + "".join(f"- `{n}` (csv)\n" for n in names)
    return re.sub(r"^## File\n(?:- .*\n?)*\n*", listing + "\n", readme, flags=re.M)


def split_csv_by_month(data: bytes, date_column: str) -> dict[str, bytes]:
    """Return {"BanHang_T05_2025.csv": bytes}; rows keep their exact source bytes.

    Rows whose date does not parse (blank/summary rows) stay with the preceding month.
    """
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    text = data[len(bom) :].decode("utf-8")
    consumed: list[str] = []

    def lines():
        for line in io.StringIO(text, newline=""):
            consumed.append(line)
            yield line

    reader = csv.reader(lines())
    header = next(reader)
    header_raw = "".join(consumed)
    consumed.clear()
    position = header.index(date_column)
    parts: dict[str, list[str]] = {}
    current = None
    for row in reader:
        raw = "".join(consumed)
        consumed.clear()
        try:
            day = parse_date(row[position])
            current = f"BanHang_T{day.month:02d}_{day.year}.csv"
        except (ParseError, ValueError, IndexError):
            pass
        parts.setdefault(current, []).append(raw)
    if None in parts:  # leading undated rows join the first month
        first = next(k for k in parts if k is not None)
        parts[first] = parts.pop(None) + parts[first]
    return {name: bom + (header_raw + "".join(rows)).encode() for name, rows in parts.items()}


def _date_column(readme: str) -> str | None:
    found = [
        m[2]
        for m in map(_BULLET.match, readme.splitlines())
        if m and plain(m[4]).strip().rstrip(".") == "ngay ban"
    ]
    return found[0] if len(found) == 1 else None


def _link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def perturb_business(source: Path, target: Path, variant: str) -> dict:
    readme = (source / "README.md").read_text(encoding="utf-8-sig")
    files = sorted(p for p in source.iterdir() if p.name != "README.md")
    new = drop_declarations(readme)
    steps: dict[str, Callable[[str], str]] = {
        "synonyms": rename_meanings,
        "no_columns": drop_column_sections,
        "monthly_csv": drop_column_sections,
    }
    if variant in steps:
        new = steps[variant](new)
    if variant == "template_names":
        new = template_sections(readme)
        if new == readme:
            return {"skipped": "no numbered-part sections"}
    written = {}
    if variant in {"status_words", "vn_numbers"}:
        new, index, changed = readme, int(re.sub(r"\D", "", source.name) or 0), 0
        rewriters = {".csv": rewrite_csv_statuses, ".xlsx": rewrite_xlsx_statuses}
        if variant == "vn_numbers":
            rewriters = {
                ".csv": rewrite_csv_money,
                ".xlsx": lambda d, i: rewrite_xlsx_statuses(d, i, _rewrite_money_sheet),
            }
        for path in list(files):
            rewrite = rewriters.get(path.suffix.lower())
            if rewrite is None:
                continue
            data, count = rewrite(path.read_bytes(), index)
            if count:
                written[path.name], changed = data, changed + count
                files.remove(path)
        if changed < (1000 if variant == "vn_numbers" else 1):  # a few cells test nothing
            return {"skipped": f"no {variant} cells found"}
    if variant == "monthly_csv":
        column = _date_column(readme)
        if len(files) != 1 or files[0].suffix.lower() != ".csv" or column is None:
            return {"skipped": "not a single dated CSV"}
        written = split_csv_by_month(files[0].read_bytes(), column)
        new = replace_file_list(new, sorted(written))
        files = []
    target.mkdir(parents=True)
    for path in files:
        _link(path, target / path.name)
    for name, data in written.items():
        (target / name).write_bytes(data)
    (target / "README.md").write_text(new, encoding="utf-8")
    return {"readme_changed": new != readme, "files": len(files) + len(written)} | (
        {"changed_cells": changed} if variant in {"status_words", "vn_numbers"} else {}
    )


def _status_header(text) -> bool:
    return bool(_STATUS_HEADER.search(normalized(str(text)).replace("_", " ")))


def _status_synonym(text, index: int) -> str | None:
    token = normalized(str(text)).strip()
    for status, words in STATUSES.items():
        if token in words:
            return STATUS_SYNONYMS[status][index % len(STATUS_SYNONYMS[status])]
    return None


def _money_header(text) -> bool:
    return bool(_MONEY_HEADER.search(normalized(str(text)).replace("_", " ")))


def _grouped(text) -> str | None:
    token = str(text).strip()
    if re.fullmatch(r"-?\d{4,}", token):
        return f"{int(token):,}".replace(",", ".")
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", token):
        return token.replace(",", ".")
    return None


def rewrite_csv_money(data: bytes, index: int) -> tuple[bytes, int]:
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    text = data[len(bom) :].decode("utf-8")
    ending = "\r\n" if "\r\n" in text[:4096] else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return data, 0
    columns = [j for j, cell in enumerate(rows[0]) if _money_header(cell)]
    changed = 0
    for row in rows[1:]:
        for j in columns:
            if j < len(row) and (new := _grouped(row[j])):
                row[j], changed = new, changed + 1
    out = io.StringIO()
    csv.writer(out, lineterminator=ending).writerows(rows)
    return bom + out.getvalue().encode("utf-8"), changed


def _rewrite_money_sheet(xml: str, shared: list[str], index: int) -> tuple[str, int]:
    cells = list(_CELL.finditer(xml))
    first = min((int(m[2]) for m in cells), default=0)

    def text(m) -> str | None:
        if 't="inlineStr"' in m[3]:
            parts = re.findall(r"<t[^>]*>(.*?)</t>", m[4] or "", re.S)
            return html.unescape("".join(parts)) if parts else None
        if 't="s"' in m[3] and (v := re.search(r"<v>(\d+)</v>", m[4] or "")):
            return shared[int(v[1])]
        return None

    columns = {m[1] for m in cells if int(m[2]) == first and _money_header(text(m) or "")}
    changed = 0

    def swap(m: re.Match) -> str:
        nonlocal changed
        if m[1] not in columns or int(m[2]) == first or 't="' in m[3] and 't="n"' not in m[3]:
            return m[0]
        value = re.search(r"<v>([^<]*)</v>", m[4] or "")
        new = _grouped(value[1]) if value else None
        if new is None:
            return m[0]
        changed += 1
        attrs = re.sub(r'\s+t="[^"]*"', "", m[3])
        return f'<c r="{m[1]}{m[2]}"{attrs} t="inlineStr"><is><t>{new}</t></is></c>'

    return _CELL.sub(swap, xml), changed


def rewrite_csv_statuses(data: bytes, index: int) -> tuple[bytes, int]:
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    text = data[len(bom) :].decode("utf-8")
    ending = "\r\n" if "\r\n" in text[:4096] else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    header = next((i for i, r in enumerate(rows[:15]) if any(map(_status_header, r))), None)
    if header is None:
        return data, 0
    columns = [j for j, cell in enumerate(rows[header]) if _status_header(cell)]
    changed = 0
    for row in rows[header + 1 :]:
        for j in columns:
            if j < len(row) and (new := _status_synonym(row[j], index)):
                row[j], changed = new, changed + 1
    out = io.StringIO()
    csv.writer(out, lineterminator=ending).writerows(rows)
    return bom + out.getvalue().encode("utf-8"), changed


def _rewrite_sheet(xml: str, shared: list[str], index: int) -> tuple[str, int]:
    def text(attrs: str, body: str | None) -> str | None:
        if 't="inlineStr"' in attrs:
            parts = re.findall(r"<t[^>]*>(.*?)</t>", body or "", re.S)
            return html.unescape("".join(parts)) if parts else None
        if 't="s"' in attrs and (m := re.search(r"<v>(\d+)</v>", body or "")):
            return shared[int(m[1])]
        return None

    cells = list(_CELL.finditer(xml))
    header_row, columns = None, set()
    for m in cells:
        if int(m[2]) <= 15 and (value := text(m[3], m[4])) and _status_header(value):
            if header_row in (None, int(m[2])):
                header_row = int(m[2])
                columns.add(m[1])
    if header_row is None:
        return xml, 0
    changed = 0

    def swap(m: re.Match) -> str:
        nonlocal changed
        if m[1] not in columns or int(m[2]) <= header_row:
            return m[0]
        value = text(m[3], m[4])
        new = _status_synonym(value, index) if value is not None else None
        if new is None:
            return m[0]
        changed += 1
        attrs = re.sub(r'\s+t="[^"]*"', "", m[3])
        return f'<c r="{m[1]}{m[2]}"{attrs} t="inlineStr"><is><t>{html.escape(new)}</t></is></c>'

    return _CELL.sub(swap, xml), changed


def rewrite_xlsx_statuses(data: bytes, index: int, sheet=None) -> tuple[bytes, int]:
    source = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in source.namelist():
        xml = source.read("xl/sharedStrings.xml").decode("utf-8")
        shared = [
            html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
            for si in re.findall(r"<si>(.*?)</si>", xml, re.S)
        ]
    out, changed = io.BytesIO(), 0
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            raw = source.read(info.filename)
            if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                xml, count = (sheet or _rewrite_sheet)(raw.decode("utf-8"), shared, index)
                raw, changed = xml.encode("utf-8"), changed + count
            target.writestr(info, raw)
    return (out.getvalue(), changed) if changed else (data, 0)


def check_mappings(split_dir: Path, work_dir: Path, businesses: list[str]) -> dict:
    """Which fixed mapper (if any) still reads each business; catalogs only, no facts."""
    from src.data.catalog import prepare_business
    from src.data.normalization.relational import relational_mapping
    from src.data.normalization.sales import sales_mapping
    from src.data.normalization.stock import stock_mapping
    from src.data.split import load_split

    split, result = load_split(split_dir), {}
    for business in businesses:
        context = prepare_business(split, business, cache_dir=work_dir / "cache")
        result[business] = "unsupported"
        for name, mapper in [
            ("sales", sales_mapping),
            ("relational", relational_mapping),
            ("snapshot", stock_mapping),
        ]:
            try:
                mapper(context)
            except Exception:  # noqa: BLE001 - any mapper failure means "not read"
                continue
            result[business] = name
            break
        print(f"{split_dir.name} {business}: {result[business]}", flush=True)
    return result


# Standard wording -> chat/regional spellings (everyday Vietnamese, not from test questions).
CHAT_FORMS = [
    ("đơn hàng", ["đh"]),
    ("bao nhiêu", ["bnhiu", "nhiêu", "bn"]),
    ("hôm nay", ["hnay"]),
    ("hôm qua", ["hqua"]),
    ("tháng này", ["tháng ni"]),
    ("được", ["đc"]),
    ("không", ["ko", "hông"]),
    ("nào", ["mô"]),
    ("nhất", ["nhứt"]),
    ("còn", ["kòn"]),
    ("giùm", ["jùm"]),
    ("với", ["dới"]),
    ("vậy", ["z", "dậy"]),
    ("doanh thu", ["dthu"]),
    ("nhiều", ["nhìu"]),
    ("trước", ["trc"]),
    ("gì", ["j"]),
]


def chat_question(text: str, index: int) -> str:
    """Rewrite function words of one question with fixed chat spellings."""
    for n, (word, forms) in enumerate(CHAT_FORMS):
        pattern = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.I)
        text = pattern.sub(forms[(index + n) % len(forms)].replace("\\", r"\\"), text)
    return re.sub(r"(?<!\w)[Tt]háng (\d{1,2})(?!\d)", r"th\1", text) if index % 2 else text


def chat_split(data_dir: Path, root: Path) -> int:
    """Questions rewritten in chat spelling; data, rulebook and answers unchanged."""
    root.mkdir(parents=True)
    for business in sorted((data_dir / "data").iterdir()):
        for path in business.iterdir():
            _link(path, root / "data" / business.name / path.name)
    for name in ["rulebook.md", "calendar.csv"]:
        _link(data_dir / name, root / name)
    kept, lines = set(), []
    for i, line in enumerate((data_dir / "questions.jsonl").open(encoding="utf-8")):
        q = json.loads(line)
        new = chat_question(q["question"], i)
        if new != q["question"]:
            kept.add(q["id"])
            lines.append(json.dumps(q | {"question": new}, ensure_ascii=False) + "\n")
    (root / "questions.jsonl").write_text("".join(lines), encoding="utf-8")
    answers = (data_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (root / "answers.jsonl").write_text(
        "".join(a for a in answers if json.loads(a)["id"] in kept), encoding="utf-8"
    )
    return len(kept)


# Same meaning, other everyday wording (rulebook terms; not copied from test questions).
# Each entry: name, pattern, replacement forms (rotated by question index).
PHRASINGS = [
    (
        "last_n_days_today",
        r"(\d+) ngày gần nhất",
        [
            r"\1 ngày qua tính cả hôm nay",
            r"\1 ngày qua (kể cả hôm nay)",
            r"\1 ngày gần đây kể cả hôm nay",
        ],
    ),
    ("shop_owner", r"của cửa hàng", ["của quán", "của tiệm", "của shop"]),
    (
        "sales_money",
        r"(?i)\bdoanh thu (tháng|tuần|hôm|ngày|\d+ ngày|của cửa hàng|trong)",
        [r"tiền bán \1", r"tiền bán hàng \1"],
    ),
    (
        "aov_order",
        r"(?i)\bdoanh thu bình quân mỗi đơn(?: hàng)? là bao nhiêu",
        ["mỗi đơn bình quân bao nhiêu tiền", "mỗi đơn trung bình được bao nhiêu tiền"],
    ),
    (
        "aov_value",
        r"(?i)\bgiá trị trung bình một đơn",
        ["trung bình 1 đơn giá trị", "bình quân trên 1 đơn"],
    ),
    ("money_typo", r"bao nhiêu tiền", ["bao nhiêu tiềng", "bao nhiêu tền", "bao nhiêu tiền"]),
    ("total_typo", r"tổng cộng", ["tog cog", "tổng cộng", "tổng cộg"]),
    ("best_typo", r"nhiều nhất", ["nhiều nhta", "nhiều nhất", "nhìu nhất"]),
    ("month_short", r"(?i)\btháng (này|trước)", [r"th \1", r"tháng \1"]),
    (
        "ytd_now",
        r"(?i)\btừ đầu năm (?:đến|tới) hết (?:hôm nay|hnay)",
        ["đầu năm tới giờ", "từ đầu năm tới nay"],
    ),
    ("until_day", r"(?i)\bđến hết ngày (\d)", [r"đến hết \1"]),
    # Round 4b: words typed together, "tổng bao nhiêu tiền", "đ" against the number.
    ("joined_typo", r"(?i)\bdoanh thu\b", ["doanhthu", "doanh thu", "doanh thu"]),
    ("joined_best", r"\bcao nhất\b", ["caonhất", "cao nhất"]),
    ("joined_day", r"\bngày nào\b", ["ngàynào", "ngày nào"]),
    (
        "money_total",
        r"bán (được|đc) bao nhiêu tiền",
        [r"bán \1 tổng bao nhiêu tiền", r"bán \1 tất cả bao nhiêu tiền"],
    ),
    ("amount_d", r"(\d) đồng\b", [r"\1đ", r"\1 đồng"]),
    ("up_down", r"tăng hay giảm", ["lên hay xuống", "tăng hay giảm"]),
    ("debt_short", r"khách (?:đang )?còn thiếu tiền", ["khách thiếu tiền"]),
    ("debt_unpaid", r"khách chưa trả hết nợ", ["khách nợ chưa trả xong"]),
    (
        "debt_top",
        r"(\d) khách nào đang nợ (nhiều|nhìu) nhất",
        [r"ai đang nợ nhiều nhất, cho top \1"],
    ),
    ("english_total", r"\btổng cộng\b", ["total", "tổng cộng", "tổng cộng"]),
    ("english_revenue", r"(?i)\bdoanh thu\b", ["revenue", "doanh thu", "doanh thu", "doanh thu"]),
    ("best_seller", r"\bbán chạy nhất\b", ["best seller", "bán chạy nhất"]),
    ("cash_slang", r"\btiền mặt\b", ["tiền tươi", "tiền mặt"]),
    ("transfer_short", r"\bchuyển khoản\b", ["ck", "chuyển khoản"]),
    ("otc_slang", r"\bkhông kê đơn\b", ["không cần đơn bác sĩ", "không kê đơn"]),
    ("stock_in_slang", r"\bnhập hàng\b", ["đánh hàng", "cất hàng", "nhập hàng"]),
]


def phrasing_question(text: str, index: int, as_of: str) -> tuple[str, list[str]]:
    """Rewrite one question with PHRASINGS and bare months; returns (text, rule names)."""
    used = []
    for n, (name, pattern, forms) in enumerate(PHRASINGS):
        new = re.sub(pattern, forms[(index + n) % len(forms)], text)
        if new != text:
            used.append(name)
            text = new
    # Month without year (rulebook: this year unless after today, else last year), and
    # "năm ngoái" for last year's month; only for a single month mention.
    year, month = int(as_of[:4]), int(as_of[5:7])
    months = list(re.finditer(r"(?i)\btháng (\d{1,2})(?:/| năm )(\d{4})\b", text))
    # Only a question with this one date: "tháng 11, 12/2025" must keep its year.
    if len(months) == 1 and len(re.findall(r"\d/\d|\d{4}", text)) == 1:
        m = months[0]
        value, y = int(m[1]), int(m[2])
        if y == year and value <= month and index % 2:
            text, name = text[: m.start()] + f"tháng {value}" + text[m.end() :], "month_no_year"
            used.append(name)
        elif y == year - 1:
            # A bare month after today is last year's too.
            bare = value > month and index % 2
            form = f"tháng {value}" if bare else f"tháng {value} năm ngoái"
            text = text[: m.start()] + form + text[m.end() :]
            used.append("month_last_year")
    return text, used


def phrasing_split(data_dir: Path, root: Path) -> int:
    """Questions in other everyday wording; data, rulebook and answers unchanged."""
    root.mkdir(parents=True)
    for business in sorted((data_dir / "data").iterdir()):
        for path in business.iterdir():
            _link(path, root / "data" / business.name / path.name)
    for name in ["rulebook.md", "calendar.csv"]:
        _link(data_dir / name, root / name)
    rules, lines = {}, []
    for i, line in enumerate((data_dir / "questions.jsonl").open(encoding="utf-8")):
        q = json.loads(line)
        new, used = phrasing_question(q["question"], i, q["as_of_date"])
        if used:
            rules[q["id"]] = used
            lines.append(json.dumps(q | {"question": new}, ensure_ascii=False) + "\n")
    (root / "questions.jsonl").write_text("".join(lines), encoding="utf-8")
    answers = (data_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (root / "answers.jsonl").write_text(
        "".join(a for a in answers if json.loads(a)["id"] in rules), encoding="utf-8"
    )
    (root / "phrasing.json").write_text(json.dumps(rules, indent=0) + "\n")
    return len(rules)


def _protected_names(data_dir: Path) -> dict[str, set[str]]:
    """Plain text of every question and answer per business; renamed values avoid it."""
    texts: dict[str, list[str]] = {}
    answers = {}
    if (data_dir / "answers.jsonl").exists():
        for line in (data_dir / "answers.jsonl").open(encoding="utf-8"):
            record = json.loads(line)
            answers[record["id"]] = json.dumps(record, ensure_ascii=False)
    for line in (data_dir / "questions.jsonl").open(encoding="utf-8"):
        q = json.loads(line)
        texts.setdefault(q["business_id"], []).append(
            q["question"] + " " + answers.get(q["id"], "")
        )
    return {b: _Contains(plain(" ".join(t))) for b, t in texts.items()}


class _Contains(set):
    """``name in protected`` is a substring test over the business's question/answer text."""

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def __contains__(self, name) -> bool:
        return str(name) in self.text


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    parser.add_argument("--businesses", nargs="+", help="default: every business")
    parser.add_argument(
        "--check-work-dir", type=Path, help="only report which mapper reads existing variants"
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    if args.out_dir.resolve().is_relative_to(data_dir.parent):
        parser.error("--out-dir must be outside the dataset directory")
    businesses = args.businesses or sorted(p.name for p in (data_dir / "data").iterdir())
    if args.check_work_dir:
        report = {}
        for variant in ["original", *args.variants]:
            split_dir = data_dir if variant == "original" else args.out_dir / variant
            present = [b for b in businesses if (split_dir / "data" / b).is_dir()]
            report[variant] = check_mappings(split_dir, args.check_work_dir / variant, present)
        args.check_work_dir.mkdir(parents=True, exist_ok=True)
        (args.check_work_dir / "mappers.json").write_text(json.dumps(report, indent=1) + "\n")
        return
    for variant in args.variants:
        root = args.out_dir / variant
        if root.exists():
            parser.error(f"{root} exists; use a fresh --out-dir")
        if variant == "chat_questions":
            print(f"{variant}: {chat_split(data_dir, root)} questions -> {root}")
            continue
        if variant == "phrasing_questions":
            print(f"{variant}: {phrasing_split(data_dir, root)} questions -> {root}")
            continue
        protected = _protected_names(data_dir)
        report = {
            b: row_variant(
                data_dir / "data" / b, root / "data" / b, variant, protected.get(b, set())
            )
            if variant in ROW_VARIANTS
            else perturb_business(data_dir / "data" / b, root / "data" / b, variant)
            for b in businesses
        }
        kept = {b for b, r in report.items() if "skipped" not in r}
        for name in ["rulebook.md", "calendar.csv"]:
            _link(data_dir / name, root / name)
        if variant == "status_in_notes":
            # The public rulebook's general clause; train shops never needed it.
            rulebook = (data_dir / "rulebook.md").read_text(encoding="utf-8").splitlines()
            at = next(i for i, line in enumerate(rulebook) if "Ô để trống ở các dòng sau" in line)
            rulebook.insert(at + 1, NOTE_CLAUSE)
            (root / "rulebook.md").unlink()
            (root / "rulebook.md").write_text("\n".join(rulebook) + "\n", encoding="utf-8")
        for name in ["questions.jsonl", "answers.jsonl"]:
            if (data_dir / name).exists():
                lines = (data_dir / name).read_text(encoding="utf-8").splitlines(keepends=True)
                ids = {
                    json.loads(q)["id"]
                    for q in (data_dir / "questions.jsonl").open(encoding="utf-8")
                    if json.loads(q)["business_id"] in kept
                }
                (root / name).write_text(
                    "".join(line for line in lines if json.loads(line)["id"] in ids),
                    encoding="utf-8",
                )
        (root / "perturbation.json").write_text(json.dumps(report, indent=1) + "\n")
        print(f"{variant}: {len(kept)} businesses -> {root}")


# --- Row-level variants: same facts, messier cell layout (coverage round 2) ---------------

ROW_VARIANTS = (
    "abbreviations",
    "footer_rows",
    "status_in_notes",
    "non_item_rows",
    "short_headers",
    "summary_rows",
    "padded_codes",
    "qty_with_unit",
    "repeated_headers",
    "mixed_dates",
    "unaccented_names",
)
_ROW = re.compile(r"<row\b([^>]*?)(?:/>|>(.*?)</row>)", re.S)


def _col_letters(index: int) -> str:
    letters = ""
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _col_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch) - 64
    return index


class Grid:
    """One CSV file or XLSX sheet as rows of cells; untouched XLSX cells keep their XML.

    A cell is ``(kind, value, xml)``: kind ``text``/``number``/``other``; ``xml`` is the
    original ``(attrs, body)`` of an unchanged XLSX cell, else None.
    """

    def __init__(self, rows: list[dict[int, tuple]], wrap=None):
        self.rows, self.wrap = rows, wrap

    def text(self, r: int, c: int) -> str | None:
        cell = self.rows[r].get(c)
        return None if cell is None or cell[1] in {None, ""} else str(cell[1])

    def kind(self, r: int, c: int) -> str | None:
        cell = self.rows[r].get(c)
        return cell[0] if cell else None

    def set(self, r: int, c: int, value: str, kind: str = "text") -> None:
        self.rows[r][c] = (kind, value, None)

    def header(self, names: set[str]) -> int | None:
        best = max(
            range(min(15, len(self.rows))),
            key=lambda r: sum(
                normalized(self.text(r, c) or "").strip() in names for c in self.rows[r]
            ),
            default=None,
        )
        if best is None or not any(
            normalized(self.text(best, c) or "").strip() in names for c in self.rows[best]
        ):
            return None
        return best

    @classmethod
    def from_csv(cls, data: bytes) -> Grid:
        bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
        text = data[len(bom) :].decode("utf-8")
        ending = "\r\n" if "\r\n" in text[:4096] else "\n"
        rows = [
            {j + 1: ("text", v, None) for j, v in enumerate(row)}
            for row in csv.reader(io.StringIO(text, newline=""))
        ]
        return cls(rows, ("csv", bom, ending))

    def to_csv(self) -> bytes:
        _, bom, ending = self.wrap
        out = io.StringIO()
        writer = csv.writer(out, lineterminator=ending)
        full = max((max(row, default=0) for row in self.rows), default=0)
        for row in self.rows:
            width = full if row else 0  # inserted rows keep the file's record width
            writer.writerow(
                ["" if row.get(j) is None else str(row[j][1] or "") for j in range(1, width + 1)]
            )
        return bom + out.getvalue().encode("utf-8")

    @classmethod
    def from_sheet(cls, xml: str, shared: list[str]) -> Grid | None:
        start, end = xml.find("<sheetData"), xml.find("</sheetData>")
        if start < 0 or end < 0 or "<mergeCell " in xml:
            return None  # merged layouts would need their ranges moved
        body_start = xml.index(">", start) + 1
        rows = []
        for m in _ROW.finditer(xml, body_start, end):
            cells = {}
            for c in _CELL.finditer(m[2] or ""):
                attrs, body = c[3], c[4]
                if 't="s"' in attrs and (v := re.search(r"<v>(\d+)</v>", body or "")):
                    cell = ("text", shared[int(v[1])])
                elif 't="inlineStr"' in attrs:
                    parts = re.findall(r"<t[^>]*>(.*?)</t>", body or "", re.S)
                    cell = ("text", html.unescape("".join(parts)))
                elif 't="' not in attrs or 't="n"' in attrs:
                    v = re.search(r"<v>([^<]*)</v>", body or "")
                    cell = ("number", v[1] if v and "<f" not in (body or "") else None)
                    if "<f" in (body or ""):
                        cell = ("other", v[1] if v else None)
                else:
                    cell = ("other", None)
                cells[_col_index(c[1])] = (*cell, (attrs, body))
            number = int(re.search(r'\br="(\d+)"', m[1])[1])
            while len(rows) < number - 1:
                rows.append({})
            rows.append(cells)
        return cls(rows, ("xlsx", xml[:body_start], xml[end:]))

    def to_sheet(self) -> str:
        _, head, tail = self.wrap
        head = re.sub(r"<dimension\b[^>]*/>", "", head)
        out = [head]
        for i, row in enumerate(self.rows, 1):
            if not row:
                continue
            out.append(f'<row r="{i}">')
            for j in sorted(row):
                kind, value, xml = row[j]
                ref = f"{_col_letters(j)}{i}"
                if xml is not None:
                    attrs, body = xml
                    out.append(
                        f'<c r="{ref}"{attrs}>{body}</c>'
                        if body is not None
                        else f'<c r="{ref}"{attrs}/>'
                    )
                elif kind == "number":
                    out.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    out.append(
                        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                        f"{html.escape(str(value))}</t></is></c>"
                    )
            out.append("</row>")
        return "".join(out) + tail


def _shared_strings(source: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in source.namelist():
        return []
    xml = source.read("xl/sharedStrings.xml").decode("utf-8")
    return [
        html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S)
    ]


def _sheet_names(source: zipfile.ZipFile) -> dict[str, str]:
    """worksheet part path -> sheet name."""
    workbook = source.read("xl/workbook.xml").decode("utf-8")
    rels = source.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    targets = {
        m[1]: m[2]
        for m in re.finditer(r'<Relationship\b[^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"', rels)
    }
    targets |= {
        m[2]: m[1]
        for m in re.finditer(r'<Relationship\b[^>]*?Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)
    }
    names = {}
    for m in re.finditer(r"<sheet\b([^>]*)/>", workbook):
        name = re.search(r'name="([^"]*)"', m[1])
        rid = re.search(r'r:id="([^"]*)"', m[1])
        if name and rid and rid[1] in targets:
            target = targets[rid[1]].lstrip("/")
            names[target if target.startswith("xl/") else "xl/" + target] = html.unescape(name[1])
    return names


def edit_file(path: Path, edit: Callable[[Grid, str], int]) -> tuple[bytes, int]:
    """Apply ``edit(grid, sheet_name)`` to a CSV or each XLSX sheet; returns (bytes, cells)."""
    data = path.read_bytes()
    if path.suffix.lower() == ".csv":
        grid = Grid.from_csv(data)
        count = edit(grid, path.stem)
        return (grid.to_csv(), count) if count else (data, 0)
    if path.suffix.lower() != ".xlsx":
        return data, 0
    source = zipfile.ZipFile(io.BytesIO(data))
    shared, names = _shared_strings(source), _sheet_names(source)
    out, changed = io.BytesIO(), 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            raw = source.read(info.filename)
            if info.filename in names:
                grid = Grid.from_sheet(raw.decode("utf-8"), shared)
                count = edit(grid, names[info.filename]) if grid else 0
                if count:
                    raw, changed = grid.to_sheet().encode("utf-8"), changed + count
            target.writestr(info, raw)
    return (out.getvalue(), changed) if changed else (data, 0)


def column_fields(readme: str) -> dict[str, dict[str, str]]:
    """README section file -> {normalized column name: field} for the known meanings."""
    meanings = {
        m: f for table in [MEANINGS, EXTRA_MEANINGS] for f, values in table.items() for m in values
    }
    meanings["don gia"] = "unit_price"
    sections: dict[str, dict[str, str]] = {}
    section = None
    for line in readme.splitlines():
        if line.startswith("#"):
            match = re.match(r"^## Cột trong `([^`]+)`", line)
            section = match[1] if match else None
            if section:
                sections.setdefault(section, {})
        elif section and (bullet := _BULLET.match(line)):
            field = meanings.get(plain(bullet[4]).strip().rstrip("."))
            sections[section][normalized(bullet[2]).strip()] = field or ""
    return sections


def _section_for(path: Path, sections: dict) -> dict[str, str]:
    if path.name in sections:
        return sections[path.name]
    stem = re.sub(r"_(?:\d+)$", "", path.stem)
    found = [s for s in sections if Path(s).stem == stem and Path(s).suffix == path.suffix]
    return sections[found[0]] if len(found) == 1 else {}


def _fields(grid: Grid, header: int, columns: dict[str, str]) -> dict[str, int]:
    found = {}
    for c in grid.rows[header]:
        field = columns.get(normalized(grid.text(header, c) or "").strip())
        if field and field not in found:
            found[field] = c
    return found


def abbreviation_of(name: str, used: dict[str, str]) -> str | None:
    """First word plus initials of the rest ("Cà phê sữa đá" -> "Cà PSĐ"); unique."""
    if name in used:
        return used[name]
    words = name.split()
    if len(words) < 3:
        return None
    short = words[0] + " " + "".join(w[0] for w in words[1:] if w[0].isalnum()).upper()
    taken = {plain(v) for v in used.values()} | {plain(k) for k in used}
    if plain(short) in taken or plain(short) == plain(name):
        return None
    used[name] = short
    return short


def _pairs(short_names: dict[str, str]):
    return sorted((short, full) for full, short in short_names.items())


def add_sheet(data: bytes, name: str, rows: list[list[str]]) -> bytes:
    """Append one inline-string worksheet to an XLSX workbook."""
    source = zipfile.ZipFile(io.BytesIO(data))
    parts = {i.filename: source.read(i.filename) for i in source.infolist()}
    number = 1 + max(
        int(m[1]) for f in parts if (m := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", f))
    )
    body = "".join(
        f'<row r="{i}">'
        + "".join(
            f'<c r="{_col_letters(j)}{i}" t="inlineStr"><is><t>{html.escape(v)}</t></is></c>'
            for j, v in enumerate(row, 1)
        )
        + "</row>"
        for i, row in enumerate(rows, 1)
    )
    parts[f"xl/worksheets/sheet{number}.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{body}</sheetData></worksheet>"
    ).encode()
    rid = f"rIdAbbr{number}"
    rels = parts["xl/_rels/workbook.xml.rels"].decode()
    parts["xl/_rels/workbook.xml.rels"] = rels.replace(
        "</Relationships>",
        f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/worksheet" Target="worksheets/sheet{number}.xml"/></Relationships>',
    ).encode()
    workbook = parts["xl/workbook.xml"].decode()
    sheet_id = 1 + max(int(x) for x in re.findall(r'sheetId="(\d+)"', workbook))
    parts["xl/workbook.xml"] = workbook.replace(
        "</sheets>",
        '<sheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        f' name="{html.escape(name)}" sheetId="{sheet_id}" r:id="{rid}"/></sheets>',
    ).encode()
    types = parts["[Content_Types].xml"].decode()
    parts["[Content_Types].xml"] = types.replace(
        "</Types>",
        f'<Override PartName="/xl/worksheets/sheet{number}.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    ).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for filename, raw in parts.items():
            target.writestr(filename, raw)
    return out.getvalue()


def _initials(name: str) -> str:
    words = re.findall(r"[^\W\d_]+", name)
    return "".join(w[0] for w in words).upper() + "x"


def short_header_names(names: list[str]) -> dict[str, str]:
    """Abbreviate column names so generic header words no longer score them."""
    out, used = {}, set()
    for name in names:
        short = _initials(name)
        while short.casefold() in used or HEADER_WORDS.search(normalized(short)):
            short += "x"
        used.add(short.casefold())
        out[name] = short
    return out


def _date_text(raw: str, kind: str) -> date | None:
    try:
        if kind == "number":
            if not re.fullmatch(r"\d{5}", raw):
                return None
            return parse_date(raw, date_system="1900")
        if re.search(r"\d:\d", raw):
            return None
        return parse_date(raw)
    except ParseError:
        return None


def _serial(day: date) -> int:
    return (day - date(1899, 12, 30)).days


def _year_hint(path: Path, sheet: str) -> int | None:
    for text in [sheet, path.stem]:
        found = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", text))
        if len(found) == 1:
            return int(next(iter(found)))
    return None


def _unaccented(text: str) -> str:
    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return unicodedata.normalize("NFC", "".join(c for c in text if not unicodedata.combining(c)))


# Status written as a note (public-rulebook clause "Sổ không có cột trạng thái"); completed
# orders get an empty or unrelated note. Everyday wording, not copied from test files.
NOTE_FORMS = {
    "completed": ["", "khách quen", "", "gói kỹ giùm", "bảo hành 12 tháng", "khách trả tiền mặt"],
    "cancelled": ["Khách huỷ", "cancel", "Huỷ đơn"],
    "returned": ["khách trả lại", "trả lại hàng", "Trả hàng", "khách mang trả", "Khách trả hàng"],
    "shipping": ["đang giao", "Đang giao hàng", "đang ship", "Chưa giao"],
}
NOTE_CLAUSE = (
    "| Sổ không có cột trạng thái | trạng thái đơn ghi ở cột Ghi chú. Ghi chú không nhắc trạng "
    "thái là đơn hoàn thành |"
)
# Invoice adjustment lines (§1: never revenue): item text and amount.
ADJUSTMENT_LINES = [("Phí ship", 15000), ("Voucher", -10000), ("Dùng 20 điểm", -20000)]
ORDER_FIELDS = {
    "order_id",
    "ordered_at",
    "status",
    "customer",
    "customer_id",
    "customer_group",
    "channel",
    "clock",
}


def row_variant(source: Path, target: Path, variant: str, protected: set[str]) -> dict:
    """Write one business with ``variant`` applied; ``protected``: plain names in Q/A text."""
    readme = (source / "README.md").read_text(encoding="utf-8-sig")
    sections = column_fields(readme)
    files = sorted(p for p in source.iterdir() if p.name != "README.md")
    written: dict[str, bytes] = {}
    changed = 0
    new_readme = readme
    sale_tables = {"order_id", "ordered_at"}
    renames: dict[str, str] = {}
    if variant == "summary_rows":
        # One customer, never named in a question or answer, gets a "Tống Thắng ..." name.
        names = Counter()
        for path in files:
            columns = _section_for(path, sections)

            def collect(grid: Grid, sheet: str, columns=columns) -> int:
                header = grid.header(set(columns))
                if header is None or "customer" not in _fields(grid, header, columns):
                    return 0
                c = _fields(grid, header, columns)["customer"]
                for r in range(header + 1, len(grid.rows)):
                    if (name := grid.text(r, c)) and not is_walk_in(name):
                        names[name] += 1
                return 0

            edit_file(path, collect)
        for name, _ in names.most_common():
            if plain(name) not in protected and len(plain(name)) > 3:
                renames[name] = "Tống Thắng " + name
                break
    bare_names: dict[str, set[str]] = {}
    if variant == "unaccented_names":
        # Only names whose unaccented spelling stays unique across every table change.
        for path in files:
            columns = _section_for(path, sections)

            def names_of(grid: Grid, sheet: str, columns=columns) -> int:
                header = grid.header(set(columns))
                if header is not None and "item" in (fields := _fields(grid, header, columns)):
                    for r in range(header + 1, len(grid.rows)):
                        if name := grid.text(r, fields["item"]):
                            bare_names.setdefault(token(_unaccented(name)), set()).add(name)
                return 0

            edit_file(path, names_of)
    short_names: dict[str, str] = {}  # full name -> abbreviation (unique, not a full name)
    seen: set[str] = set()
    shorts: dict[str, dict[str, str]] = {}
    statuses: dict[str, str] = {}
    for path in files:
        columns = _section_for(path, sections)
        if not columns:
            continue
        year = None

        def edit(grid: Grid, sheet: str, path=path, columns=columns) -> int:
            nonlocal year
            header = grid.header(set(columns))
            if header is None:
                return 0
            fields = _fields(grid, header, columns)
            count = 0
            data = range(header + 1, len(grid.rows))
            if variant == "abbreviations":
                # Code-less sale lines: every other line writes an abbreviation of the item.
                if (
                    path.suffix.lower() != ".xlsx"
                    or "item" not in fields
                    or "product_id" in fields
                    or "quantity" not in fields
                ):
                    return 0
                c = fields["item"]
                for r in data:
                    name = grid.text(r, c)
                    if (
                        name
                        and r % 2
                        and not summary_label(name)
                        and (short := abbreviation_of(name, short_names))
                    ):
                        grid.set(r, c, short)
                        count += 1
            elif variant == "status_in_notes":
                if "status" not in fields or "note" in fields:
                    return 0
                c = fields["status"]
                grid.set(header, c, "Ghi chú")
                for r in data:
                    raw = grid.text(r, c)
                    if raw is None:
                        continue
                    try:
                        status = normalize_status(raw)
                    except ParseError:
                        continue
                    forms = NOTE_FORMS[status]
                    grid.set(r, c, forms[r % len(forms)])
                    count += 1
                statuses[path.name] = next(k for k, f in columns.items() if f == "status")
            elif variant == "non_item_rows":
                if not {"order_id", "item", "amount"} <= fields.keys():
                    return 0
                out, orders = grid.rows[: header + 1], 0
                rows = list(data)
                for n, r in enumerate(rows):
                    out.append(grid.rows[r])
                    oid = grid.text(r, fields["order_id"])
                    following = (
                        grid.text(rows[n + 1], fields["order_id"]) if n + 1 < len(rows) else None
                    )
                    if not oid or following == oid:
                        continue
                    orders += 1
                    if orders % 7:
                        continue
                    text, amount = ADJUSTMENT_LINES[(orders // 7) % len(ADJUSTMENT_LINES)]
                    keep = {fields[f] for f in ORDER_FIELDS if f in fields}
                    line = {k: v for k, v in grid.rows[r].items() if k in keep}
                    line[fields["item"]] = ("text", text, None)
                    kind = grid.kind(r, fields["amount"])
                    line[fields["amount"]] = (
                        "number" if kind == "number" else "text",
                        str(amount),
                        None,
                    )
                    out.append(line)
                    count += 1
                grid.rows = out
            elif variant == "short_headers":
                names = [
                    grid.text(header, c) for c in sorted(grid.rows[header]) if grid.text(header, c)
                ]
                if len(names) >= 8 or {plain(n) for n in names} == set(stock_fields.values()):
                    return 0  # wide headers already score; the snapshot mapper names columns
                mapping = shorts.setdefault(path.name, short_header_names(names))
                for c in grid.rows[header]:
                    if (old := grid.text(header, c)) in mapping:
                        grid.set(header, c, mapping[old])
                        count += 1
            elif variant == "summary_rows":
                if "customer" in fields:
                    for r in data:
                        if (name := grid.text(r, fields["customer"])) in renames:
                            grid.set(r, fields["customer"], renames[name])
                            count += 1
                if sale_tables <= fields.keys():
                    # A "Tổng dd/mm" line in the order-ID column closes each day.
                    out, last_day, last_id = grid.rows[: header + 1], None, None
                    for r in data:
                        raw = grid.text(r, fields["ordered_at"])
                        day = (
                            _date_text(raw, grid.kind(r, fields["ordered_at"]) or "")
                            if raw
                            else None
                        )
                        if day is None and raw:
                            try:
                                day = parse_date(raw.split()[0])
                            except ParseError:
                                day = None
                        oid = grid.text(r, fields["order_id"])
                        if last_day and day and day != last_day and oid and oid != last_id:
                            out.append(
                                {fields["order_id"]: ("text", f"Tổng {last_day:%d/%m}", None)}
                            )
                            count += 1
                        out.append(grid.rows[r])
                        last_day, last_id = day or last_day, oid or last_id
                    grid.rows = out
            elif variant == "footer_rows":
                if sale_tables <= fields.keys():
                    # Hand-kept books end with a total and footnotes written in the date column.
                    c, total = fields["ordered_at"], 0
                    for r in data:
                        raw = grid.text(r, fields["amount"]) if "amount" in fields else None
                        if raw and re.fullmatch(r"-?\d+", raw):
                            total += int(raw)
                    footer = {c: ("text", "Tổng cộng", None)}
                    if "amount" in fields and total:
                        footer[fields["amount"]] = ("number", str(total), None)
                    grid.rows = [
                        *grid.rows,
                        footer,
                        {c: ("text", "Ghi chú: tiền tính bằng đồng", None)},
                        {c: ("text", "Ghi chú: số liệu ghi tay", None)},
                    ]
                    count += 3
            elif variant == "padded_codes":
                if {"product_id", "quantity"} <= fields.keys():
                    c = fields.get("product_id")
                    for r in data if c else []:
                        code = grid.text(r, c)
                        if code and grid.kind(r, c) == "text" and r % 2:
                            grid.set(r, c, ("  " + code) if r % 4 == 1 else (code + " "))
                            count += 1
            elif variant == "qty_with_unit":
                if {"quantity", "unit"} <= fields.keys():
                    q, u = fields["quantity"], fields["unit"]
                    for r in data:
                        qty, unit = grid.text(r, q), grid.text(r, u)
                        if qty and unit and re.fullmatch(r"\d+", qty) and r % 2:
                            grid.set(r, q, f"{qty} {unit.casefold() if r % 4 == 1 else unit}")
                            count += 1
            elif variant == "repeated_headers":
                if sale_tables <= fields.keys():
                    copy = dict(grid.rows[header])
                    out = grid.rows[: header + 1]
                    for n, r in enumerate(data, 1):
                        out.append(grid.rows[r])
                        if n % 50 == 0 and r + 1 < len(grid.rows):
                            out.append(dict(copy))
                            count += 1
                    grid.rows = out
            elif variant == "mixed_dates":
                if sale_tables <= fields.keys() and path.suffix.lower() == ".xlsx":
                    c = fields["ordered_at"]
                    hint = _year_hint(path, sheet)
                    for r in data:
                        raw, kind = grid.text(r, c), grid.kind(r, c)
                        day = _date_text(raw, kind) if raw else None
                        if day is None:
                            continue
                        form = r % 5
                        if form == 1:
                            grid.set(r, c, str(_serial(day)), "number")
                        elif form == 2:
                            grid.set(r, c, str(_serial(day)))
                        elif form == 3:
                            grid.set(r, c, f"{day.day} thg {day.month} {day.year}")
                        elif form == 4 and hint == day.year:
                            grid.set(r, c, f"{day.day}/{day.month}")
                        else:
                            grid.set(r, c, f"{day:%d/%m/%Y}")
                        count += 1
            elif variant == "unaccented_names":
                if "item" in fields and "quantity" in fields:
                    c = fields["item"]
                    for r in data:
                        name = grid.text(r, c)
                        if not name or name not in seen or r % 3:
                            seen.add(name or "")
                            continue
                        bare = _unaccented(name)
                        if bare != name and bare_names.get(token(bare)) == {name}:
                            grid.set(r, c, bare)
                            count += 1
            return count

        data, count = edit_file(path, edit)
        if count and variant == "abbreviations" and path.suffix.lower() == ".xlsx":
            data = add_sheet(
                data, "Ghi tắt", [["Ghi tắt", "Tên hàng"], *map(list, _pairs(short_names))]
            )
        if count:
            written[path.name] = data
            changed += count
    if variant == "status_in_notes" and statuses:
        for name, label in statuses.items():
            section = re.compile(r"^## Cột trong `" + re.escape(name) + "`")
            lines, inside = [], False
            for line in new_readme.splitlines():
                if line.startswith("#"):
                    inside = bool(section.match(line))
                match = _BULLET.match(line) if inside else None
                if match and normalized(match[2]).strip() == label:
                    line = match[1] + "Ghi chú" + match[3] + "ghi chú đơn"
                lines.append(line)
            new_readme = "\n".join(lines) + "\n"
    if variant == "short_headers" and shorts:
        for name, mapping in shorts.items():
            section = re.compile(r"^## Cột trong `" + re.escape(name) + "`")
            lines, inside = [], False
            for line in new_readme.splitlines():
                if line.startswith("#"):
                    inside = bool(section.match(line))
                match = _BULLET.match(line) if inside else None
                if match and match[2] in mapping:
                    line = match[1] + mapping[match[2]] + match[3] + match[4]
                lines.append(line)
            new_readme = "\n".join(lines) + "\n"
    if not changed:
        return {"skipped": f"no {variant} cells found"}
    target.mkdir(parents=True)
    for path in files:
        if path.name not in written:
            _link(path, target / path.name)
    for name, data in written.items():
        (target / name).write_bytes(data)
    (target / "README.md").write_text(new_readme, encoding="utf-8")
    return {"files": len(files), "changed_cells": changed} | (
        {"renamed_customer": next(iter(renames.values()))} if renames else {}
    )


if __name__ == "__main__":
    main()
