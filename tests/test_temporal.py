from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.data.split import CalendarDay
from src.semantics.rulebook import SemanticError, parse_rulebook
from src.temporal.resolver import resolve_window


@pytest.mark.parametrize(
    "expression,anchor,start,end",
    [
        ({"kind": "today"}, "2026-01-02", "2026-01-02", "2026-01-02"),
        ({"kind": "yesterday"}, "2026-01-01", "2025-12-31", "2025-12-31"),
        ({"kind": "current_week"}, "2026-01-02", "2025-12-29", "2026-01-02"),
        ({"kind": "previous_week"}, "2026-01-02", "2025-12-22", "2025-12-28"),
        ({"kind": "last_n_days", "days": 7}, "2026-01-02", "2025-12-27", "2026-01-02"),
        ({"kind": "current_month"}, "2025-03-31", "2025-03-01", "2025-03-31"),
        ({"kind": "previous_mtd"}, "2025-03-31", "2025-02-01", "2025-02-28"),
        ({"kind": "previous_mtd"}, "2024-03-31", "2024-02-01", "2024-02-29"),
        ({"kind": "previous_month"}, "2026-01-02", "2025-12-01", "2025-12-31"),
        ({"kind": "previous_mtd"}, "2026-01-02", "2025-12-01", "2025-12-02"),
        ({"kind": "month", "month": 3, "year": 2025}, "2025-03-02", "2025-03-01", "2025-03-31"),
        ({"kind": "month", "month": 4}, "2025-03-02", "2024-04-01", "2024-04-30"),
        ({"kind": "month", "month": 3}, "2025-03-02", "2025-03-01", "2025-03-31"),
        ({"kind": "quarter", "quarter": 2}, "2025-03-02", "2024-04-01", "2024-06-30"),
        ({"kind": "quarter", "quarter": 1, "year": 2024}, "2024-01-02", "2024-01-01", "2024-03-31"),
        ({"kind": "year", "year": 2024}, "2024-01-02", "2024-01-01", "2024-12-31"),
        ({"kind": "year_to_date"}, "2024-02-29", "2024-01-01", "2024-02-29"),
    ],
)
def test_calendar_windows_have_independent_expected_bounds(
    rulebook_text, expression, anchor, start, end
):
    window = resolve_window(expression, anchor, parse_rulebook(rulebook_text, "synthetic"))
    assert (window.start.isoformat(), window.end.isoformat()) == (start, end)
    assert window.summary()["inclusive"] is True
    assert window.cutoff is None


def test_explicit_future_days_and_fractional_timestamp_end_are_preserved(rulebook_text):
    rules = parse_rulebook(rulebook_text, "synthetic")
    expression = {"kind": "range", "start": "2026-01-01", "end": "2026-01-03"}
    window = resolve_window(expression, "2026-01-02", rules)
    assert window.contains("2026-01-03T23:59:59.999999+07:00")
    assert not window.contains("2026-01-04T00:00:00+07:00")
    cutoff = resolve_window({**expression, "cutoff": "as_of"}, "2026-01-02", rules)
    assert cutoff.end == window.end  # requested window is not overwritten
    assert not cutoff.contains("2026-01-03")


def test_week_rule_change_changes_actual_dates_and_unknown_rule_fails(rulebook_text):
    modified = rulebook_text.replace("thứ Hai", "Chủ nhật").replace(
        "Chủ nhật đến Chủ nhật", "Chủ nhật đến thứ Bảy"
    )
    rules = parse_rulebook(modified, "synthetic")
    window = resolve_window({"kind": "current_week"}, "2026-01-02", rules)
    assert window.start == date(2025, 12, 28)
    unknown = parse_rulebook(rulebook_text.replace("bắt đầu thứ Hai", "bắt đầu thứ Ba"), "test")
    with pytest.raises(SemanticError) as error:
        resolve_window({"kind": "current_week"}, "2026-01-02", unknown)
    assert error.value.code == "rule_unresolved"


@pytest.mark.parametrize(
    "expression",
    [
        {"kind": "last_n_days", "days": 0},
        {"kind": "last_n_days", "days": True},
        {"kind": "month", "month": 13},
        {"kind": "year"},
        {"kind": "range", "start": "2026-01-03", "end": "2026-01-01"},
        {"kind": "today", "invented_filter": True},
        {"kind": "today", "cutoff": []},
        {"kind": "last_n_days", "days": 3652059},
        {"kind": "snapshot"},
        {"kind": "day", "date": "2026-02-30"},
    ],
)
def test_invalid_or_unimplemented_expressions_have_typed_errors(rulebook_text, expression):
    with pytest.raises(SemanticError):
        resolve_window(expression, "2026-01-02", parse_rulebook(rulebook_text, "synthetic"))


def test_lunar_uses_supplied_calendar_and_checks_both_windows(rulebook_text):
    # Deliberately synthetic lunar mapping; never derive dates from holiday text.
    first = date(2026, 1, 1)
    days = tuple(
        CalendarDay(
            first + timedelta(days=i),
            (first + timedelta(days=i)).isoweekday(),
            23 if i == 20 else 10 if i == 36 else 1,
            12 if i <= 20 else 1,
            2025 if i <= 20 else 2026,
            False,
            "",
        )
        for i in range(40)
    )
    rules = parse_rulebook(rulebook_text, "synthetic")
    tet = resolve_window({"kind": "tet", "year": 2026}, "2026-01-02", rules, days)
    before = resolve_window({"kind": "before_tet", "year": 2026}, "2026-01-02", rules, days)
    assert (tet.start, tet.end) == (date(2026, 1, 21), date(2026, 2, 6))
    assert (before.start, before.end) == (date(2026, 1, 4), date(2026, 1, 20))
    assert (tet.end - tet.start).days == (before.end - before.start).days == 16
    for broken in [
        (),
        days[:36],
        days[10:],
        days[:22] + days[23:],
        days + (replace(days[20], day=date(2026, 3, 1)),),
    ]:
        with pytest.raises(SemanticError) as error:
            resolve_window({"kind": "before_tet", "year": 2026}, "2026-01-02", rules, broken)
        assert error.value.code == "calendar_lookup"
