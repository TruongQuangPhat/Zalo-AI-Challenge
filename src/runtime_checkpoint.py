"""Identity and immutable-prefix validation for the existing inference entrypoint."""

from __future__ import annotations

import fcntl
import hashlib
import os
import platform
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.contracts import validate_answer
from src.data.cache import fingerprint
from src.data.catalog import digest, discover_sources
from src.data.split import Question, Split
from src.jsonio import ContractError, dumps, loads

CHECKPOINT_VERSION = "runtime-checkpoint-v1"


def atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ContractError("refusing symlink checkpoint")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".checkpoint-", delete=False) as f:
            temporary = Path(f.name)
            f.write((dumps(value) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def run_lock(folder: Path, *, recovering: bool = False) -> Iterator[None]:
    path = folder / "run.lock"
    if path.is_symlink():
        raise ContractError("symlink run lock")
    try:
        handle = path.open("rb" if recovering else "xb")
    except FileNotFoundError as exc:
        raise ContractError("missing checkpoint lock/source identity proof") from exc
    with handle:
        try:
            fcntl.flock(
                handle.fileno(), (fcntl.LOCK_SH if recovering else fcntl.LOCK_EX) | fcntl.LOCK_NB
            )
        except BlockingIOError as exc:
            raise ContractError("resume parent is still running; stop it before recovery") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def log_name(index: int, qid: str) -> str:
    return f"{index:06d}_{hashlib.sha256(qid.encode()).hexdigest()[:16]}.json"


def run_identity(split: Split, questions: tuple[Question, ...], adapter: Any) -> dict:
    root = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "src").rglob("*.py")),
        root / "predict.sh",
        root / "pyproject.toml",
        root / "uv.lock",
    ]
    metadata = getattr(adapter, "metadata", None)
    if metadata:
        metadata = dict(metadata)
        if isinstance(metadata.get("model"), dict):
            metadata["model"] = {
                k: v
                for k, v in metadata["model"].items()
                if k
                not in {
                    "load_seconds",
                    "startup_seconds_including_hash",
                    "peak_rss_mib",
                    "native_device_log",
                    "ready",
                }
            }
    sources = {business: discover_sources(split.root, business)[0] for business in split.businesses}
    dependencies = {
        "version": CHECKPOINT_VERSION,
        "source": {p.relative_to(root).as_posix(): digest(p) for p in paths},
        "inputs": {**split.source_hashes, "business_sources": sources},
        "question_order": [q.id for q in questions],
        "adapter": {
            "name": adapter.name,
            "class": f"{type(adapter).__module__}.{type(adapter).__qualname__}",
            "metadata": metadata,
        },
        "split_namespace": split.root.name,
        "python": platform.python_version(),
        "sqlite": __import__("sqlite3").sqlite_version,
    }
    return {
        "sha256": fingerprint(dependencies),
        "dependencies": dependencies,
        "resumable": bool((metadata or {}).get("model", {}).get("sha256")),
    }


def record_chain(previous: str, answer: dict, record: dict) -> str:
    # Strict JSON reload canonicalizes binary-float diagnostic timings to Decimal,
    # including exponent spelling. Money/answer values already use exact Decimal.
    return fingerprint(
        {"previous": previous, "answer": loads(dumps(answer)), "record": loads(dumps(record))}
    )


def read_prefix(folder: Path, identity: dict, questions, *, tail_policy: str) -> tuple[list, dict]:
    if any(p.is_symlink() for p in [folder, *folder.parents]):
        raise ContractError("resume path must not contain symlinks")
    with run_lock(folder, recovering=True):
        return _read_prefix(folder, identity, questions, tail_policy=tail_policy)


def _read_prefix(folder: Path, identity: dict, questions, *, tail_policy: str) -> tuple[list, dict]:
    """Read complete newline-terminated records; never modify the interrupted run."""
    if tail_policy not in {"reject", "discard"}:
        raise ContractError("resume tail policy must be reject or discard")
    if any(p.is_symlink() for p in [folder, *folder.parents]):
        raise ContractError("resume path must not contain symlinks")
    if any(p.is_symlink() for p in folder.rglob("*")):
        raise ContractError("resume artifacts must not contain symlinks")
    manifest = loads((folder / "run_manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise ContractError("resume manifest must be a JSON object")
    if not identity["resumable"] or manifest.get("identity") != identity:
        raise ContractError("resume identity mismatch or missing source/model/input/config proof")
    if manifest.get("status") not in {
        "running",
        "interrupted",
        "completed",
        "completed_with_errors",
    }:
        raise ContractError("unsupported resume manifest status")
    submission = folder / "submission.jsonl"
    content = submission.read_bytes()
    lines = content.splitlines(keepends=True)
    tail = b""
    if lines and not lines[-1].endswith(b"\n"):
        if tail_policy != "discard":
            raise ContractError(
                "unterminated submission tail; explicit --resume-tail discard required"
            )
        tail = lines.pop()
    if len(lines) > len(questions):
        raise ContractError("resume has too many records")
    committed = manifest.get("questions_written")
    if type(committed) is not int or not 0 <= committed <= len(lines):
        raise ContractError("checkpoint claims records absent from submission")
    chain = ""
    records, log_hashes = [], {}
    for index, line in enumerate(lines):
        try:
            answer = loads(line.decode("utf-8"))
            if not isinstance(answer, dict):
                raise ContractError("complete resume record must be a JSON object")
            validate_answer(answer)
        except (ValueError, UnicodeError) as exc:
            raise ContractError(f"malformed complete resume record {index}") from exc
        q = questions[index]
        if answer["id"] != q.id:
            raise ContractError("resume IDs duplicate, unknown or out of input order")
        name = log_name(index, q.id)
        record = loads((folder / "logs" / name).read_text())
        if not isinstance(record, dict):
            raise ContractError("resume log must be a JSON object")
        if (
            record.get("id") != q.id
            or record.get("business_id") != q.business_id
            or record.get("as_of_date") != q.as_of_date
            or record.get("identity") != identity["sha256"]
            or record.get("answer") != answer
        ):
            raise ContractError("resume log/prediction/identity mismatch")
        chain = record_chain(chain, answer, record)
        if index + 1 == committed and chain != manifest.get("committed_record_chain"):
            raise ContractError("checkpoint record checksum mismatch")
        if record.get("catalog") != f"catalogs/{q.business_id}.json":
            raise ContractError("resume catalog path mismatch")
        records.append((answer, record, name))
        log_hashes[name] = digest(folder / "logs" / name)
    expected = set(log_hashes)
    if len(records) < len(questions):
        expected.add(log_name(len(records), questions[len(records)].id))
    if set(p.name for p in (folder / "logs").iterdir()) - expected:
        raise ContractError("unexpected resume logs")
    if manifest["status"].startswith("completed") and (
        tail or committed != len(records) or len(records) != len(questions)
    ):
        raise ContractError("completed resume is incomplete")
    return records, {
        "parent": str(folder.resolve()),
        "parent_status": manifest["status"],
        "submission_sha256": digest(submission),
        "logs_sha256": log_hashes,
        "records_recovered": len(records),
        "stale_checkpoint_records": len(records) - committed,
        "tail_policy": tail_policy,
        "discarded_tail_bytes": len(tail),
        "discarded_tail_sha256": hashlib.sha256(tail).hexdigest() if tail else None,
        "parent_manifest_sha256": digest(folder / "run_manifest.json"),
    }
