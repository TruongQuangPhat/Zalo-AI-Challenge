"""Code fallback: when the rule planner abstains, the model writes pandas code over the raw
shop files (README + rulebook + table previews), several samples run in a sandboxed
subprocess, and an answer is kept only when enough samples agree (wrong answers cost -1).

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
from datetime import date
from pathlib import Path
from typing import Any

from src.contracts import validate_answer
from src.jsonio import ContractError

PROMPT_VERSION = "code-fallback-v1"
WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]
PREVIEW_CHAR_BUDGET = 16000
CELL_CHARS = 36
MAX_COLUMNS = 28

# Runs inside the sandbox before the model's code. argv[1] = pickled tables, argv[2] = today.
PRELUDE = r'''
import sys, json, math, re, pickle, datetime as dt
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np
pd.set_option("future.no_silent_downcasting", True)
_TABLES = pickle.load(open(sys.argv[1], "rb"))
TODAY = date.fromisoformat(sys.argv[2])
CALENDAR = _TABLES.pop("__calendar__", None)

def _header_row(df):
    best, best_n = 0, -1
    for i in range(min(15, len(df))):
        n = sum(isinstance(v, str) and v.strip() != "" for v in df.iloc[i].tolist())
        if n > best_n:
            best, best_n = i, n
    return best

def tables():
    """Names of every table: 'file' or 'file::sheet'."""
    return list(_TABLES)

def raw(name):
    """Table exactly as stored, header=None (all rows, including titles/footers)."""
    return _TABLES[name].copy()

def table(name, header_row=None):
    """Table with row `header_row` (auto-detected if None) as column names."""
    df = _TABLES[name]
    if name.endswith(".sqlite") or "::" in name and name.split("::")[0].endswith(".sqlite"):
        return df.copy()
    h = _header_row(df) if header_row is None else header_row
    out = df.iloc[h + 1:].copy()
    cols, seen = [], {}
    for j, v in enumerate(df.iloc[h].tolist()):
        c = str(v).strip() if v is not None and not (isinstance(v, float) and math.isnan(v)) else f"col{j}"
        if c in seen:
            seen[c] += 1
            c = f"{c}.{seen[c]}"
        else:
            seen[c] = 0
        cols.append(c)
    out.columns = cols
    return out.dropna(how="all").reset_index(drop=True)
'''

EPILOGUE = r'''
def _plain(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/inf answer")
        return int(v) if v.is_integer() else v
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v
print("\n__ANSWER__" + json.dumps(_plain(ANSWER), ensure_ascii=False))
'''


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value)
    if hasattr(value, "isoformat") and text.endswith(" 00:00:00"):
        text = text[:-9]
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


def _header_index(frame) -> int:
    best, best_n = 0, -1
    for i in range(min(15, len(frame))):
        n = sum(isinstance(v, str) and v.strip() != "" for v in frame.iloc[i].tolist())
        if n > best_n:
            best, best_n = i, n
    return best


def preview(tables: dict, sample_rows: int = 3) -> str:
    """Compact text view; tables with the same header are listed once."""
    groups: dict[tuple, list[str]] = {}
    for name, frame in tables.items():
        if name.startswith("__"):
            continue
        sqlite = ".sqlite::" in name
        if sqlite:
            header = tuple(str(c) for c in frame.columns)
        else:
            h = _header_index(frame) if len(frame) else 0
            header = tuple(_cell(v) for v in frame.iloc[h].tolist()) if len(frame) else ()
        groups.setdefault(header, []).append(name)
    parts = []
    for header, names in groups.items():
        name = names[0]
        frame = tables[name]
        sqlite = ".sqlite::" in name
        h = -1 if sqlite else (_header_index(frame) if len(frame) else 0)
        body = frame if sqlite else frame.iloc[h + 1 :]
        lines = [f"### {name}  (rows={len(body)}, header_row={h if not sqlite else 'sql'})"]
        if not sqlite and h > 0:
            for i in range(h):
                lines.append("title: " + " | ".join(c for c in map(_cell, frame.iloc[i]) if c))
        lines.append("columns: " + " | ".join(header[:MAX_COLUMNS]))
        rows = list(range(min(sample_rows, len(body))))
        if len(body) > sample_rows:
            rows.append(len(body) - 1)
        for i in rows:
            cells = [_cell(v) for v in body.iloc[i].tolist()[:MAX_COLUMNS]]
            lines.append(("last: " if i == len(body) - 1 and i >= sample_rows else "row: ")
                         + " | ".join(cells))
        if len(names) > 1:
            lines.append(f"same columns ({len(names) - 1} more): " + ", ".join(names[1:]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


INSTRUCTIONS = """Bạn là chuyên viên phân tích dữ liệu cho một cửa hàng nhỏ ở Việt Nam. Trả lời câu hỏi
bằng cách viết MỘT khối code Python (```python ... ```) tính đáp án từ các bảng dữ liệu của shop.

Quy tắc:
- Làm đúng theo RULEBOOK và README bên dưới (câu hỏi thắng README, README thắng rulebook nếu rulebook ghi vậy).
- "Hôm nay" là TODAY (biến có sẵn, kiểu date). Bản ghi sau hôm nay coi như chưa xảy ra, trừ khi rulebook nói khác.
- Chỉ dùng dữ liệu trong bảng; không bịa. Nếu dữ liệu không đủ để trả lời: ANSWER = {"type": "unanswerable"}.
- Code phải gán biến ANSWER = {"type": <type>, "answer": <giá trị>} theo đúng format type trong rulebook
  (number, percent, string, date, boolean, list, table). Tiền là số nguyên VND. percent ghi số phần trăm (12.4).
- Tên lấy nguyên văn từ dữ liệu. Kiểm tra kỹ tên cột thật trong bảng (in ra không được xem).

API có sẵn (đã import pandas as pd, numpy as np, datetime, re, math):
- tables(): danh sách tên bảng ('file' hoặc 'file::sheet').
- table(name, header_row=None): DataFrame, dùng dòng header_row làm tên cột (None = tự dò như preview).
- raw(name): DataFrame header=None, giữ mọi dòng (dòng tiêu đề, dòng tổng).
- CALENDAR: DataFrame calendar.csv (có thể None). TODAY: date.
Giá trị ô có thể là chuỗi số kiểu "1.250.000", ngày dạng chuỗi, ô trống ở dòng sau của cùng nhóm: tự làm sạch.
Không đọc file, không dùng mạng. Chỉ trả về khối code, không giải thích."""


def build_messages(
    question: str, today: str, readme: str, rulebook: str, table_view: str, calendar_head: str
) -> list[dict]:
    weekday = WEEKDAYS[date.fromisoformat(today).weekday()]
    user = (
        f"# RULEBOOK\n{rulebook.strip()}\n\n# README của shop\n{readme.strip() or '(trống)'}\n\n"
        f"# CÁC BẢNG (xem trước)\n{table_view}\n\n# CALENDAR (vài dòng đầu)\n{calendar_head}\n\n"
        f"# CÂU HỎI\nHôm nay (TODAY) = {today} ({weekday}).\n{question}"
    )
    return [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": user}]


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S)
    if blocks:
        return max(blocks, key=len)
    if "ANSWER" in text:
        return text
    return None


def run_code(code: str, tables_path: Path, today: str, timeout: float = 40) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.py"
        script.write_text(PRELUDE + "\n" + code + "\n" + EPILOGUE, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "PYTHONNOUSERSITE": "1",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script), str(tables_path), today],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
    marker = proc.stdout.rfind("__ANSWER__")
    if proc.returncode != 0 or marker < 0:
        return {"error": (proc.stderr or proc.stdout)[-400:]}
    try:
        return {"answer": json.loads(proc.stdout[marker + len("__ANSWER__") :].strip())}
    except ValueError as exc:
        return {"error": f"bad answer json: {exc}"}


def _norm(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().casefold()
    if isinstance(value, list):
        return tuple(_norm(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _norm(v)) for k, v in value.items()))
    return str(value)


def vote_key(answer: dict) -> str:
    return json.dumps([answer.get("type"), _norm(answer.get("answer"))], ensure_ascii=False,
                      default=str)


class CodeAgent:
    name = "code-fallback"

    def __init__(self, model, root: Path, cache_dir: Path, *, samples: int = 4,
                 min_votes: int = 3, temperature: float = 0.6, max_tokens: int = 1400,
                 timeout: float = 600, answer_unanswerable: bool = False):
        self.model, self.root, self.cache_dir = model, root, cache_dir
        self.samples, self.min_votes, self.temperature = samples, min_votes, temperature
        self.max_tokens, self.timeout = max_tokens, timeout
        self.answer_unanswerable = answer_unanswerable
        self._shops: dict[str, tuple[Path, str]] = {}
        self._rulebook = (root / "rulebook.md").read_text(encoding="utf-8-sig")
        calendar = root / "calendar.csv"
        self._calendar_head = (
            "\n".join(calendar.read_text(encoding="utf-8-sig").splitlines()[:3])
            if calendar.exists() else "(không có)"
        )

    @property
    def metadata(self) -> dict:
        return {
            "prompt_version": PROMPT_VERSION, "samples": self.samples,
            "min_votes": self.min_votes, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "answer_unanswerable": self.answer_unanswerable,
        }

    def _shop(self, business: str) -> tuple[Path, str]:
        if business not in self._shops:
            import pandas as pd

            shop_dir = self.root / "data" / business
            tables = load_tables(shop_dir)
            view = preview(tables)
            if len(view) > PREVIEW_CHAR_BUDGET:
                view = preview(tables, sample_rows=1)
            if len(view) > PREVIEW_CHAR_BUDGET:
                view = view[:PREVIEW_CHAR_BUDGET] + "\n…(cắt bớt)"
            calendar = self.root / "calendar.csv"
            if calendar.exists():
                tables["__calendar__"] = pd.read_csv(calendar, encoding="utf-8-sig")
            path = self.cache_dir / "code_agent" / f"{business}.pkl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                pickle.dump(tables, stream)
            self._shops[business] = (path, view)
        return self._shops[business]

    def answer(self, question) -> dict:
        started = time.monotonic()
        log: dict[str, Any] = {"prompt_version": PROMPT_VERSION}
        tables_path, view = self._shop(question.business_id)
        readme_path = self.root / "data" / question.business_id / "README.md"
        readme = readme_path.read_text(encoding="utf-8-sig") if readme_path.exists() else ""
        messages = build_messages(
            question.question, question.as_of_date, readme, self._rulebook, view,
            self._calendar_head,
        )
        result = self.model.sample(
            messages, self.timeout, n=self.samples, temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        log["usage"] = result.get("usage")
        log["model_seconds"] = result.get("seconds")
        from concurrent.futures import ThreadPoolExecutor

        codes = [extract_code(text) for text in result["texts"]]
        unique = list(dict.fromkeys(c for c in codes if c is not None))
        with ThreadPoolExecutor(max_workers=max(1, len(unique))) as pool:
            by_code = dict(
                zip(unique, pool.map(lambda c: run_code(c, tables_path, question.as_of_date), unique))
            )
        runs = []
        for code in codes:
            if code is None:
                runs.append({"error": "no code"})
                continue
            run = dict(by_code[code])
            run["code"] = code[-1500:]
            if "answer" in run:
                candidate = run["answer"]
                if not isinstance(candidate, dict) or "type" not in candidate:
                    run = {"error": "ANSWER is not {type, answer}", "code": run["code"]}
                else:
                    record = {"id": question.id, "type": candidate["type"]}
                    if candidate["type"] not in {"unanswerable", "abstain"}:
                        record["answer"] = candidate.get("answer")
                    try:
                        validate_answer(record)
                        run["record"] = record
                    except ContractError as exc:
                        run["error"] = f"format: {exc}"
            runs.append(run)
        votes = collections.Counter(vote_key(r["record"]) for r in runs if "record" in r)
        log["runs"] = runs
        log["votes"] = dict(votes)
        log["seconds"] = time.monotonic() - started
        answer = {"id": question.id, "type": "abstain"}
        if votes:
            key, count = votes.most_common(1)[0]
            chosen = next(r["record"] for r in runs if "record" in r and vote_key(r["record"]) == key)
            log["top_votes"] = count
            if count >= self.min_votes and (
                chosen["type"] != "unanswerable" or self.answer_unanswerable
            ):
                answer = chosen
        log["chosen"] = answer["type"]
        return {"answer": answer, "log": log}


class FallbackPlanner:
    """Rule planner first; the code agent only answers what the planner abstains on."""

    accepts_missing_context = True

    def __init__(self, base, agent: CodeAgent, *, skip_base: bool = False):
        self.base, self.agent, self.skip_base = base, agent, skip_base
        self.name = f"{base.name}+{agent.name}" + ("-only" if skip_base else "")

    @property
    def metadata(self) -> dict:
        return {**self.base.metadata, "fallback": self.agent.metadata,
                "skip_base": self.skip_base}

    def decide(self, question, *, context) -> dict:
        decision = None
        if context is not None and not self.skip_base:
            decision = self.base.decide(question, context=context)
            if decision["answer"]["type"] != "abstain":
                return decision
        try:
            fallback = self.agent.answer(question)
        except Exception as exc:  # the fallback never turns an abstain into a question error
            fallback = {"answer": {"id": question.id, "type": "abstain"},
                        "log": {"error": f"{type(exc).__name__}: {exc}"}}
        plan = dict(decision["plan"]) if decision else {"status": "no_context"}
        plan["fallback"] = fallback["log"]
        return {
            **(decision or {}),
            "answer": fallback["answer"],
            "plan": plan,
            "execution": "code_fallback" if fallback["answer"]["type"] != "abstain"
            else (decision or {}).get("execution", "not_performed"),
        }
