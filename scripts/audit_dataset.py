"""Train-only structural inventory and frozen evaluation membership; no label parsing."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from src.data.split import Split, load_split
from src.jsonio import ContractError

PARTITIONS = {"dev", "validation", "final_holdout"}
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def save_json(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise ContractError(f"symlink output: {path}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def inventory(root: Path) -> dict[str, Any]:
    """Hash source bytes and list tables; never scan table rows or parse labels."""
    root = root.resolve()
    files, tables = {}, []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"source symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = {"bytes": path.stat().st_size, "sha256": digest(path)}
        names = []
        if path.suffix == ".csv":
            names = ["csv"]
        elif path.suffix == ".xlsx":
            with zipfile.ZipFile(path) as archive:
                workbook = ET.fromstring(archive.read("xl/workbook.xml"))
                names = [
                    sheet.attrib["name"]
                    for sheet in workbook.findall(NS + "sheets/" + NS + "sheet")
                ]
        elif path.suffix == ".sqlite":
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                names = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ]
        tables.extend({"path": relative, "table": name} for name in names)
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "files": files,
        "tables": tables,
        "source_fingerprint": hashlib.sha256(canonical).hexdigest(),
        "method": "sha256 bytes + workbook.xml sheet names + sqlite_master; no row profiling",
    }


def compare_inventory(current: dict, eda_dir: Path, split_name: str) -> dict:
    def rows(name: str) -> list[dict]:
        with (eda_dir / name).open(encoding="utf-8-sig", newline="") as handle:
            return [r for r in csv.DictReader(handle) if r["split"] == split_name]

    def relative(value: str) -> str:
        prefix = split_name + "/"
        if not value.startswith(prefix):
            raise ContractError("inventory split/path mismatch")
        return value[len(prefix) :]

    old_files = {
        relative(r["path"]): {"bytes": int(r["bytes"]), "sha256": r["sha256"]}
        for r in rows("file_inventory.csv")
    }
    old_tables = {(relative(r["path"]), r["table"]) for r in rows("table_inventory.csv")}
    new_files = current["files"]
    new_tables = {(r["path"], r["table"]) for r in current["tables"]}
    diff = {
        "added_files": sorted(new_files.keys() - old_files.keys()),
        "missing_files": sorted(old_files.keys() - new_files.keys()),
        "changed_files": sorted(
            key for key in new_files.keys() & old_files.keys() if new_files[key] != old_files[key]
        ),
        "added_tables": sorted(new_tables - old_tables),
        "missing_tables": sorted(old_tables - new_tables),
    }
    return {"matches": not any(diff.values()), **diff}


def _membership(protocol: dict, split: Split, groups: dict, *, structure: bool) -> dict:
    parts = protocol["partitions"]
    if set(parts) != PARTITIONS:
        raise ContractError("protocol needs dev/validation/final_holdout")
    by_business = {business: [] for business in split.businesses}
    for question in split.questions:
        by_business[question.business_id].append(question.id)
    seen_businesses, seen_questions = set(), set()
    group_partitions: dict[str, set[str]] = {}
    summary = {}
    for name, part in parts.items():
        businesses, ids = part["businesses"], part["question_ids"]
        if not businesses or len(set(businesses)) != len(businesses):
            raise ContractError("empty/duplicate business membership")
        if seen_businesses.intersection(businesses):
            raise ContractError("business leakage across partitions")
        if not set(businesses).issubset(by_business):
            raise ContractError("unknown business")
        expected = {qid for business in businesses for qid in by_business[business]}
        if set(ids) != expected or len(ids) != len(expected):
            raise ContractError("question membership mismatch/duplicates")
        if seen_questions.intersection(ids):
            raise ContractError("question leakage across partitions")
        seen_businesses.update(businesses)
        seen_questions.update(ids)
        for business in businesses:
            group = groups[business]
            group_partitions.setdefault(group, set()).add(name)
        summary[name] = {"businesses": len(businesses), "questions": len(ids)}
    if seen_businesses != set(split.businesses):
        raise ContractError("unassigned businesses")
    if structure and any(len(parts) != 1 for parts in group_partitions.values()):
        raise ContractError("structure leakage across partitions")
    return summary


def validate_manifest(manifest: dict, split: Split, current: dict) -> dict:
    if manifest.get("version") != "validation-split-v1":
        raise ContractError("unknown manifest version")
    if manifest.get("source_files") != current["files"]:
        raise ContractError("source fingerprints changed; review frozen split")
    if manifest.get("source_fingerprint") != current["source_fingerprint"]:
        raise ContractError("aggregate fingerprint mismatch")
    if type(manifest.get("seed")) is not int or not manifest.get("selection_rule"):
        raise ContractError("missing seed/selection rule")
    groups = manifest["business_groups"]
    if set(groups) != set(split.businesses):
        raise ContractError("group membership incomplete")
    if not all(group in manifest["group_definitions"] for group in groups.values()):
        raise ContractError("undefined structure group")
    protocols = manifest["protocols"]
    if set(protocols) != {"business", "structure"}:
        raise ContractError("missing evaluation protocol")
    summary = {
        name: _membership(protocol, split, groups, structure=name == "structure")
        for name, protocol in protocols.items()
    }
    for name, protocol in protocols.items():
        dev = set(protocol["partitions"]["dev"]["businesses"])
        if not set(manifest["previous_gold_businesses"]).issubset(dev):
            raise ContractError(f"previous gold business outside dev: {name}")
        dev_ids = set(protocol["partitions"]["dev"]["question_ids"])
        if not set(manifest["previous_gold_question_ids"]).issubset(dev_ids):
            raise ContractError(f"previous gold question outside dev: {name}")
    return summary


def validate_case_specs(specification: dict) -> int:
    if specification.get("version") != "data-acceptance-v1":
        raise ContractError("unknown acceptance version")
    cases = specification["cases"]
    ids = [case["case_id"] for case in cases]
    required = {f"C{i:02d}" for i in range(1, 31)} | {"P1-SUMMARY", "P1-ERROR", "P1-IDENTITY"}
    if set(ids) != required or len(ids) != len(required):
        raise ContractError("missing/duplicate acceptance case")
    for case in cases:
        for key in ["title", "source", "rule_refs", "input", "expected", "capability"]:
            if not case.get(key):
                raise ContractError(f"acceptance case missing {key}")
        if case["priority"] not in {"P0", "P1", "P2"}:
            raise ContractError("unknown acceptance priority")
        if case["implementation_status"] != "planned":
            raise ContractError("spec validator cannot certify runtime acceptance")
    return len(cases)


def freeze_manifest(selection: dict, split: Split, current: dict) -> dict:
    if "source_files" in selection:
        raise ContractError("manifest already frozen; refusing to reselect/overwrite")
    for protocol in selection["protocols"].values():
        for part in protocol["partitions"].values():
            part["question_ids"] = [
                q.id for q in split.questions if q.business_id in part["businesses"]
            ]
    selection["source_files"] = current["files"]
    selection["source_fingerprint"] = current["source_fingerprint"]
    validate_manifest(selection, split, current)
    return selection


def outside_inputs(path: Path, data_dir: Path, eda_dir: Path) -> None:
    resolved = path.resolve()
    # Source root normally dataset/<split>; exclude the whole dataset, including EDA.
    if resolved.is_relative_to(data_dir.resolve().parent) or resolved.is_relative_to(
        eda_dir.resolve()
    ):
        raise ContractError("output must be outside dataset/ and EDA inputs")


def prepare(
    data_dir: Path,
    eda_dir: Path,
    manifest_path: Path,
    output: Path,
    *,
    freeze: bool,
    cases_path: Path,
) -> dict:
    outside_inputs(output, data_dir, eda_dir)
    outside_inputs(manifest_path, data_dir, eda_dir)
    if data_dir.resolve().name != "train":
        raise ContractError("Dataset audit is train-only")
    output.mkdir(parents=True, exist_ok=False)
    split = load_split(data_dir)
    before = inventory(data_dir)
    diff = compare_inventory(before, eda_dir, "train")
    if not diff["matches"]:
        save_json(output / "inventory_diff.json", diff)
        raise ContractError("inventory changed; inspect inventory_diff.json before proceeding")
    manifest = json.loads(manifest_path.read_text())
    if freeze:
        manifest = freeze_manifest(manifest, split, before)
        save_json(manifest_path, manifest)
    summary = validate_manifest(manifest, split, before)
    case_count = validate_case_specs(json.loads(cases_path.read_text()))
    after = inventory(data_dir)
    if before != after:
        raise ContractError("source inputs changed during preparation")
    report = {
        "status": "passed",
        "source_files": len(before["files"]),
        "tables": len(before["tables"]),
        "source_fingerprint": before["source_fingerprint"],
        "inventory_diff": diff,
        "protocols": summary,
        "groups": dict(Counter(manifest["business_groups"].values())),
        "inputs_unchanged": True,
        "acceptance_specs": {"count": case_count, "schema": "passed", "runtime": "planned"},
        "labels": "hashed as opaque bytes; never parsed",
        "scope": "train only; table contents/ETL/model accuracy not certified",
    }
    save_json(output / "inventory.json", before)
    save_json(output / "verification.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--eda-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--freeze", action="store_true", help="one-time freeze of selection manifest"
    )
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/fixtures/data_acceptance_cases.json")
    )
    args = parser.parse_args()
    try:
        result = prepare(
            args.data_dir,
            args.eda_dir,
            args.manifest,
            args.output_dir,
            freeze=args.freeze,
            cases_path=args.cases,
        )
    except (ContractError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Dataset audit: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
