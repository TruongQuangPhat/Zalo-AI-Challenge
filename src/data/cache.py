"""Checksum-validated, atomically published catalogs; no cached query answers."""

from __future__ import annotations

import hashlib
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from src.jsonio import ContractError, dumps, loads


def fingerprint(value: dict) -> str:
    def ordered(item):
        if isinstance(item, float):
            return Decimal(str(item))
        if isinstance(item, dict):
            return {key: ordered(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [ordered(value) for value in item]
        return item

    return hashlib.sha256(dumps(ordered(value)).encode()).hexdigest()


def load_catalog(directory: Path, dependencies: dict) -> dict | None:
    entry = directory / fingerprint(dependencies)
    try:
        if entry.is_symlink() or not entry.is_dir():
            return None
        manifest_path, catalog_path = entry / "manifest.json", entry / "catalog.json"
        if manifest_path.is_symlink() or catalog_path.is_symlink():
            return None
        manifest = loads(manifest_path.read_text())
        content = catalog_path.read_bytes()
        if manifest != {
            "complete": True,
            "dependencies": dependencies,
            "catalog_sha256": hashlib.sha256(content).hexdigest(),
        }:
            return None
        catalog = loads(content.decode())
        return catalog if isinstance(catalog, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def save_catalog(directory: Path, dependencies: dict, catalog: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ContractError("cache directory must not be a symlink")
    entry = directory / fingerprint(dependencies)
    if entry.exists() or entry.is_symlink():
        # A damaged entry remains untrusted. Replace individual files atomically;
        # readers accept it only after the complete checksummed manifest is published.
        if entry.is_symlink() or not entry.is_dir():
            raise ContractError("invalid cache entry path")
    else:
        entry.mkdir()
    content = (dumps(catalog) + "\n").encode()
    manifest = {
        "complete": True,
        "dependencies": dependencies,
        "catalog_sha256": hashlib.sha256(content).hexdigest(),
    }
    for name, data in [
        ("catalog.json", content),
        ("manifest.json", (dumps(manifest) + "\n").encode()),
    ]:
        with tempfile.NamedTemporaryFile(dir=entry, prefix=".writing-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, entry / name)
        finally:
            temporary.unlink(missing_ok=True)
