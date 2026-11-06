"""Audit released authority only; never load questions, labels or inference runs."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from src.data.catalog import digest, discover_sources, require_output
from src.jsonio import dumps
from src.semantics.rulebook import parse_rulebook


def audit(root: Path, *, include_sources: bool = False) -> dict:
    """A unit is one table row or nonempty paragraph/bullet, excluding headings.

    README units are descriptive source declarations. Their presence does not
    certify the mapper supports them or that all declared facts are available.
    """
    root = root.resolve()
    raw = (root / "rulebook.md").read_bytes()
    rules = parse_rulebook(
        raw.decode("utf-8-sig"), root.name, source_sha256=hashlib.sha256(raw).hexdigest()
    )
    readmes, sources = {}, {}
    for path in sorted((root / "data").glob("*/README.md")):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8-sig")
        section = ""
        units = []
        for line, quote in enumerate(text.splitlines(), 1):
            if quote.startswith("#"):
                section = quote.lstrip("# ")
            elif quote.strip():
                units.append(
                    {"line": line, "section": section, "quote": quote, "kind": "descriptive"}
                )
        readmes[relative] = {"sha256": digest(path), "units": units}
        if include_sources:
            permitted, _ = discover_sources(root, path.parent.name)
            sources.update(permitted)
    return {
        "split": root.name,
        "rulebook_sha256": rules.sha256,
        "parser_sha256": rules.parser_sha256,
        "calendar_sha256": digest(root / "calendar.csv"),
        "clauses": rules.clauses,
        "rules": rules.summary()["rules"],
        "readmes": readmes,
        "sources": sources,
        "limits": "Document inventory/understanding only; no capability or numeric accuracy claim.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dirs", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-sources", action="store_true")
    args = parser.parse_args()
    for root in args.data_dirs:
        require_output(args.output, root)
    result = {
        root.name: audit(root, include_sources=args.include_sources) for root in args.data_dirs
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps(result) + "\n")
    unresolved = [
        (split, clause["line"])
        for split, report in result.items()
        for clause in report["clauses"]
        if not clause["understood"]
    ]
    for split, report in result.items():
        print(f"{split}: {len(report['clauses'])} rulebook units; {len(report['readmes'])} READMEs")
    if unresolved:
        print(f"Unrecognized authority units: {unresolved}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
