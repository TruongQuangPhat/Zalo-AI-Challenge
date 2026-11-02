"""Verify frozen source readers on train; all working catalogs/cache are temporary."""

from __future__ import annotations

import argparse
import csv
import json
import os
import resource
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scripts.verify_runtime import check_quality
from src.data.cache import fingerprint
from src.data.catalog import digest, prepare_business, require_output
from src.data.split import load_split
from src.environment import require_project_environment
from src.jsonio import ContractError, dumps, write_json

ROOT = Path(__file__).resolve().parents[1]


def code_fingerprint() -> str:
    files = [*ROOT.joinpath("src").rglob("*.py"), Path(__file__)]
    return fingerprint({str(p.relative_to(ROOT)): digest(p) for p in sorted(files)})


def verify(scope: str, work: Path) -> dict:
    split = load_split(ROOT / "dataset/train")
    manifest = json.loads((ROOT / "config/validation_split.json").read_text())
    dev = set.intersection(
        *(
            set(protocol["partitions"]["dev"]["businesses"])
            for protocol in manifest["protocols"].values()
        )
    )
    businesses = sorted(dev if scope == "dev" else split.businesses)
    sources = {
        name: digest(split.root / name)
        for name in manifest["source_files"]
        if name in {"rulebook.md", "calendar.csv", "questions.jsonl"}
        or (name.startswith("data/") and Path(name).parts[1] in businesses)
    }
    if any(value != manifest["source_files"][name]["sha256"] for name, value in sources.items()):
        raise ContractError("source drift from frozen train manifest")
    with (ROOT / "dataset/explore/table_inventory.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        expected_tables = {
            (row["path"].removeprefix("train/"), row["table"])
            for row in csv.DictReader(handle)
            if row["split"] == "train" and row["business_id"] in businesses
        }
    frozen_code = code_fingerprint()
    results, all_tables = {}, set()
    began = time.perf_counter()
    for business in businesses:
        context = prepare_business(split, business, cache_dir=work / "cache")
        catalog = context.catalog
        if not catalog["read_complete"]:
            raise ContractError(f"read failure in {business}: {dumps(catalog['issues'])}")
        first_digest = fingerprint(catalog)
        first_time = context.elapsed_seconds
        second = prepare_business(split, business)
        if fingerprint(second.catalog) != first_digest:
            raise ContractError(f"cold catalog changed: {business}")
        warm = prepare_business(split, business, cache_dir=work / "cache")
        if not warm.cache_hit or fingerprint(warm.catalog) != first_digest:
            raise ContractError(f"cache mismatch: {business}")
        table_records = []
        for table in catalog["tables"]:
            if table["read_status"] == "not_read":
                continue
            all_tables.add((table["source_ref"]["file"], table["source_ref"]["table"]))
            if sum(table["row_accounting"].values()) != table["raw_row_count"]:
                raise ContractError("raw row accounting differs")
            for column in table["columns"]:
                if column["row_count"] != column["null_count"] + column["non_null_count"]:
                    raise ContractError("column null accounting differs")
            table_records.append(table)
        # Independent observations were frozen while clarifying the requirements.
        observed_csv = {
            "data/biz_101/ban_hang.csv": 56502,
            "data/biz_110/danh_sach_hoa_don.csv": 29318,
            "data/biz_111/don_hang.csv": 66470,
            "data/biz_111/chi_tiet_don.csv": 106586,
            "data/biz_111/thanh_toan.csv": 66196,
        }
        for table in table_records:
            expected = observed_csv.get(table["source_ref"]["file"])
            if expected is not None and table["raw_row_count"] != expected:
                raise ContractError("raw count differs from independent source observation")
        issue_counts = Counter()
        for table in table_records:
            issue_counts.update(table["issue_counts"])
        results[business] = {
            "files": len(catalog["files"]),
            "tables": len(table_records),
            "raw_rows": sum(t["raw_row_count"] for t in table_records),
            "ambiguous_structures": sum(t["structure_status"] != "resolved" for t in table_records),
            "partial_distinct_profiles": sum(
                t["statistics_status"] != "exact" for t in table_records
            ),
            "issue_counts": dict(sorted(issue_counts.items())),
            "relationship_diagnostics": len(catalog["issues"]),
            "catalog_sha256": first_digest,
            "cold_seconds": round(first_time, 3),
            "repeat_cold_seconds": round(second.elapsed_seconds, 3),
            "warm_seconds": round(warm.elapsed_seconds, 3),
            "cold_repeat_and_cache": "passed",
        }
        print(
            dumps(
                {
                    "business": business,
                    **{k: results[business][k] for k in ["tables", "raw_rows", "cold_seconds"]},
                }
            ),
            flush=True,
        )
    if all_tables != expected_tables:
        raise ContractError(
            f"table inventory differs: missing={sorted(expected_tables - all_tables)}, "
            f"added={sorted(all_tables - expected_tables)}"
        )
    if code_fingerprint() != frozen_code:
        raise ContractError("source code changed during frozen verification")
    if any(digest(split.root / name) != value for name, value in sources.items()):
        raise ContractError("source inputs changed during verification")
    return {
        "scope": scope,
        "code_sha256": frozen_code,
        "source_fingerprint": fingerprint(sources),
        "source_files_unchanged": len(sources),
        "businesses": results,
        "tables": len(all_tables),
        "raw_rows": sum(r["raw_rows"] for r in results.values()),
        "seconds": round(time.perf_counter() - began, 3),
        "process_peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "limitations": [
            "No normalization, metric execution, business accuracy or LLM acceptance.",
            "Date coverage stays unknown until shared parsers and availability evidence exist.",
            "Key/relationship statistics are bounded; partial checks never prove completeness.",
            "All-train mode is frozen technical acceptance, not a model/PA holdout evaluation.",
            "Labels and public/private sources are outside this verification.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["dev", "all-train"], default="dev")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/ingestion_verification.json")
    args = parser.parse_args()
    require_project_environment()
    require_output(args.report, ROOT / "dataset/train")
    os.environ.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    report = {"recorded_at": datetime.now(UTC).isoformat(), "verified": False}
    with tempfile.TemporaryDirectory(prefix="datamate-ingestion-") as temporary:
        try:
            report["quality"] = check_quality(Path(temporary))
            report["sources"] = verify(args.scope, Path(temporary))
            report["verified"] = True
        except (ContractError, OSError, ValueError) as exc:
            report["error"] = str(exc)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.report, report)
    print(dumps({"verified": report["verified"], "report": str(args.report)}))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
