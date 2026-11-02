"""Read only the split contract and business READMEs, never answer files."""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.contracts import require_id
from src.jsonio import ContractError, read_jsonl
from src.semantics.rulebook import RuleProfile, parse_rulebook


@dataclass(frozen=True)
class Question:
    id: str
    business_id: str
    question: str
    as_of_date: str


@dataclass(frozen=True)
class CalendarDay:
    day: date
    weekday: int
    lunar_day: int
    lunar_month: int
    lunar_year: int
    lunar_leap: bool
    holiday: str


@dataclass(frozen=True)
class Split:
    root: Path
    questions: tuple[Question, ...]
    businesses: tuple[str, ...]
    source_hashes: dict[str, str]
    calendar_days: int
    calendar: tuple[CalendarDay, ...] = ()
    rules: RuleProfile | None = None
    rulebook_text: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "questions": len(self.questions),
            "businesses": len(self.businesses),
            "calendar_days": self.calendar_days,
            "source_hashes": self.source_hashes,
        }


def inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _source(root: Path, relative: str) -> Path:
    candidate = root / relative
    if any(p.is_symlink() for p in [candidate, *candidate.parents] if p.is_relative_to(root)):
        raise ContractError(f"source symlink is forbidden: {relative}")
    path = candidate.resolve()
    if not inside(path, root) or not path.is_file():
        raise ContractError(f"missing/escaping input: {relative}")
    return path


def _iso_day(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ContractError("as_of_date/date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError("invalid calendar date") from exc


def load_split(data_dir: Path) -> Split:
    root = data_dir.resolve()
    if not root.is_dir():
        raise ContractError("data-dir must be an existing split directory")
    sources: dict[str, str] = {}

    def checked(relative: str) -> Path:
        path = _source(root, relative)
        sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    rulebook = checked("rulebook.md").read_text(encoding="utf-8-sig")
    if not rulebook.strip():
        raise ContractError("rulebook.md is empty")
    calendar_path = checked("calendar.csv")
    previous: date | None = None
    calendar_dates: set[str] = set()
    calendar: list[CalendarDay] = []
    required = {
        "date",
        "weekday",
        "lunar_day",
        "lunar_month",
        "lunar_year",
        "lunar_leap",
        "holiday",
    }
    with calendar_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ContractError("calendar.csv is missing required columns")
        for line, row in enumerate(reader, 2):
            try:
                current = _iso_day(row["date"])
                if previous is not None and current != previous + timedelta(days=1):
                    raise ContractError("calendar dates must be unique and continuous")
                if int(row["weekday"]) != current.isoweekday():
                    raise ContractError("calendar weekday differs from date")
                if not 1 <= int(row["lunar_day"]) <= 30:
                    raise ContractError("invalid lunar day")
                if not 1 <= int(row["lunar_month"]) <= 12:
                    raise ContractError("invalid lunar month")
                if int(row["lunar_year"]) <= 0 or row["lunar_leap"] not in {"true", "false"}:
                    raise ContractError("invalid lunar year/leap flag")
            except (ValueError, TypeError) as exc:
                raise ContractError(f"calendar.csv:{line}: {exc}") from exc
            calendar_dates.add(row["date"])
            calendar.append(
                CalendarDay(
                    current,
                    current.isoweekday(),
                    int(row["lunar_day"]),
                    int(row["lunar_month"]),
                    int(row["lunar_year"]),
                    row["lunar_leap"] == "true",
                    row["holiday"],
                )
            )
            previous = current
    if not calendar_dates:
        raise ContractError("calendar.csv has no dates")
    questions: list[Question] = []
    ids: set[str] = set()
    businesses: set[str] = set()
    for line, record in read_jsonl(checked("questions.jsonl")):
        try:
            qid = require_id(record)
            if qid in ids:
                raise ContractError("duplicate question id")
            business = record.get("business_id")
            if not isinstance(business, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", business):
                raise ContractError("business_id must be a safe, nonempty identifier")
            text = record.get("question")
            if not isinstance(text, str) or not text.strip():
                raise ContractError("question must be a nonempty string")
            day = record.get("as_of_date")
            _iso_day(day)
            if day not in calendar_dates:
                raise ContractError("as_of_date is outside the supplied calendar")
            if business not in businesses:
                directory = (root / "data" / business).resolve()
                if not inside(directory, root) or not directory.is_dir():
                    raise ContractError("business directory missing or escapes split")
                if (
                    not checked(f"data/{business}/README.md")
                    .read_text(encoding="utf-8-sig")
                    .strip()
                ):
                    raise ContractError("business README is empty")
                files = [
                    f for f in directory.rglob("*") if f.suffix in {".csv", ".xlsx", ".sqlite"}
                ]
                if not files or any(not inside(f.resolve(), root) for f in files):
                    raise ContractError("business data missing or symlink escapes split")
                businesses.add(business)
            questions.append(Question(qid, business, text, day))
            ids.add(qid)
        except ContractError as exc:
            raise ContractError(f"questions.jsonl:{line}: {exc}") from exc
    if not questions:
        raise ContractError("questions.jsonl has no questions")
    rules = parse_rulebook(rulebook, root.name, source_sha256=sources["rulebook.md"])
    return Split(
        root,
        tuple(questions),
        tuple(sorted(businesses)),
        sources,
        len(calendar_dates),
        tuple(calendar),
        rules,
        rulebook,
    )


def question_dict(question: Question) -> dict[str, Any]:
    return asdict(question)
