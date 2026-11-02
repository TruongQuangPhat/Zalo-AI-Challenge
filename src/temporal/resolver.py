"""Resolve calendar windows without question parsing or transaction-coverage guesses."""

from __future__ import annotations

import calendar as gregorian
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from src.data.normalization.values import parse_datetime
from src.semantics.rulebook import RuleProfile, SemanticError

if TYPE_CHECKING:
    from src.data.split import CalendarDay

VERSION = "temporal-v1"


def iso_day(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise SemanticError("time_expression", "date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SemanticError("time_expression", "invalid ISO day") from exc
    if parsed.isoformat() != value:
        raise SemanticError("time_expression", "date must be YYYY-MM-DD")
    return parsed


@dataclass(frozen=True)
class TimeWindow:
    window_id: str
    kind: str
    start: date
    end: date
    as_of_date: date
    cutoff: date | None
    rule_refs: tuple[str, ...]

    def summary(self) -> dict:
        return {
            "id": self.window_id,
            "expression_kind": self.kind,
            "time_axis": "ordered_at",
            "requested_start": self.start.isoformat(),
            "requested_end": self.end.isoformat(),
            "inclusive": True,
            "as_of_date": self.as_of_date.isoformat(),
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
            "rule_refs": list(self.rule_refs),
            "timezone_policy": "source_local_date",
        }

    def contains(self, value: str) -> bool:
        """Keep source-local date/offset; include all fractional seconds on the last day."""
        day = parse_datetime(value).date()
        return self.start <= day <= min(self.end, self.cutoff or self.end)


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise SemanticError("time_expression", f"{name} must be an integer in {lower}..{upper}")
    return value


def _month(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, gregorian.monthrange(year, month)[1])


def _continuous(calendar: tuple[CalendarDay, ...], start: date, end: date) -> None:
    selected = [day.day for day in calendar if start <= day.day <= end]
    if len(selected) != (end - start).days + 1 or any(
        day != start + timedelta(days=i) for i, day in enumerate(selected)
    ):
        raise SemanticError("calendar_lookup", "lunar window is not covered continuously")


def resolve_window(
    expression: dict,
    as_of_date: str,
    rules: RuleProfile,
    calendar: tuple[CalendarDay, ...] = (),
) -> TimeWindow:
    """Resolve the S1 grammar; invalid/unsupported expressions raise a typed diagnostic.

    Explicit periods remain whole periods. A structured question may explicitly
    request cutoff='as_of'; this never becomes a global preparation filter.
    """
    allowed = {
        "day": {"date"},
        "range": {"start", "end"},
        "today": set(),
        "yesterday": set(),
        "current_week": set(),
        "previous_week": set(),
        "month": {"year", "month"},
        "current_month": set(),
        "previous_month": set(),
        "previous_mtd": set(),
        "last_n_days": {"days"},
        "year": {"year"},
        "year_to_date": set(),
        "quarter": {"year", "quarter"},
        "tet": {"year"},
        "before_tet": {"year"},
    }
    if not isinstance(expression, dict) or not isinstance(expression.get("kind"), str):
        raise SemanticError("time_expression", "structured kind is required")
    kind = expression["kind"]
    if kind not in allowed or set(expression) - (allowed[kind] | {"kind", "id", "cutoff"}):
        raise SemanticError("time_unsupported", "unknown time operator or expression fields")
    if expression.get("cutoff") is not None and expression["cutoff"] != "as_of":
        raise SemanticError("time_expression", "cutoff must be null or as_of")
    window_id = expression.get("id", "period")
    if not isinstance(window_id, str) or not window_id.strip():
        raise SemanticError("time_expression", "window ID must be nonempty text")
    anchor = iso_day(as_of_date)
    refs = []

    def require(rule_id: str):
        refs.append(rule_id)
        return rules.require(rule_id)

    try:
        if kind == "day":
            start = end = iso_day(expression.get("date"))
        elif kind == "range":
            require("inclusive_range")
            start, end = (iso_day(expression.get(field)) for field in ["start", "end"])
        elif kind in {"today", "yesterday"}:
            require("as_of")
            start = end = anchor - timedelta(days=kind == "yesterday")
        elif kind in {"current_week", "previous_week"}:
            require("as_of")
            first = require("week").parameters["first_weekday"]
            start = anchor - timedelta(days=(anchor.weekday() - first) % 7)
            if kind == "previous_week":
                end, start = start - timedelta(days=1), start - timedelta(days=7)
            else:
                end = anchor
        elif kind in {"month", "quarter", "year"}:
            if kind == "month":
                require("explicit_month")
                month = _integer(expression.get("month"), "month", 1, 12)
            elif kind == "quarter":
                require("missing_year")  # Also establishes Gregorian quarter definition.
                month = 3 * (_integer(expression.get("quarter"), "quarter", 1, 4) - 1) + 1
            else:
                month = 1
            year = _integer(expression.get("year", anchor.year), "year", 1, 9999)
            if "year" not in expression:
                if kind == "year":
                    raise SemanticError("time_expression", "explicit year is required")
                require("as_of")
                require("missing_year")
                year -= date(year, month, 1) > anchor
            start = date(year, month, 1)
            last_month = month if kind == "month" else month + 2 if kind == "quarter" else 12
            end = _month(year, last_month)[1]
        elif kind in {"current_month", "previous_month", "previous_mtd"}:
            require("as_of")
            require("explicit_month")
            require("current_month")
            year, month = anchor.year, anchor.month
            if kind != "current_month":
                year, month = (year, month - 1) if month > 1 else (year - 1, 12)
            start, end = _month(year, month)
            if kind != "previous_month":
                end = date(year, month, min(anchor.day, end.day))
        elif kind == "last_n_days":
            require("as_of")
            require("last_n_days")
            days = _integer(expression.get("days"), "days", 1, 3652059)
            start, end = anchor - timedelta(days=days - 1), anchor
        elif kind == "year_to_date":
            require("as_of")
            require("year_to_date")
            start, end = date(anchor.year, 1, 1), anchor
        else:
            rule = require("tet")
            year = _integer(expression.get("year"), "lunar year", 2, 9999)
            boundaries = []
            for side, lunar_year in [("start", year - 1), ("end", year)]:
                found = [
                    day.day
                    for day in calendar
                    if (
                        day.lunar_year == lunar_year
                        and not day.lunar_leap
                        and day.lunar_month == rule.parameters[side + "_month"]
                        and day.lunar_day == rule.parameters[side + "_day"]
                    )
                ]
                if len(found) != 1:
                    raise SemanticError("calendar_lookup", "missing or ambiguous lunar boundary")
                boundaries.append(found[0])
            start, end = boundaries
            if start > end:
                raise SemanticError("calendar_lookup", "lunar boundaries are reversed")
            _continuous(calendar, start, end)
            if kind == "before_tet":
                days = (end - start).days + 1
                start, end = start - timedelta(days=days), start - timedelta(days=1)
                _continuous(calendar, start, end)
        if start > end:
            raise SemanticError("time_expression", "window start is after its end")
        if expression.get("cutoff") == "as_of":
            require("as_of")
        return TimeWindow(
            window_id,
            kind,
            start,
            end,
            anchor,
            anchor if expression.get("cutoff") else None,
            tuple(dict.fromkeys(refs)),
        )
    except SemanticError:
        raise
    except (ValueError, OverflowError) as exc:
        raise SemanticError(
            "time_expression", "calendar arithmetic is outside valid dates"
        ) from exc
