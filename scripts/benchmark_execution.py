"""Synthetic facts benchmark; never opens released questions, transactions or labels."""

from __future__ import annotations

import argparse
import resource
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

from src.agent.business_execution import _reduce
from src.agent.execution import _compute
from src.data.cache import fingerprint
from src.data.catalog import digest
from src.data.normalization.entities import SCHEMA as EXTENSION_SCHEMA
from src.data.normalization.store import SCHEMA, PreparedBusiness
from src.jsonio import write_json


class MeasuredCursor(sqlite3.Cursor):
    def fetchone(self):
        row = super().fetchone()
        if row is not None:
            self.connection.counters["rows_delivered"] += 1
            self.connection.counters["scalar_bytes_delivered"] += sum(
                len(str(value).encode()) for value in row if value is not None
            )
        return row

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def fetchall(self):
        return list(self)


class MeasuredConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        self.counters["queries"] += 1
        return self.cursor(factory=MeasuredCursor).execute(sql, parameters)

    def set_progress_handler(self, handler, interval):
        def count():
            self.counters["vm_steps_lower_bound"] += interval
            return handler() if handler else 0

        super().set_progress_handler(count, interval or 1000)


@dataclass(frozen=True)
class MeasuredPrepared(PreparedBusiness):
    counters: dict = field(default_factory=dict)

    def connect(self):
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, factory=MeasuredConnection
        )
        connection.counters = self.counters
        connection.set_progress_handler(None, 1000)
        return connection


def frozen_module(ref: str, name: str) -> ModuleType:
    source = subprocess.run(
        ["git", "show", f"{ref}:{name}"], check=True, capture_output=True, text=True
    ).stdout
    module = ModuleType("synthetic_frozen_" + name.replace("/", "_").replace(".", "_"))
    sys.modules[module.__name__] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def benchmark(directory: Path, orders: int = 100000, baseline_ref: str | None = None) -> dict:
    if orders < 100:
        raise ValueError("benchmark requires at least 100 orders")
    compute, reduce, schema, extension_schema = _compute, _reduce, SCHEMA, EXTENSION_SCHEMA
    if baseline_ref:
        compute = frozen_module(baseline_ref, "src/agent/execution.py")._compute
        reduce = frozen_module(baseline_ref, "src/agent/business_execution.py")._reduce
        schema = frozen_module(baseline_ref, "src/data/normalization/store.py").SCHEMA
        extension_schema = frozen_module(baseline_ref, "src/data/normalization/entities.py").SCHEMA
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "facts.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema + extension_schema)
        conn.executemany(
            "INSERT INTO orders VALUES (?,?,?,?,?)",
            (
                (
                    f"o{i:08d}",
                    str(i),
                    "2026-01-31T23:59:59.999999+07:00" if i >= orders - 100 else "2025-01-01",
                    "completed",
                    "resolved",
                )
                for i in range(orders)
            ),
        )
        conn.executemany(
            "INSERT INTO order_lines VALUES (?,?,?,?,?,?)",
            (
                (
                    f"l{i:08d}_{j}",
                    f"o{i:08d}",
                    "100000000000000000000000000000.125",
                    "sale_item",
                    "{}",
                    None,
                )
                for i in range(orders)
                for j in range(3)
            ),
        )
        conn.executemany(
            "INSERT INTO lineage VALUES (?,?,?,?,?,?,?)",
            (
                (f"r{i:08d}_{j}", "synthetic", i, "kept", f"l{i:08d}_{j}", "{}", "[]")
                for i in range(orders)
                for j in range(3)
            ),
        )
        conn.execute(
            "INSERT INTO entities VALUES ('product','product','sku','Synthetic','[]','unit','{}')"
        )
        conn.executemany(
            "INSERT INTO line_items VALUES (?,?,?,?,?,?)",
            (
                (f"l{i:08d}_{j}", "product", "2.5", "1", "1", "{}")
                for i in range(orders)
                for j in range(3)
            ),
        )
        conn.commit()
    before = digest(path)
    counters = {}
    context = SimpleNamespace(
        prepared=MeasuredPrepared(
            path,
            {"shape": "order_lines", "extensions": {"counts": {"entities": 1}}},
            counters=counters,
        )
    )
    records = []
    for temperature in ["connection_cold", "warm_repeat"]:
        for metric in ["revenue", "order_count", "calendar_day_count", "quantity"]:
            check = {
                "contract": {
                    "metric_id": metric,
                    "formula": "sum_exact_lines",
                    "status_policy": ["completed", "shipping"],
                    "row_roles": ["sale_item", "service_fee"],
                    "rounding": {"decimal_places": 0},
                },
                "effective_window": {"start": "2026-01-31", "end": "2026-01-31"},
            }
            counters.clear()
            counters.update(
                queries=0, rows_delivered=0, scalar_bytes_delivered=0, vm_steps_lower_bound=0
            )
            started = time.monotonic()
            try:
                if metric == "quantity":
                    result = reduce(
                        context,
                        {"business_spec": {"group": None, "metric": metric}},
                        {**check, "metric_id": metric},
                        None,
                    )
                    result["exact_value"] = result["value"]
                else:
                    result = compute(context, check, round_result=False)
                expected = (
                    Decimal("30000000000000000000000000000037.500")
                    if metric == "revenue"
                    else Decimal(
                        750 if metric == "quantity" else 100 if metric == "order_count" else 1
                    )
                )
                assert result["exact_value"] == expected, (result["exact_value"], expected)
                record = {
                    "status": "verified",
                    "exact_value": result["exact_value"],
                    "diagnostics": result.get("scan"),
                    "orders_selected": result.get("orders_selected"),
                    "lines_selected": result.get("lines_selected"),
                }
            except Exception as exc:
                record = {"status": "failed", "error": str(exc)}
            records.append(
                {
                    "temperature": temperature,
                    "metric": metric,
                    "wall_seconds": time.monotonic() - started,
                    "profile": dict(counters),
                    **record,
                }
            )
    assert digest(path) == before
    workload = {
        "version": "synthetic-scans-v1",
        "orders": orders,
        "lines_per_order": 3,
        "in_window_orders": 100,
        "money": "100000000000000000000000000000.125",
        "window": {"start": "2026-01-31", "end": "2026-01-31"},
        "statuses": ["completed", "shipping"],
        "execution_guard_seconds": 15,
    }
    report = {
        "workload": workload,
        "workload_sha256": fingerprint(workload),
        "benchmark_source_sha256": digest(Path(__file__)),
        "baseline_ref": baseline_ref,
        "orders": orders,
        "lines": orders * 3,
        "lineage": orders * 3,
        "db_bytes": path.stat().st_size,
        "facts_sha256": before,
        "rss_peak_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "runs": records,
        "limitations": (
            "Connection cold, OS cache not evicted; synthetic SQL/reduction only, "
            "no model/preflight or native SLA."
        ),
    }
    write_json(directory / "benchmark.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--orders", type=int, default=100000)
    parser.add_argument("--baseline-ref", help="frozen local Git revision for synthetic comparison")
    args = parser.parse_args()
    print(benchmark(args.output_dir, args.orders, args.baseline_ref))
