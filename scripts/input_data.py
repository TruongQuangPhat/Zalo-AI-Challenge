"""Shared raw-input allowlist for host-side staging, never labels or cached answers."""

from pathlib import Path

from src.jsonio import ContractError


def input_files(root: Path) -> dict[str, Path]:
    files = {name: root / name for name in ("questions.jsonl", "rulebook.md", "calendar.csv")}
    for path in sorted((root / "data").rglob("*")):
        if path.is_symlink():
            raise ContractError("input source symlink is forbidden")
        if path.is_file():
            if path.suffix.casefold() not in {".md", ".csv", ".xlsx", ".sqlite", ".db"}:
                raise ContractError(f"unexpected raw data file: {path.name}")
            files[path.relative_to(root).as_posix()] = path
    if any(p.is_symlink() or not p.is_file() for p in files.values()):
        raise ContractError("missing or symlinked inference input")
    return files
