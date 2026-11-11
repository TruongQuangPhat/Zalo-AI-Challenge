"""Store versioned prepared facts with exact decimals and revision-checked caching."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from src.data.cache import fingerprint
from src.data.catalog import BusinessContext, digest, discover_sources, require_output
from src.data.normalization.entities import build_entities
from src.data.normalization.llm_mapping import (
    Propose,
    check_proposal,
    normalize_proposal,
    request_proposal,
    request_status_aliases,
    synthesize_readme,
    verify_facts,
)
from src.data.normalization.relational import build_relational, relational_mapping
from src.data.normalization.sales import build_sales, sales_mapping
from src.data.normalization.stock import build_stock, stock_mapping
from src.data.normalization.values import STATUS_ALIASES, parse_date
from src.data.profiling import normalized
from src.jsonio import ContractError, dumps, loads

VERSION = "prepared-facts-v8"
MAX_STATUS_VALUES = 30  # more distinct rejected values than this is not a status column
SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE orders (
    order_key TEXT PRIMARY KEY, raw_order_id TEXT NOT NULL,
    ordered_at TEXT NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('completed','cancelled','returned','shipping')),
    mapping_status TEXT NOT NULL CHECK(mapping_status IN ('resolved','ambiguous'))
) STRICT;
CREATE TABLE order_lines (
    line_key TEXT PRIMARY KEY, order_key TEXT NOT NULL REFERENCES orders(order_key),
    net_amount TEXT, row_role TEXT NOT NULL, source_values TEXT NOT NULL, event_at TEXT
) STRICT;
CREATE INDEX orders_status_day ON orders(status,substr(ordered_at,1,10),order_key);
CREATE INDEX order_lines_order ON order_lines(order_key);
CREATE TABLE lineage (
    raw_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    disposition TEXT NOT NULL, line_key TEXT REFERENCES order_lines(line_key),
    source_ref TEXT NOT NULL, transformations TEXT NOT NULL
) STRICT;
CREATE INDEX lineage_line ON lineage(line_key);
CREATE TABLE issues (issue_id TEXT PRIMARY KEY, detail TEXT NOT NULL) STRICT;
CREATE TABLE inventory_snapshot (
    product_code TEXT PRIMARY KEY, product_name TEXT NOT NULL, base_unit TEXT NOT NULL,
    quantity TEXT NOT NULL, snapshot_at TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE order_revenues (
    order_key TEXT PRIMARY KEY REFERENCES orders(order_key),
    net_amount TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE sales_events (
    event_key TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, status TEXT NOT NULL,
    net_amount TEXT, row_role TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE INDEX sales_events_status_day ON sales_events(status,substr(occurred_at,1,10));
CREATE TABLE order_times (
    order_key TEXT PRIMARY KEY REFERENCES orders(order_key),
    seconds_of_day TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE order_scopes (
    order_key TEXT PRIMARY KEY REFERENCES orders(order_key), channel TEXT NOT NULL
) STRICT;
CREATE TABLE event_groups (
    event_key TEXT PRIMARY KEY REFERENCES sales_events(event_key), item_group TEXT NOT NULL
) STRICT;
CREATE TABLE order_groups (
    order_key TEXT PRIMARY KEY REFERENCES orders(order_key), customer_group TEXT NOT NULL
) STRICT;
CREATE TABLE line_groups (
    line_key TEXT PRIMARY KEY REFERENCES order_lines(line_key), item_group TEXT NOT NULL
) STRICT;
"""


@dataclass(frozen=True)
class PreparedBusiness:
    path: Path | None
    summary: dict
    cache_hit: bool = False
    elapsed_seconds: float = 0

    def connect(self) -> sqlite3.Connection:
        if self.path is None or self.path.is_symlink():
            raise ContractError("prepared store is unavailable")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    def iter_records(self, table: str) -> Iterator[dict]:
        if table not in {
            "orders",
            "order_lines",
            "lineage",
            "issues",
            "inventory_snapshot",
            "order_revenues",
            "sales_events",
            "order_times",
            "order_scopes",
            "order_groups",
            "line_groups",
            "event_groups",
            "entities",
            "order_parties",
            "line_items",
            "payments",
            "purchases",
            "stock_balances",
            "lots",
            "lot_allocations",
        }:
            raise ContractError("unknown prepared table")
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"):
                record = dict(row)
                if (
                    table in {"order_lines", "order_revenues", "sales_events"}
                    and record["net_amount"] is not None
                ):
                    record["net_amount"] = Decimal(record["net_amount"])
                if table == "inventory_snapshot":
                    record["quantity"] = Decimal(record["quantity"])
                for field in ["source_values", "source_ref", "transformations", "detail"]:
                    if field in record:
                        record[field] = loads(record[field])
                yield record


def _assert_revision(context: BusinessContext) -> None:
    catalog = context.catalog
    sources, excluded = discover_sources(context.root, catalog["business_id"])
    dependencies = catalog["dependencies"]
    if (
        sources != dependencies["sources"]
        or excluded != dependencies["excluded"]
        or any(
            digest(context.root / name) != expected
            for name, expected in dependencies["authority"].items()
        )
    ):
        raise ContractError("source/authority changed since raw catalog preparation")


def _copy_atomic(source: Path, target: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".writing-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, handle)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _cached(entry: Path, dependencies: dict) -> tuple[Path, dict] | None:
    try:
        path, manifest = entry / "facts.sqlite", entry / "manifest.json"
        if any(p.is_symlink() for p in [entry, path, manifest]):
            return None
        if loads(manifest.read_text()) != {
            "complete": True,
            "dependencies": dependencies,
            "facts_sha256": digest(path),
        }:
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return None
            metadata = {key: loads(value) for key, value in conn.execute("SELECT * FROM metadata")}
            if metadata["dependencies"] != dependencies:
                return None
            return path, metadata["summary"]
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return None


def _select_mapping(context: BusinessContext) -> tuple[object, object, dict[str, str]]:
    attempts = {}
    for name, mapper, builder in [
        ("sales", sales_mapping, build_sales),
        ("relational", relational_mapping, build_relational),
        ("snapshot", stock_mapping, build_stock),
    ]:
        try:
            return mapper(context), builder, attempts
        except ContractError as error:
            attempts[name] = str(error)
    return None, None, attempts


def _unsupported(context: BusinessContext, attempts: dict, started: float, **extra):
    result = PreparedBusiness(
        None,
        {
            "version": VERSION,
            "status": "unsupported",
            "reason": "; ".join(attempts.values()),
            "failure_family": "unsupported_mapping",
            "mapping_attempts": attempts,
            "metric_execution": "not_implemented",
            **extra,
        },
        elapsed_seconds=time.perf_counter() - started,
    )
    context.prepared = result
    return result


def prepare_facts(
    context: BusinessContext,
    output_dir: Path,
    *,
    cache_dir: Path | None = None,
    propose: Propose | None = None,
) -> PreparedBusiness:
    """Prepare supported facts once/business; unsupported shapes never become empty facts.

    ``propose`` (a model) is consulted only when no README mapper reads sales; its column
    meanings become README declarations and must pass the unchanged mappers, builders,
    quality gates and ``verify_facts`` (one retry with the rejection reasons).
    """
    started = time.perf_counter()
    require_output(output_dir, context.root)
    if cache_dir is not None:
        require_output(cache_dir, context.root)
    _assert_revision(context)
    mapping, builder, attempts = _select_mapping(context)
    # A shop read only as an inventory snapshot has unread sales; the model may map them.
    if mapping is not None and (propose is None or builder is not build_stock):
        return _with_statuses(context, output_dir, cache_dir, mapping, builder, started, propose)
    snapshot = mapping
    if propose is None:
        return _unsupported(context, attempts, started)
    if output_dir.exists():
        raise ContractError("prepared output already exists; use a fresh directory")
    end_hint = context.catalog["coverage"].get("available_end_hint")
    end_day = parse_date(end_hint).isoformat() if end_hint else None
    feedback: list[str] = []
    rejected = []
    for _ in range(2):
        try:
            raw = request_proposal(context.catalog, propose, cache_dir, feedback)
        except ContractError as error:
            rejected.append([str(error)])
            break
        proposal = normalize_proposal(context.catalog, raw)
        feedback = check_proposal(context.catalog, proposal)
        if not feedback:
            catalog = {**context.catalog, "readme": synthesize_readme(context.catalog, proposal)}
            trial = replace(context, catalog=catalog)
            mapping, builder, failed = _select_mapping(trial)
            if mapping is None or builder is build_stock:
                feedback = [f"{k}: {v}" for k, v in failed.items()] or ["sales still unmapped"]
        if not feedback:
            provenance = {
                "mapping_source": "llm_proposal",
                "proposal_sha256": fingerprint(proposal),
                "proposal": proposal,
                "raw_proposal": raw,
            }
            try:
                result = _with_statuses(
                    trial,
                    output_dir,
                    cache_dir,
                    mapping,
                    builder,
                    started,
                    propose,
                    provenance=provenance,
                    verify=lambda conn, summary: verify_facts(conn, summary, end_day),
                )
            except ContractError as error:
                feedback = [str(error)]
                if output_dir.is_dir() and not any(output_dir.iterdir()):
                    output_dir.rmdir()  # the rejected build left nothing; retry reuses it
            else:
                context.catalog = catalog
                context.prepared = result
                return result
        rejected.append(feedback)
    if snapshot is not None:
        fallback = {"mapping_source": "snapshot_fallback", "llm_rejections": rejected}
        return _store(
            context, output_dir, cache_dir, snapshot, build_stock, started, provenance=fallback
        )
    return _unsupported(
        context, attempts, started, mapping_source="llm_rejected", llm_rejections=rejected
    )


def _unknown_statuses(path: Path) -> list[str]:
    """Distinct raw status values the fixed vocabulary rejected, one per normalized token."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT json_extract(detail,'$.raw_value') FROM issues "
            "WHERE json_extract(detail,'$.code')='status_ambiguous'"
        ).fetchall()
    values = {}
    for (raw,) in rows:
        text = str(raw).strip() if raw is not None else ""
        if text:
            values.setdefault(normalized(text).strip(), text)
    return sorted(values.values())


def _with_statuses(
    context: BusinessContext,
    output_dir: Path,
    cache_dir: Path | None,
    mapping,
    builder,
    started: float,
    propose: Propose | None,
    *,
    provenance: dict | None = None,
    verify=None,
) -> PreparedBusiness:
    """Build; if order statuses outside the fixed vocabulary blocked it, let the model
    classify those values (closed enum, ``unknown`` stays blocking) and rebuild once.

    The rebuild must pass ``verify_facts``; otherwise the original build is kept. ``verify``
    (model-mapped facts) applies to whatever is returned.
    """
    args = (context, output_dir, cache_dir, mapping, builder, started)
    result = _store(*args, provenance=provenance)
    values = []
    if propose is not None and (result.summary.get("issue_counts") or {}).get("status_ambiguous"):
        values = _unknown_statuses(result.path)
    aliases = {}
    if values and len(values) <= MAX_STATUS_VALUES:
        try:
            aliases = request_status_aliases(
                context.catalog["business_id"], values, propose, cache_dir
            )
        except ContractError:
            aliases = {}
    if aliases:
        end_hint = context.catalog["coverage"].get("available_end_hint")
        end_day = parse_date(end_hint).isoformat() if end_hint else None
        shutil.rmtree(output_dir)  # this call's own blocked build
        aliased = _store(
            *args,
            provenance={**(provenance or {"mapping_source": "readme"}), "status_aliases": aliases},
            status_aliases=aliases,
        )
        check = verify or (lambda conn, summary: verify_facts(conn, summary, end_day))
        try:
            return _verified(aliased, output_dir, check)
        except ContractError:
            result = _store(*args, provenance=provenance)  # cached original build
    return _verified(result, output_dir, verify)


def _verified(result: PreparedBusiness, output_dir: Path, verify) -> PreparedBusiness:
    """Run a model-path check on finished facts; a rejected build is removed, not kept."""
    if verify is None:
        return result
    with closing(result.connect()) as connection:
        errors = verify(connection, result.summary)
    if errors:
        shutil.rmtree(output_dir)
        raise ContractError("model mapping rejected: " + "; ".join(errors))
    return result


def _store(
    context: BusinessContext,
    output_dir: Path,
    cache_dir: Path | None,
    mapping,
    builder,
    started: float,
    *,
    provenance: dict | None = None,
    status_aliases: dict[str, str] | None = None,
) -> PreparedBusiness:
    if output_dir.exists():
        raise ContractError("prepared output already exists; use a fresh directory")
    dependencies = {
        "version": VERSION,
        "source": context.catalog["dependencies"],
        "rule_parser_sha256": context.rules.parser_sha256 if context.rules else None,
        "normalizer_sha256": fingerprint(
            {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
        ),
    }
    if provenance is not None:
        dependencies["mapping_source"] = provenance["mapping_source"]
        if "proposal_sha256" in provenance:
            dependencies["proposal_sha256"] = provenance["proposal_sha256"]
    if status_aliases:
        dependencies["status_aliases"] = status_aliases
    cache_entry = cache_dir / "prepared" / fingerprint(dependencies) if cache_dir else None
    if cache_entry is not None:
        require_output(cache_entry, context.root)
        cached = _cached(cache_entry, dependencies)
        if cached:
            source, summary = cached
            output_dir.mkdir(parents=True)
            destination = output_dir / "facts.sqlite"
            _copy_atomic(source, destination)
            _assert_revision(context)
            result = PreparedBusiness(destination, summary, True, time.perf_counter() - started)
            context.prepared = result
            return result
    output_dir.mkdir(parents=True)
    destination = output_dir / "facts.sqlite"
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".sqlite", delete=False) as handle:
        temporary = Path(handle.name)
    aliases_token = STATUS_ALIASES.set(status_aliases or {})
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.executescript(SCHEMA)
            summary = {
                "version": VERSION,
                "split": context.catalog["split"],
                "business_id": context.catalog["business_id"],
                "backend": "sqlite",
                "money_storage": "exact_decimal_text",
                **builder(context, connection, mapping),
            }
            if builder is not build_stock:
                try:
                    snapshot_mapping = stock_mapping(context)
                except ContractError:
                    snapshot_mapping = None
                if snapshot_mapping is not None:
                    stock_summary = build_stock(context, connection, snapshot_mapping)
                    summary["snapshot"] = stock_summary["snapshot"]
                    summary["snapshot_preparation"] = stock_summary
                    summary["blocking_metrics"] = sorted(
                        set(summary.get("blocking_metrics", []))
                        | set(stock_summary["blocking_metrics"])
                    )
            summary["extensions"] = build_entities(context, connection, summary, mapping)
            if provenance is not None:
                summary.update(provenance)
            for key, value in [("dependencies", dependencies), ("summary", summary)]:
                connection.execute("INSERT INTO metadata VALUES (?,?)", (key, dumps(value)))
            connection.commit()
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ContractError("prepared database integrity failed")
        _assert_revision(context)
        os.replace(temporary, destination)
    finally:
        STATUS_ALIASES.reset(aliases_token)
        temporary.unlink(missing_ok=True)
    if cache_entry is not None:
        cache_entry.mkdir(parents=True, exist_ok=True)
        _copy_atomic(destination, cache_entry / "facts.sqlite")
        manifest = {
            "complete": True,
            "dependencies": dependencies,
            "facts_sha256": digest(destination),
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=cache_entry, delete=False) as handle:
            manifest_temporary = Path(handle.name)
            try:
                handle.write(dumps(manifest) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(manifest_temporary, cache_entry / "manifest.json")
            finally:
                manifest_temporary.unlink(missing_ok=True)
    result = PreparedBusiness(destination, summary, False, time.perf_counter() - started)
    context.prepared = result
    return result
