"""Create a deterministic allowlisted Docker context without dataset or reports."""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
from pathlib import Path

from src.jsonio import ContractError, write_json

ROOT = Path(__file__).resolve().parents[1]


def build_context(root: Path, output: Path) -> dict:
    root = root.resolve()
    candidates = [root / p for p in ["Dockerfile", ".dockerignore", "predict.sh"]]
    candidates.extend(sorted((root / "src").rglob("*.py")))
    files = []
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ContractError("Docker context output cannot be a symlink")
    with tarfile.open(output, "w") as archive:
        for path in candidates:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ContractError("source symlink is not permitted in Docker context")
            data = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o555 if relative == "predict.sh" else 0o444
            info.mtime = info.uid = info.gid = 0
            archive.addfile(info, io.BytesIO(data))
            files.append(
                {"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            )
    return {
        "files": files,
        "context_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "dataset_included": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_context(ROOT, args.output)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.manifest, manifest)
    print(f"Docker context: {len(manifest['files'])} allowlisted files; dataset excluded")


if __name__ == "__main__":
    main()
