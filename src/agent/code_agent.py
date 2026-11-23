"""Code fallback: when the rule planner abstains, the model writes pandas code over the raw
shop files (README + rulebook + table previews), several samples run in a sandboxed
subprocess, failed samples get one repair round from their traceback, and an answer is kept
only when enough samples agree (wrong answers cost -1).

Post-hoc addition after the 2026 private test (not part of the submitted image).
"""

from __future__ import annotations

import collections
import json
import math
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from src.contracts import validate_answer
from src.jsonio import ContractError

PROMPT_VERSION = "code-fallback-v4"
WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]
PREVIEW_CHAR_BUDGET = 18000
CELL_CHARS = 36
MAX_COLUMNS = 28
MAX_DISTINCT = 15

# Pure helpers shared by the sandbox (exec'd there) and the preview (exec'd here), so the
# model sees exactly the column types and values its code will get from table().
HELPERS = r'''
_THOUSANDS = re.compile(r"\d{1,3}(?:[.,]\d{3})+")
_VN_DATE = re.compile(r"^\s*(\d{1,2})\s*thg\s*(\d{1,2})\s*,?\s*(\d{4})", re.I)
_DATE_HINT = re.compile(r"\d{1,4}\s*[/\-.]\s*\d{1,2}|\d\s*thg\s*\d|\d{4}-\d{2}-\d{2}", re.I)

def _num(v):
    """Number from a cell; None when the cell is not a number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().lower().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("()+-")
    s = re.sub(r"(vnđ|vnd|đ|₫)$", "", s)
    mult = 1.0
    if s.endswith("k") and re.fullmatch(r"[\d.,]+k", s):
        mult, s = 1000.0, s[:-1]
    if not s or not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return None
    try:
        if _THOUSANDS.fullmatch(s):
            x = float(re.sub(r"[.,]", "", s))
        elif "," in s and "." in s:
            x = float(s.replace(".", "").replace(",", ".")) if s.rfind(",") > s.rfind(".") \
                else float(s.replace(",", ""))
        elif s.count(",") == 1:
            x = float(s.replace(",", "."))
        elif s.count(".") <= 1:
            x = float(s)
        else:
            return None
    except ValueError:
        return None
    x *= mult
    return -x if neg else x

_DMY = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")

def _day_first(values):
    """False when a column is written month first (some 'a/b/yyyy' has b > 12 and none a > 12)."""
    firsts, seconds = [], []
    for v in values:
        m = _DMY.match(v) if isinstance(v, str) else None
        if m:
            firsts.append(int(m.group(1)))
            seconds.append(int(m.group(2)))
    return not (seconds and max(seconds) > 12 and max(firsts) <= 12)

def _date(v, dayfirst=True):
    if isinstance(v, (pd.Timestamp, datetime)):
        return pd.Timestamp(v)
    if isinstance(v, date):
        return pd.Timestamp(v)
    if not isinstance(v, str) or not _DATE_HINT.search(v):
        return None
    m = _VN_DATE.match(v)
    s = f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else v.strip()
    try:
        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", s):
            return pd.Timestamp(s)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return pd.to_datetime(s, dayfirst=dayfirst)
    except (ValueError, TypeError, OverflowError):
        return None

def _clean(df):
    """Numeric-looking text columns become numbers, date-looking ones datetimes (>= 90%)."""
    out = df.copy()
    for c in out.columns:
        col = out[c]
        if col.dtype != object:
            continue
        vals = [v for v in col.tolist() if v is not None and not (isinstance(v, float) and math.isnan(v)) and str(v).strip() != ""]
        if not vals:
            continue
        nums = [_num(v) for v in vals]
        codes = sum(isinstance(v, str) and re.fullmatch(r"0\d{6,}", v.strip() or "x") is not None for v in vals)
        if sum(n is not None for n in nums) >= 0.9 * len(vals) and codes < 0.5 * len(vals):
            out[c] = pd.to_numeric(col.map(_num), errors="coerce")
            continue
        first = _day_first(vals)
        dates = [_date(v, first) for v in vals]
        if sum(d is not None for d in dates) >= 0.9 * len(vals):
            out[c] = pd.to_datetime(col.map(lambda v: _date(v, first)), errors="coerce")
    return out

def _header_row(df):
    best, best_n = 0, -1
    for i in range(min(15, len(df))):
        n = sum(isinstance(v, str) and v.strip() != "" for v in df.iloc[i].tolist())
        if n > best_n:
            best, best_n = i, n
    return best

def _with_header(df, h):
    out = df.iloc[h + 1:].copy()
    cols, seen = [], {}
    for j, v in enumerate(df.iloc[h].tolist()):
        c = str(v).strip() if v is not None and not (isinstance(v, float) and math.isnan(v)) else f"col{j}"
        if hasattr(v, "isoformat"):
            c = str(v)[:10]
        if c in seen:
            seen[c] += 1
            c = f"{c}.{seen[c]}"
        else:
            seen[c] = 0
        cols.append(c)
    out.columns = cols
    return out.dropna(how="all").reset_index(drop=True)

def _is_sql(name):
    return ".sqlite::" in name.lower()

def _resolve(name, store):
    if name in store:
        return [name]
    low = str(name).lower().strip()
    keys = [k for k in store if k.lower() == low or k.split("::")[0].lower() == low
            or k.split("::")[-1].lower() == low]
    if not keys:
        keys = [k for k in store if low and low in k.lower()]
    if not keys:
        raise KeyError(f"Không có bảng {name!r}. Các bảng: {list(store)}")
    return keys

def _table(store, name, header_row=None, clean=True, cache=None):
    parts = []
    keys = _resolve(name, store)
    for k in keys:
        df = store[k]
        if cache is not None and header_row is None and clean and k in cache:
            df = cache[k].copy()  # cleaned once per shop, outside the sandbox
        elif not _is_sql(k):
            df = _with_header(df, _header_row(df) if header_row is None else header_row)
        else:
            df = df.copy()
        if clean and not (cache is not None and header_row is None and k in cache):
            df = _clean(df)
        if len(keys) > 1:
            df["sheet"] = k.split("::")[-1]
        parts.append(df)
    return parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)
'''

# Runs inside the sandbox (cwd = the shop's folder). argv[1] = pickled tables, argv[2] = today.
PRELUDE = (
    r"""
import sys, json, math, re, pickle, warnings, datetime as dt
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np
pd.set_option("future.no_silent_downcasting", True)
"""
    + HELPERS
    + r'''
_STORE = pickle.load(open(sys.argv[1], "rb"))
if "__raw__" in _STORE:
    _TABLES, _CLEAN, CALENDAR = _STORE["__raw__"], _STORE["__clean__"], _STORE.get("__calendar__")
else:
    _TABLES, _CLEAN = _STORE, None
    CALENDAR = _TABLES.pop("__calendar__", None)
TODAY = date.fromisoformat(sys.argv[2])

import unicodedata as _ud

def _key(name):
    text = _ud.normalize("NFD", str(name)).replace("đ", "d").replace("Đ", "D")
    text = "".join(ch for ch in text if _ud.category(ch) != "Mn").casefold()
    return re.sub(r"[^0-9a-z]+", "", text)

class Frame(pd.DataFrame):
    """DataFrame whose df['col'] also finds a column written without accents, case or spaces,
    and whose KeyError lists the real columns."""

    @property
    def _constructor(self):
        return Frame

    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            if isinstance(key, str) or (isinstance(key, list) and all(isinstance(k, str) for k in key)):
                by_key = {}
                for c in self.columns:
                    by_key.setdefault(_key(c), []).append(c)
                wanted = [key] if isinstance(key, str) else key
                found = []
                for k in wanted:
                    hits = [k] if k in self.columns else by_key.get(_key(k), [])
                    if len(hits) != 1:
                        raise KeyError(f"{k!r}; các cột: {list(self.columns)}") from None
                    found.append(hits[0])
                return super().__getitem__(found[0] if isinstance(key, str) else found)
            raise

def tables(name=None):
    """Names of every table ('file' or 'file::sheet'); with a name, the matching ones."""
    if name is None:
        return list(_TABLES)
    return _resolve(name, _TABLES)

def raw(name):
    """Table exactly as stored, header=None (all rows, including titles/footers)."""
    keys = _resolve(name, _TABLES)
    return _TABLES[keys[0]].copy()

def table(name, header_row=None, clean=True):
    """Table with its header row as column names; numbers/dates converted; a file name
    without '::sheet' joins all its sheets (column 'sheet')."""
    return Frame(_table(_TABLES, name, header_row, clean, _CLEAN))

YESTERDAY = TODAY - timedelta(days=1)

def round_half_up(x, digits=0):
    """Round half away from zero (2.5 -> 3, 2.45 -> 2.5 with digits=1)."""
    from decimal import Decimal, ROUND_HALF_UP
    q = Decimal(1).scaleb(-digits)
    out = float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))
    return int(out) if digits == 0 else out

def week_range(day, start="mon"):
    """(first, last) date of the week holding `day`; start='mon' or 'sun' as the rulebook says."""
    day = pd.Timestamp(day).date()
    offset = day.weekday() if start == "mon" else (day.weekday() + 1) % 7
    first = day - timedelta(days=offset)
    return first, first + timedelta(days=6)

def month_range(year, month):
    """(first, last) date of a calendar month."""
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return first, last

def last_n_days(n, end):
    """(first, last) of n consecutive days ending on `end` (TODAY or YESTERDAY per rulebook)."""
    end = pd.Timestamp(end).date()
    return end - timedelta(days=n - 1), end

def solar_to_lunar(day):
    """(lunar_day, lunar_month, lunar_year, leap) of a solar date, from calendar.csv."""
    row = CALENDAR[CALENDAR["date"] == pd.Timestamp(day).strftime("%Y-%m-%d")].iloc[0]
    return int(row["lunar_day"]), int(row["lunar_month"]), int(row["lunar_year"]), str(row["lunar_leap"]).lower() == "true"

def lunar_to_solar(lunar_day, lunar_month, lunar_year, leap=False):
    """Solar date of a lunar date, from calendar.csv."""
    c = CALENDAR
    hit = c[(c["lunar_day"] == lunar_day) & (c["lunar_month"] == lunar_month)
            & (c["lunar_year"] == lunar_year)
            & (c["lunar_leap"].astype(str).str.lower() == str(bool(leap)).lower())]
    return date.fromisoformat(str(hit.iloc[0]["date"])[:10])

def num(v):
    """Parse one number written as text ('1.250.000', '560,000', '12k'); NaN if not a number."""
    x = _num(v)
    return float("nan") if x is None else x

def to_date(v):
    """Parse one date ('1 thg 6 2025', '25/01/2025', Timestamp); NaT if not a date."""
    d = _date(v)
    return pd.NaT if d is None else d
'''
)

EPILOGUE = r"""
def _plain(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/inf answer")
        return int(v) if v.is_integer() and abs(v) < 2**53 else v
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (pd.Series, pd.Index, np.ndarray)):
        return [_plain(x) for x in list(v)]
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v
print("\n__ANSWER__" + json.dumps(_plain(ANSWER), ensure_ascii=False))
"""


def _helpers() -> dict:
    import datetime as dt_module
    from datetime import date as date_cls
    from datetime import datetime

    import numpy as np
    import pandas as pd

    import warnings

    namespace = {
        "re": re,
        "math": math,
        "warnings": warnings,
        "pd": pd,
        "np": np,
        "date": date_cls,
        "datetime": datetime,
        "dt": dt_module,
    }
    exec(HELPERS, namespace)  # noqa: S102 - our own constant helper source
    return namespace


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value)
    if hasattr(value, "isoformat") and text.endswith(" 00:00:00"):
        text = text[:-9]
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        text = str(int(value))
    text = text.replace("\n", " ").replace("|", "/")
    return text if len(text) <= CELL_CHARS else text[: CELL_CHARS - 1] + "…"


def load_tables(shop_dir: Path) -> dict:
    """Every table of a shop as raw DataFrames (xlsx/csv header=None; sqlite with header)."""
    import sqlite3

    import pandas as pd

    tables: dict[str, Any] = {}
    for path in sorted(p for p in shop_dir.rglob("*") if p.is_file()):
        name = str(path.relative_to(shop_dir))
        suffix = path.suffix.lower()
        try:
            if suffix == ".xlsx":
                sheets = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl")
                for sheet, frame in sheets.items():
                    frame = frame.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
                    frame.columns = range(frame.shape[1])
                    tables[f"{name}::{sheet}"] = frame
            elif suffix == ".csv":
                frame = pd.read_csv(
                    path, header=None, sep=None, engine="python", encoding="utf-8-sig", dtype=str
                )
                tables[name] = frame.dropna(how="all").reset_index(drop=True)
            elif suffix == ".sqlite":
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                    names = [
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                        )
                    ]
                    for table in names:
                        tables[f"{name}::{table}"] = pd.read_sql_query(
                            f'SELECT * FROM "{table}"', conn
                        )
        except Exception as exc:  # one unreadable file must not hide the others
            tables[f"{name}::__error__"] = pd.DataFrame({"error": [f"{type(exc).__name__}: {exc}"]})
    return tables


def _kind(series) -> str:
    import pandas as pd

    if pd.api.types.is_datetime64_any_dtype(series):
        return "ngày"
    if pd.api.types.is_numeric_dtype(series):
        return "số"
    return "chữ"


def clean_tables(tables: dict) -> dict:
    """What table(name) returns for every single table, computed once per shop."""
    helpers = _helpers()
    cleaned: dict[str, Any] = {}
    for name in tables:
        if name.startswith("__"):
            continue
        try:
            cleaned[name] = helpers["_table"](tables, name)
        except Exception:  # noqa: BLE001 - such a table is cleaned in the sandbox instead
            continue
    return cleaned


def preview(
    tables: dict, sample_rows: int = 3, distinct: bool = True, cleaned: dict | None = None
) -> str:
    """Compact view of what table() returns: columns with their kind, a few rows, the last
    row, and the values of short text columns (status, channel, group...). Tables with the
    same columns are listed once."""
    helpers = _helpers()
    cleaned = clean_tables(tables) if cleaned is None else cleaned
    groups: dict[tuple, list[str]] = {}
    for name, frame in cleaned.items():
        groups.setdefault(tuple(str(c) for c in frame.columns), []).append(name)
    parts = []
    for header, names in groups.items():
        name = names[0]
        frame = cleaned[name]
        rows_total = sum(len(cleaned[n]) for n in names)
        lines = [
            f"### {name}  (dòng={len(frame)}"
            + (f", cả nhóm={rows_total}" if len(names) > 1 else "")
            + ")"
        ]
        raw = tables[name]
        if ".sqlite::" not in name.lower() and len(raw):
            h = helpers["_header_row"](raw)
            for i in range(h):
                title = " | ".join(c for c in map(_cell, raw.iloc[i]) if c)
                if title:
                    lines.append("tiêu đề: " + title)
        cols = list(frame.columns)[:MAX_COLUMNS]
        lines.append("cột: " + " | ".join(f"{c} [{_kind(frame[c])}]" for c in cols))
        picks = list(range(min(sample_rows, len(frame))))
        if len(frame) > sample_rows:
            picks.append(len(frame) - 1)
        for i in picks:
            tag = "cuối: " if i == len(frame) - 1 and i >= sample_rows else "dòng: "
            lines.append(tag + " | ".join(_cell(v) for v in frame.iloc[i].tolist()[:MAX_COLUMNS]))
        if distinct:
            for c in cols:
                if _kind(frame[c]) != "chữ":
                    continue
                values = frame[c].dropna().astype(str).str.strip()
                values = values[values != ""].unique()
                if 1 < len(values) <= MAX_DISTINCT:
                    lines.append(f"giá trị của {c}: " + ", ".join(_cell(v) for v in values))
        if len(names) > 1:
            lines.append(f"cùng cột ({len(names) - 1} bảng nữa): " + ", ".join(names[1:]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


INSTRUCTIONS = """Bạn là chuyên viên phân tích dữ liệu cho một cửa hàng nhỏ ở Việt Nam. Trả lời câu hỏi
bằng cách viết MỘT khối code Python (```python ... ```) tính đáp án từ các bảng dữ liệu của shop.

Quy tắc:
- Làm đúng theo RULEBOOK và README bên dưới (câu hỏi thắng README, README thắng rulebook nếu rulebook ghi vậy).
- "Hôm nay" là TODAY (biến có sẵn, kiểu date). Bản ghi sau hôm nay coi như chưa xảy ra, trừ khi rulebook nói khác.
- Chỉ dùng dữ liệu trong bảng; không bịa. Nếu dữ liệu không đủ để trả lời: ANSWER = {"type": "unanswerable"}.
- Code phải gán biến ANSWER = {"type": <type>, "answer": <giá trị>} theo đúng format type trong rulebook
  (number, percent, string, date, boolean, list, table). number/percent là số Python, không phải chuỗi.
  Tiền là số nguyên VND. percent ghi số phần trăm (12.4). Tên lấy nguyên văn từ dữ liệu.
- Dùng ĐÚNG tên bảng và tên cột như phần xem trước; so khớp giá trị chữ (trạng thái, kênh, nhóm…)
  theo danh sách "giá trị của <cột>" đã liệt kê.

API có sẵn (đã import pandas as pd, numpy as np, datetime, re, math):
- table(name): DataFrame của bảng, tên cột lấy từ dòng tiêu đề, cột số đã là số, cột ngày đã là datetime
  (đúng như phần xem trước). name là 'file::sheet'; chỉ ghi 'file' thì ghép mọi sheet (thêm cột 'sheet').
- raw(name): bảng nguyên trạng, header=None (giữ dòng tiêu đề, dòng tổng).
- num(x): đổi một ô chữ thành số ('1.250.000', '560,000', '12k'); to_date(x): đổi một ô thành ngày.
- tables(): danh sách tên bảng. CALENDAR: DataFrame calendar.csv (có thể None). TODAY, YESTERDAY: date.
- round_half_up(x, digits=0): làm tròn nửa lên. week_range(day, start='mon'|'sun'),
  month_range(y, m), last_n_days(n, end): trả (ngày đầu, ngày cuối), gồm cả hai đầu.
- solar_to_lunar(day) -> (ngày, tháng, năm âm, nhuận); lunar_to_solar(ngày, tháng, năm âm, nhuận=False).
Ô trống ở các dòng sau của cùng một nhóm dòng (cùng đơn) có thể cần ffill.

Trước khi viết code, tự xác định theo RULEBOOK (ghi ngắn ở comment đầu code):
1. Shop thuộc ngành nào trong mục "Theo ngành" (nếu có) và luật ngành nào áp cho câu hỏi.
2. Kỳ thời gian chính xác: tuần bắt đầu thứ mấy, kỳ tới hôm nay dừng ở hết hôm nay hay hết hôm qua,
   "tính đến ngày X" là hết ngày X hay hết ngày trước X, tháng/quý không ghi năm.
3. Bản ghi nào được tính (trạng thái đơn, dòng tổng, dòng trùng) và cách làm tròn đáp án.
Chỉ trả về khối code (comment ngắn ở đầu là được), không giải thích ngoài code."""


def build_messages(
    question: str, today: str, readme: str, rulebook: str, table_view: str, calendar_head: str
) -> list[dict]:
    weekday = WEEKDAYS[date.fromisoformat(today).weekday()]
    user = (
        f"# RULEBOOK\n{rulebook.strip()}\n\n# README của shop\n{readme.strip() or '(trống)'}\n\n"
        f"# CÁC BẢNG (xem trước, đúng như table() trả về)\n{table_view}\n\n"
        f"# CALENDAR (vài dòng đầu)\n{calendar_head}\n\n"
        f"# CÂU HỎI\nHôm nay (TODAY) = {today} ({weekday}).\n{question}"
    )
    return [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": user}]


def extract_code(text: str) -> str | None:
    if "<think>" in text:
        if "</think>" not in text:
            return None  # thinking ran out of tokens before any code
        text = text.rsplit("</think>", 1)[1]
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S)
    if blocks:
        return max(blocks, key=len)
    if "ANSWER" in text:
        return text
    return None


def run_code(
    code: str, tables_path: Path, today: str, timeout: float = 40, cwd: Path | None = None
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.py"
        script.write_text(PRELUDE + "\n" + code + "\n" + EPILOGUE, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tmp,
            "PYTHONNOUSERSITE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script), str(tables_path.resolve()), today],
                cwd=str(cwd) if cwd else tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
    marker = proc.stdout.rfind("__ANSWER__")
    if proc.returncode != 0 or marker < 0:
        # The temporary folder differs per run; without it equal failures read the same.
        return {"error": (proc.stderr or proc.stdout).replace(str(script), "run.py")[-600:]}
    try:
        return {"answer": json.loads(proc.stdout[marker + len("__ANSWER__") :].strip())}
    except ValueError as exc:
        return {"error": f"bad answer json: {exc}"}


def _coerce(candidate: dict) -> dict:
    """Numeric answers written as text ('12.5', '1.250.000') become numbers."""
    kind, value = candidate.get("type"), candidate.get("answer")
    if kind in {"number", "percent"} and isinstance(value, str):
        parsed = _helpers()["_num"](value.replace("%", ""))
        if parsed is not None:
            value = int(parsed) if parsed.is_integer() else parsed
    if kind == "boolean" and isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        value = value.strip().lower() == "true"
    if kind == "date" and isinstance(value, str) and len(value) > 10 and value[:10].count("-") == 2:
        value = value[:10]
    if kind == "table":
        value = _as_table(value)
    return {**candidate, "answer": value}


def _as_table(value: Any) -> Any:
    """{'columns', 'rows'} from the shapes code usually builds (records, column dict)."""
    if isinstance(value, dict) and "columns" in value and "rows" in value:
        return {"columns": value["columns"], "rows": value["rows"]}
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        columns = list(value[0])
        if all(list(r) == columns for r in value):
            return {"columns": columns, "rows": [[r[c] for c in columns] for r in value]}
    if isinstance(value, dict) and value and all(isinstance(v, list) for v in value.values()):
        lengths = {len(v) for v in value.values()}
        if len(lengths) == 1:
            columns = list(value)
            return {"columns": columns, "rows": [list(r) for r in zip(*value.values())]}
    return value


def _norm(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        try:
            return round(float(value), 2)
        except OverflowError:  # an absurdly large int still votes, as its digits
            return str(value)
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().casefold()
    if isinstance(value, list):
        return tuple(_norm(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _norm(v)) for k, v in value.items()))
    return str(value)


def vote_key(answer: dict) -> str:
    return json.dumps(
        [answer.get("type"), _norm(answer.get("answer"))], ensure_ascii=False, default=str
    )


def suspicious_zero(record: dict) -> bool:
    """A numeric 0 from generated code is almost always a filter that matched nothing: in the
    train labels 4 of 2,462 number/percent answers are 0, and the rulebook says missing data
    is unanswerable, not 0."""
    return record.get("type") in {"number", "percent"} and record.get("answer") == 0


def _short_error(error: str) -> str:
    lines = [line for line in error.strip().splitlines() if line.strip()]
    return "\n".join(lines[-8:])


class CodeAgent:
    name = "code-fallback"

    def __init__(
        self,
        model,
        root: Path,
        cache_dir: Path,
        *,
        samples: int = 4,
        min_votes: int = 3,
        temperature: float = 0.6,
        max_tokens: int = 1400,
        timeout: float = 3600,
        answer_unanswerable: bool = False,
        repairs: int = 2,
        thinking: bool = False,
        workers: int = 8,
    ):
        self.model, self.root, self.cache_dir = model, root, cache_dir
        self.samples, self.min_votes, self.temperature = samples, min_votes, temperature
        self.max_tokens, self.timeout = max_tokens, timeout
        self.answer_unanswerable, self.repairs = answer_unanswerable, repairs
        self.thinking, self.workers = thinking, workers
        self._shops: dict[str, tuple[Path, str]] = {}
        self._rulebook = (root / "rulebook.md").read_text(encoding="utf-8-sig")
        calendar = root / "calendar.csv"
        self._calendar_head = (
            "\n".join(calendar.read_text(encoding="utf-8-sig").splitlines()[:3])
            if calendar.exists()
            else "(không có)"
        )

    @property
    def metadata(self) -> dict:
        return {
            "prompt_version": PROMPT_VERSION,
            "samples": self.samples,
            "min_votes": self.min_votes,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "answer_unanswerable": self.answer_unanswerable,
            "repairs": self.repairs,
            "thinking": self.thinking,
        }

    def _shop(self, business: str) -> tuple[Path, str]:
        if business not in self._shops:
            import pandas as pd

            shop_dir = self.root / "data" / business
            tables = load_tables(shop_dir)
            cleaned = clean_tables(tables)
            view = preview(tables, cleaned=cleaned)
            if len(view) > PREVIEW_CHAR_BUDGET:
                view = preview(tables, sample_rows=2, cleaned=cleaned)
            if len(view) > PREVIEW_CHAR_BUDGET:
                view = preview(tables, sample_rows=1, distinct=False, cleaned=cleaned)
            if len(view) > PREVIEW_CHAR_BUDGET:
                view = view[:PREVIEW_CHAR_BUDGET] + "\n…(cắt bớt)"
            calendar = self.root / "calendar.csv"
            store = {
                "__raw__": tables,
                "__clean__": cleaned,
                "__calendar__": pd.read_csv(calendar, encoding="utf-8-sig")
                if calendar.exists()
                else None,
            }
            path = self.cache_dir / "code_agent" / f"{business}.pkl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                pickle.dump(store, stream)
            self._shops[business] = (path, view)
        return self._shops[business]

    def _messages(self, question) -> list[dict]:
        _, view = self._shop(question.business_id)
        readme_path = self.root / "data" / question.business_id / "README.md"
        readme = readme_path.read_text(encoding="utf-8-sig") if readme_path.exists() else ""
        return build_messages(
            question.question,
            question.as_of_date,
            readme,
            self._rulebook,
            view,
            self._calendar_head,
        )

    def _generate(self, conversations, *, n, temperature, thinking) -> list[dict]:
        if hasattr(self.model, "sample_batch"):
            return self.model.sample_batch(
                conversations,
                self.timeout,
                n=n,
                temperature=temperature,
                max_tokens=self.max_tokens,
                thinking=thinking,
            )
        return [
            self.model.sample(
                c,
                self.timeout,
                n=n,
                temperature=temperature,
                max_tokens=self.max_tokens,
                thinking=thinking,
            )
            for c in conversations
        ]

    def _run_all(self, jobs: list[tuple[Any, str | None]]) -> list[dict]:
        """Execute (question, code) pairs in parallel; each run checked against the contract."""

        def one(job):
            question, code = job
            if code is None:
                return {"error": "no code"}
            tables_path, _ = self._shop(question.business_id)
            run = dict(
                run_code(
                    code,
                    tables_path,
                    question.as_of_date,
                    cwd=self.root / "data" / question.business_id,
                )
            )
            run["code"] = code[-1500:]
            if "answer" in run:
                candidate = run["answer"]
                if not isinstance(candidate, dict) or "type" not in candidate:
                    return {"error": "ANSWER is not {type, answer}", "code": run["code"]}
                candidate = _coerce(candidate)
                record = {"id": question.id, "type": candidate["type"]}
                if candidate["type"] not in {"unanswerable", "abstain"}:
                    record["answer"] = candidate.get("answer")
                try:
                    validate_answer(record)
                    run["record"] = record
                except ContractError as exc:
                    run["error"] = f"format: {exc}"
            return run

        unique = list(dict.fromkeys((q.id, code) for q, code in jobs))
        by_id = {q.id: q for q, _ in jobs}
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            done = dict(zip(unique, pool.map(lambda k: one((by_id[k[0]], k[1])), unique)))
        return [dict(done[(q.id, code)]) for q, code in jobs]

    def _decide(self, question, runs: list[dict], log: dict) -> dict:
        votes = collections.Counter(vote_key(r["record"]) for r in runs if "record" in r)
        log["runs"] = runs
        log["votes"] = dict(votes)
        answer = {"id": question.id, "type": "abstain"}
        if votes:
            key, count = votes.most_common(1)[0]
            chosen = next(
                r["record"] for r in runs if "record" in r and vote_key(r["record"]) == key
            )
            log["top_votes"] = count
            if (
                count >= self.min_votes
                and (chosen["type"] != "unanswerable" or self.answer_unanswerable)
                and not suspicious_zero(chosen)
            ):
                answer = chosen
        log["chosen"] = answer["type"]
        return {"answer": answer, "log": log}

    def answer_many(self, questions: list) -> dict[str, dict]:
        """Answer a chunk of questions together: one batched generation, parallel runs, one
        batched repair round, then a vote per question."""
        started = time.monotonic()
        messages = {q.id: self._messages(q) for q in questions}
        first = self._generate(
            [messages[q.id] for q in questions],
            n=self.samples,
            temperature=self.temperature,
            thinking=self.thinking,
        )
        jobs = [(q, extract_code(t)) for q, out in zip(questions, first) for t in out["texts"]]
        flat = self._run_all(jobs)
        runs = {q.id: [] for q in questions}
        for (q, _), run in zip(jobs, flat):
            runs[q.id].append(run)
        logs = {
            q.id: {
                "prompt_version": PROMPT_VERSION,
                "usage": [out.get("usage")],
                "finish_reasons": out.get("finish_reasons"),
            }
            for q, out in zip(questions, first)
        }
        # Repair round: questions still short of the vote get their distinct failures back
        # with the traceback (no thinking, to keep it short); repaired runs vote too.
        asks = []
        for q in questions:
            got = collections.Counter(vote_key(r["record"]) for r in runs[q.id] if "record" in r)
            if got and got.most_common(1)[0][1] >= self.min_votes:
                continue
            seen: set[str] = set()
            for run in runs[q.id]:
                if "error" not in run or not run.get("code") or len(seen) >= self.repairs:
                    continue
                error = _short_error(run["error"])
                if error in seen:
                    continue
                seen.add(error)
                asks.append(
                    (
                        q,
                        messages[q.id]
                        + [
                            {"role": "assistant", "content": f"```python\n{run['code']}\n```"},
                            {
                                "role": "user",
                                "content": "Code trên bị lỗi:\n"
                                + error
                                + "\nSửa lại và trả về TOÀN BỘ code đã sửa trong một khối ```python```.",
                            },
                        ],
                    )
                )
        if asks:
            fixed_out = self._generate([m for _, m in asks], n=2, temperature=0.3, thinking=False)
            fixed_jobs = [
                (q, extract_code(t)) for (q, _), out in zip(asks, fixed_out) for t in out["texts"]
            ]
            for (q, _), run in zip(fixed_jobs, self._run_all(fixed_jobs)):
                run["repair"] = True
                runs[q.id].append(run)
            for (q, _), out in zip(asks, fixed_out):
                logs[q.id]["usage"].append(out.get("usage"))
        seconds = (time.monotonic() - started) / max(1, len(questions))
        results = {}
        for q in questions:
            logs[q.id]["seconds"] = seconds  # chunk time shared out
            results[q.id] = self._decide(q, runs[q.id], logs[q.id])
        return results

    def answer(self, question) -> dict:
        return self.answer_many([question])[question.id]


class FallbackPlanner:
    """Rule planner first; the code agent only answers what the planner abstains on.
    With skip_base, questions are answered in chunks (plan_ahead gives the order) so the
    model generates for many questions at once."""

    accepts_missing_context = True

    def __init__(self, base, agent: CodeAgent, *, skip_base: bool = False, chunk: int = 24):
        self.base, self.agent, self.skip_base, self.chunk = base, agent, skip_base, chunk
        self.name = f"{base.name}+{agent.name}" + ("-only" if skip_base else "")
        self._order: list = []
        self._ready: dict[str, dict] = {}

    @property
    def metadata(self) -> dict:
        return {
            **self.base.metadata,
            "fallback": self.agent.metadata,
            "skip_base": self.skip_base,
            "chunk": self.chunk,
        }

    def plan_ahead(self, questions) -> None:
        self._order = list(questions)

    def _fallback(self, question) -> dict:
        try:
            if self.skip_base and question.id not in self._ready:
                ids = [q.id for q in self._order]
                start = ids.index(question.id) if question.id in ids else None
                chunk = self._order[start : start + self.chunk] if start is not None else [question]
                chunk = [q for q in chunk if q.id not in self._ready] or [question]
                self._ready.update(self.agent.answer_many(chunk))
            if question.id in self._ready:
                return self._ready.pop(question.id)
            return self.agent.answer(question)
        except Exception as exc:  # the fallback never turns an abstain into a question error
            return {
                "answer": {"id": question.id, "type": "abstain"},
                "log": {"error": f"{type(exc).__name__}: {exc}"},
            }

    def decide(self, question, *, context) -> dict:
        decision = None
        if context is not None and not self.skip_base:
            decision = self.base.decide(question, context=context)
            if decision["answer"]["type"] != "abstain":
                return decision
        fallback = self._fallback(question)
        plan = dict(decision["plan"]) if decision else {"status": "no_context"}
        plan["fallback"] = fallback["log"]
        return {
            **(decision or {}),
            "answer": fallback["answer"],
            "plan": plan,
            "execution": "code_fallback"
            if fallback["answer"]["type"] != "abstain"
            else (decision or {}).get("execution", "not_performed"),
        }
