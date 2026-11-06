"""Bounded read-only SQL scans with one monotonic deadline and measured row delivery."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING

from src.data.normalization.values import exact_add
from src.jsonio import ContractError

if TYPE_CHECKING:
    from src.data.normalization.store import PreparedBusiness


def failure(exc: Exception, stage: str) -> dict:
    return {
        "status": "failed",
        "reason": str(exc),
        "error_stage": stage,
        "error_code": str(exc) if isinstance(exc, ContractError) else type(exc).__name__,
        "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
        "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
        **getattr(exc, "diagnostics", {}),
    }


class Scan:
    def __init__(self, connection: sqlite3.Connection | None, deadline: float | None):
        self.connection = connection
        self.started = time.monotonic()
        self.execution_deadline = self.started + 15
        self.question_deadline = deadline
        self.cursors = set()
        self.stage = "sql_scan"
        self.stats = {
            "queries": 0,
            "rows_delivered": 0,
            "vm_steps_lower_bound": 0,
            "sql_seconds": 0.0,
            "budget_hit": None,
            "decimal_seconds": 0.0,
            "decimal_values_decoded": 0,
        }

    def expired(self) -> str | None:
        now = time.monotonic()
        if self.question_deadline is not None and now >= self.question_deadline:
            return "question_deadline"
        if now >= self.execution_deadline:
            return "execution_15_seconds"
        return None

    def budget(self) -> None:
        if hit := self.expired():
            self.stats["budget_hit"] = hit
            raise ContractError(
                "execution_timeout",
                diagnostics={
                    "budget": hit,
                    "error_stage": self.stage,
                    "scan": self.summary(),
                },
            )

    def progress(self) -> int:
        self.stats["vm_steps_lower_bound"] += 1000
        hit = self.expired()
        if hit:
            self.stats["budget_hit"] = hit
        return int(hit is not None)

    def number(self, raw: str | int) -> Decimal:
        started = time.monotonic()
        try:
            self.stats["decimal_values_decoded"] += 1
            return Decimal(raw)
        finally:
            self.stats["decimal_seconds"] += time.monotonic() - started

    def add(self, left: Decimal, right: Decimal) -> Decimal:
        started = time.monotonic()
        try:
            return exact_add(left, right)
        finally:
            self.stats["decimal_seconds"] += time.monotonic() - started

    def rows(self, sql: str, params: Sequence = ()) -> Iterator[tuple]:
        self.stage = "sql_scan"
        self.budget()
        self.stats["queries"] += 1
        cursor = None
        try:
            started = time.monotonic()
            try:
                cursor = self.connection.execute(sql, params)
                self.cursors.add(cursor)
            finally:
                self.stats["sql_seconds"] += time.monotonic() - started
            while True:
                self.stage = "sql_scan"
                self.budget()
                started = time.monotonic()
                try:
                    row = cursor.fetchone()
                finally:
                    self.stats["sql_seconds"] += time.monotonic() - started
                if row is None:
                    break
                self.stats["rows_delivered"] += 1
                self.stage = "decimal_reduction"
                yield row
        finally:
            if cursor in self.cursors:
                cursor.close()
                self.cursors.remove(cursor)

    def summary(self) -> dict:
        elapsed = time.monotonic() - self.started
        return {
            **self.stats,
            "elapsed_seconds": elapsed,
            "python_seconds": max(0, elapsed - self.stats["sql_seconds"]),
            "rows_definition": "rows delivered to Python; VM work sampled every 1000 steps",
        }


@contextmanager
def scanning(prepared: PreparedBusiness, deadline: float | None) -> Iterator[Scan]:
    scan = Scan(None, deadline)
    connection = prepared.connect()
    scan.connection = connection
    connection.set_progress_handler(scan.progress, 1000)
    try:
        scan.budget()
        yield scan
        scan.budget()
    except Exception as exc:
        exc.diagnostics = {
            "scan": scan.summary(),
            "error_stage": scan.stage,
            "budget": scan.stats["budget_hit"],
        }
        raise
    finally:
        for cursor in list(scan.cursors):
            cursor.close()
            scan.cursors.remove(cursor)
        connection.set_progress_handler(None, 0)
        connection.close()
