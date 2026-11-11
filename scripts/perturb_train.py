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
import zipfile
from collections.abc import Callable
from pathlib import Path

from src.data.normalization.entities import EXTRA_MEANINGS
from src.data.normalization.relational import MEANINGS
from src.data.normalization.values import STATUSES, ParseError, parse_date
from src.data.profiling import normalized
from src.semantics.rulebook import plain

VARIANTS = ("no_decl", "synonyms", "no_columns", "monthly_csv", "status_words", "vn_numbers")
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
        report = {
            b: perturb_business(data_dir / "data" / b, root / "data" / b, variant)
            for b in businesses
        }
        kept = {b for b, r in report.items() if "skipped" not in r}
        for name in ["rulebook.md", "calendar.csv"]:
            _link(data_dir / name, root / name)
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


if __name__ == "__main__":
    main()
