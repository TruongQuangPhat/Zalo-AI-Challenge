"""Business-scoped discovery, structural preparation and reusable raw access."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.data.cache import fingerprint, load_catalog, save_catalog
from src.data.ingestion import READERS, ReaderOptions, SourceTable
from src.data.profiling import ProfileOptions, candidate_relations, profile_table
from src.data.split import Split
from src.jsonio import ContractError

if TYPE_CHECKING:
    from src.data.normalization import PreparedBusiness

VERSION = "source-catalog-v1"
OFFLINE_NAMES = {
    "answers",
    "labels",
    "annotations",
    "pilot_reviews",
    "pilot",
    "train_qa_joined",
    "question_analysis",
    "table_profiles",
    "sqlite_findings",
    "validation_split",
    "schema_catalog",
    "file_inventory",
    "table_inventory",
    "runtime_verification",
}
OFFLINE_DIRECTORIES = {"explore", "eda", "reports", ".artifacts", "annotations", "__pycache__"}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def require_output(path: Path, root: Path) -> None:
    boundary = next((p for p in [root, *root.parents] if p.name == "dataset"), root)
    if path.resolve().is_relative_to(boundary.resolve()):
        raise ContractError("output/cache must be outside the read-only input and dataset")
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ContractError("output/cache path must not contain symlinks")


def _software_hash() -> str:
    directory = Path(__file__).parent
    return fingerprint(
        {p.relative_to(directory).as_posix(): digest(p) for p in sorted(directory.rglob("*.py"))}
    )


def discover(split: Split, business: str) -> tuple[dict, list[dict]]:
    if business not in split.businesses:
        raise ContractError("unknown business in this split")
    return discover_sources(split.root, business)


def discover_sources(root: Path, business: str) -> tuple[dict, list[dict]]:
    """Hash permitted business sources without opening adjacent evaluation metadata."""
    directory = root / "data" / business
    if any(
        p.is_symlink() for p in [directory, *directory.parents] if p.is_relative_to(root)
    ) or not directory.resolve().is_relative_to(root):
        raise ContractError("business source directory must not be a symlink or escape split")
    sources, excluded = {}, []

    def visit(folder: Path):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root).as_posix()
            if path.stem.casefold() in OFFLINE_NAMES or path.name.casefold() in OFFLINE_DIRECTORIES:
                excluded.append({"file": relative, "reason": "offline_metadata"})
                continue
            if path.is_symlink():
                raise ContractError(f"source symlink is forbidden: {relative}")
            if path.is_dir():
                visit(path)
            elif path.is_file():
                if path.suffix.casefold() in READERS or path == directory / "README.md":
                    sources[relative] = {"sha256": digest(path), "bytes": path.stat().st_size}
                else:
                    excluded.append({"file": relative, "reason": "not_a_supported_source_format"})

    visit(directory)
    if f"data/{business}/README.md" not in sources:
        raise ContractError("business README missing")
    if not any(Path(p).suffix.casefold() in READERS for p in sources):
        raise ContractError("business has no permitted table sources")
    return sources, excluded


@dataclass
class BusinessContext:
    root: Path
    catalog: dict[str, Any]
    reader_options: ReaderOptions
    cache_hit: bool = False
    elapsed_seconds: float = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    prepared: PreparedBusiness | None = None

    def source_table(self, source_id: str) -> SourceTable:
        """Reopen a revision-checked source; its rows remain raw and unfiltered."""
        profile = next((t for t in self.catalog["tables"] if t["source_id"] == source_id), None)
        if profile is None or profile["read_status"] not in {"read", "empty"}:
            raise ContractError("unknown or failed raw table")
        ref = profile["source_ref"]
        path = self.root / ref["file"]
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(self.root)
            or digest(path) != ref["sha256"]
        ):
            raise ContractError("raw source changed or escapes context")
        tables = READERS[path.suffix.casefold()](
            self.root, path, ref["business_id"], ref["sha256"], self.reader_options
        )
        return next(t for t in tables if t.source_id == source_id)

    def iter_rows(self, source_id: str):
        """Stream all source rows, including headers, blanks and export copies."""
        yield from self.source_table(source_id).iter_rows()


def prepare_business(
    split: Split,
    business: str,
    *,
    cache_dir: Path | None = None,
    reader_options: ReaderOptions | None = None,
    profile_options: ProfileOptions | None = None,
) -> BusinessContext:
    started = time.perf_counter()
    reader_options = reader_options or ReaderOptions()
    profile_options = profile_options or ProfileOptions()
    if cache_dir is not None:
        require_output(cache_dir, split.root)
    sources, excluded = discover(split, business)
    authority = {name: digest(split.root / name) for name in ["rulebook.md", "calendar.csv"]}
    dependencies = {
        "version": VERSION,
        "split": split.root.name,
        "business_id": business,
        "sources": sources,
        "authority": authority,
        "excluded": excluded,
        "reader_options": asdict(reader_options),
        "profile_options": asdict(profile_options),
        "software_sha256": _software_hash(),
    }
    if cache_dir is not None:
        cached = load_catalog(cache_dir, dependencies)
        if cached is not None:
            return BusinessContext(
                split.root, cached, reader_options, True, time.perf_counter() - started
            )
    readme = (split.root / "data" / business / "README.md").read_text(encoding="utf-8-sig")
    declared_files = sorted(
        set(re.findall(r"`([^`\n]+\.(?:csv|xlsx|sqlite))`", readme, flags=re.I))
    )
    actual_names = {Path(p).name for p in sources if Path(p).suffix.casefold() in READERS}
    actual_paths = {
        str(Path(p).relative_to(f"data/{business}"))
        for p in sources
        if Path(p).suffix.casefold() in READERS
    }
    tables, files, issue_records, key_values = [], [], [], {}
    retained_bytes = 0
    for relative, source in sorted(sources.items()):
        path = split.root / relative
        if path.suffix.casefold() not in READERS:
            continue
        file_record = {"file": relative, **source, "read_status": "read", "tables": []}
        try:
            descriptors = READERS[path.suffix.casefold()](
                split.root, path, business, source["sha256"], reader_options
            )
        except Exception as exc:
            file_record["read_status"] = "failed"
            issue_records.append(
                {
                    "code": "file_read_failed",
                    "file": relative,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            files.append(file_record)
            continue
        for descriptor in descriptors:
            file_record["tables"].append(descriptor.source_id)
            if descriptor.metadata.get("exclusion_reason"):
                tables.append(
                    {
                        "source_id": descriptor.source_id,
                        "source_ref": descriptor.source_ref,
                        "format": descriptor.format,
                        "read_status": "not_read",
                        "structure_status": "unsupported",
                        "metadata": descriptor.metadata,
                        "columns": [],
                        "key_candidates": [],
                        "raw_row_count": 0,
                    }
                )
                continue
            try:
                # Retained relationship evidence shares the same total budget across a business.
                profile, values = profile_table(descriptor, profile_options)
                tables.append(profile)
                size = sum(
                    len(str(k[1]).encode()) + 200 for counts in values.values() for k in counts
                )
                if retained_bytes + size <= profile_options.max_counter_bytes // 2:
                    key_values[descriptor.source_id] = values
                    retained_bytes += size
                elif values:
                    issue_records.append(
                        {
                            "code": "relationship_evidence_budget_exhausted",
                            "source_ref": descriptor.source_ref,
                            "status": "not_checked",
                        }
                    )
            except Exception as exc:
                file_record["read_status"] = "failed"
                tables.append(
                    {
                        "source_id": descriptor.source_id,
                        "source_ref": descriptor.source_ref,
                        "format": descriptor.format,
                        "read_status": "failed",
                        "structure_status": "unsupported",
                        "metadata": descriptor.metadata,
                        "columns": [],
                        "key_candidates": [],
                        "error": f"{type(exc).__name__}: {exc}",
                        "raw_row_count": None,
                    }
                )
                issue_records.append(
                    {
                        "code": "table_read_failed",
                        "source_ref": descriptor.source_ref,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        if not descriptors or all(
            t["read_status"] == "empty" for t in tables if t["source_id"] in file_record["tables"]
        ):
            file_record["read_status"] = "empty"
        files.append(file_record)
    after, after_excluded = discover(split, business)
    if (
        after != sources
        or after_excluded != excluded
        or authority != {n: digest(split.root / n) for n in authority}
    ):
        raise ContractError("source/authority changed during preparation")
    relations = candidate_relations(tables, key_values)
    end_hint = re.search(r"Dữ liệu có đến hết (\d{1,2}/\d{1,2}/\d{4})", readme)
    catalog = {
        "version": VERSION,
        "split": split.root.name,
        "business_id": business,
        "dependencies": dependencies,
        "readme": readme,
        "rulebook_hash": authority["rulebook.md"],
        "calendar_hash": authority["calendar.csv"],
        "readme_hash": sources[f"data/{business}/README.md"]["sha256"],
        "files": files,
        "excluded_files": excluded,
        "tables": tables,
        "relationships": relations,
        "relationship_search": {
            "limit": 64,
            "limit_reached": len(relations) == 64,
            "scope": "bounded raw single-column IDs; composites/semantic links not established",
        },
        "readme_inventory": {
            "declared_files": declared_files,
            "declared_missing": [p for p in declared_files if p not in actual_names | actual_paths],
            "actual_not_named": sorted(actual_names - {Path(p).name for p in declared_files}),
        },
        "coverage": {
            "observed_span": None,
            "available_span": None,
            "available_end_hint": end_hint[1] if end_hint else None,
            "availability_basis": ["README end hint only; start/completeness unknown"]
            if end_hint
            else [],
            "snapshot_at": None,
            "history_available": None,
            "parse_status": "not_checked",
        },
        "capabilities": [],
        "issues": issue_records,
        "read_complete": all(f["read_status"] != "failed" for f in files),
        "business_semantics": "not_implemented",
    }
    if cache_dir is not None and catalog["read_complete"]:
        save_catalog(cache_dir, dependencies, catalog)
    return BusinessContext(
        split.root,
        catalog,
        reader_options,
        False,
        time.perf_counter() - started,
        {"retained_key_bytes_accounted": retained_bytes},
    )
