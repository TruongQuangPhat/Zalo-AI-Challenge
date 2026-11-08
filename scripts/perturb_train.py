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
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from src.data.normalization.entities import EXTRA_MEANINGS
from src.data.normalization.relational import MEANINGS
from src.data.normalization.values import ParseError, parse_date
from src.semantics.rulebook import plain

VARIANTS = ("no_decl", "synonyms", "no_columns", "monthly_csv")
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
    return {"readme_changed": new != readme, "files": len(files) + len(written)}


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
