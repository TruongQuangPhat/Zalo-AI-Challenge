"""Safe DataMate packaging; delegates inference to the existing predict.sh and validators."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from scripts.build_context import build_context
from scripts.input_data import input_files
from src.contracts import validate_answer
from src.data.catalog import digest
from src.data.split import load_split
from src.jsonio import ContractError, dumps, loads, read_jsonl, write_json
from src.runtime_checkpoint import log_name

ROOT = Path(__file__).resolve().parents[2]
MAX_TAR_BYTES = 100_000_000_000  # Conservative decimal interpretation of the official 100 GB.
DOCS = ("submission_guide.md", "capability-matrix.md", "data-contracts.md", "rulebook-authority.md")


def now() -> str:
    return datetime.now(UTC).isoformat()


def no_symlinks(path: Path) -> Path:
    if "," in str(path):
        raise ContractError("paths containing commas are unsupported by Docker bind mount syntax")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ContractError(f"symlink path is forbidden: {path}")
    return path.resolve()


def fresh(path: Path, *inputs: Path) -> Path:
    """Never overwrite, delete, or write within inputs or maintained source directories."""
    target = no_symlinks(path)
    protected = [
        ROOT / n
        for n in (
            "dataset",
            "src",
            "scripts",
            "docs",
            "config",
            "tests",
            ".git",
            ".venv",
            "reports",
        )
    ]
    for source in [*protected, *inputs]:
        source = source.resolve()
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise ContractError("output/cache must be disjoint from inputs and maintained source")
    if target.exists():
        raise ContractError(f"output already exists; choose a fresh path: {target}")
    return target


def model_declaration(path: Path) -> dict:
    value = loads(no_symlinks(path).read_text())
    models = value.get("models", [])
    if not models or sum(m["parameter_budget_upper_bound"] for m in models) > 15_000_000_000:
        raise ContractError("declare all inference models within the 15B parameter budget")
    # This packager supports the existing single, directly used pretrained GGUF.
    # A future trained solution must explicitly extend its evidence packaging first.
    if (
        len(models) != 1
        or value.get("fine_tuning") is not False
        or value.get("training_data_generation") is not False
    ):
        raise ContractError("trained/additional models require a reviewed training asset allowlist")
    if value.get("training_samples") != 0 or value.get("validation_samples_for_training") != 0:
        raise ContractError("pretrained-only declaration must have zero training samples")
    for model in models:
        if (
            not 0 < model["parameter_budget_upper_bound"] <= 15_000_000_000
            or not re.fullmatch(r"[0-9a-f]{64}", model["sha256"])
            or model["bytes"] <= 0
            or date.fromisoformat(model["public_release_date"]) >= date(2026, 10, 26)
            or not model["license"]
            or not model["revision"]
            or not model["source"].startswith("https://")
            or not re.fullmatch(r"/opt/model/[A-Za-z0-9_.-]+\.gguf", model["image_path"])
            or ".." in Path(model["image_path"]).parts
        ):
            raise ContractError("incomplete/ineligible model declaration")
    return value


def context(output: Path, declaration: Path, base_image: str) -> dict:
    """Extend the established allowlist, without copying weights or training/test data."""
    declared = model_declaration(declaration)
    output.mkdir(parents=True)
    archive = output / "context.tar"
    existing = build_context(ROOT, archive)
    additions = {
        "README.md": ROOT / "README.md",
        "config/submission_model.json": declaration,
        "docs/model-license.txt": ROOT / "docs/model-license.txt",
        **{f"docs/{name}": ROOT / "docs" / name for name in DOCS},
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True)
    manifest = {
        "schema": "datamate-submission-v1",
        "created_at": now(),
        "base_image": base_image,
        "git_commit": git.stdout.strip() if git.returncode == 0 else None,
        "git_dirty": bool(status.stdout) if status.returncode == 0 else None,
        "models": declared,
        "files": {
            **{
                f"/code/{f['path']}": {"sha256": f["sha256"], "bytes": f["bytes"]}
                for f in existing["files"]
                if f["path"] not in {"Dockerfile", ".dockerignore", "docs/model-license.txt"}
            },
            **{
                f"/code/{name}": {"sha256": digest(no_symlinks(path)), "bytes": path.stat().st_size}
                for name, path in additions.items()
            },
        },
        "training": {
            "performed": False,
            "data_generation": False,
            "files": [],
            "samples": 0,
            "training_only_base_checkpoints": [],
        },
        "inference_config": {
            "seed": 0,
            "temperature": 0,
            "n_ctx": 4096,
            "n_batch": 128,
            "model_threads": 4,
            "question_timeout": 120,
        },
        "limitations": [
            "Blackwell compatibility and six-hour SLA require reference-host tests",
            "model and license declarations require human review",
            "manifest does not hash itself; archive checksum covers it",
        ],
    }
    license_path = ROOT / "docs/model-license.txt"
    manifest["files"]["/opt/model/LICENSE"] = {
        "sha256": digest(license_path),
        "bytes": license_path.stat().st_size,
    }
    training_readme = (
        b"# Training declaration\n\n"
        b"No team training, fine-tuning or training-data generation was performed. "
        b"Training/validation samples: 0/0. There is no train.sh, optimizer state or "
        b"training checkpoint to reproduce. Published pretrained GGUF is used directly.\n\n"
        b"Fixed model source/revision/release/license/hash: /code/config/submission_model.json. "
        b"All inference assets remain inside the image. Source and dependency locks: /code. "
        b"Reproduce with bash /code/predict.sh on read-only /data and fresh writable /result.\n\n"
        b"A trained successor must package actual code/data/configs/logs/checkpoints and "
        b"update the declaration; this packager rejects training assets until reviewed.\n"
    )
    manifest["files"]["/code/training_code/README.md"] = {
        "sha256": hashlib.sha256(training_readme).hexdigest(),
        "bytes": len(training_readme),
    }
    training = (dumps(manifest["training"]) + "\n").encode()
    manifest["files"]["/code/training_code/data_manifest.json"] = {
        "sha256": hashlib.sha256(training).hexdigest(),
        "bytes": len(training),
    }
    with tarfile.open(archive, "a") as bundle:
        for name, path in sorted(additions.items()):
            content = path.read_bytes()
            add_bytes(bundle, "submission/" + name, content, executable=name == "predict.sh")
        add_bytes(bundle, "submission/training_code/README.md", training_readme)
        add_bytes(bundle, "submission/training_code/data_manifest.json", training)
        add_bytes(bundle, "submission/artifact_manifest.json", (dumps(manifest) + "\n").encode())
    write_json(output / "artifact_manifest.json", manifest)
    write_json(
        output / "context_manifest.json",
        {**existing, "context_sha256": digest(archive), "submission_files": manifest["files"]},
    )
    return manifest


def add_bytes(archive: tarfile.TarFile, name: str, content: bytes, *, executable=False) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(content), 0o555 if executable else 0o444
    archive.addfile(info, io.BytesIO(content))


def checked(command: list[str], *, timeout: float = 60, **kwargs) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise ContractError(f"command failed ({result.returncode}): {command[:4]}\n{result.stderr}")
    return result.stdout.strip()


def image_info(image: str) -> dict:
    value = loads(checked(["docker", "image", "inspect", image]))[0]
    if (value["Os"], value["Architecture"]) != ("linux", "amd64"):
        raise ContractError("submission image must be linux/amd64")
    return {"id": value["Id"], "size": value["Size"], "config": value["Config"]}


def inventory(image: str, expected: dict, output: Path) -> dict:
    """No model load: verify packaged bytes, installed versions and unwanted data paths."""
    code = """
import hashlib, importlib.metadata, json, platform, tomllib
from pathlib import Path
m = json.loads(Path('/code/artifact_manifest.json').read_text())
files = dict(m['files'])
for item in m['models']['models']:
    files[item['image_path']] = {'sha256': item['sha256'], 'bytes': item['bytes']}
for name, spec in files.items():
    p = Path(name)
    assert p.is_file() and not p.is_symlink(), name
    assert p.stat().st_size == spec['bytes'], name
    with p.open('rb') as f:
        assert hashlib.file_digest(f, 'sha256').hexdigest() == spec['sha256'], name
assert {str(p) for p in Path('/code/src').rglob('*.py')} == {
    p for p in m['files'] if p.startswith('/code/src/') and p.endswith('.py')}
for root in ['/code', '/opt/model']:
    for p in Path(root).rglob('*'):
        assert p.name not in {'answers.jsonl', 'questions.jsonl', 'facts.sqlite', '.env'}, str(p)
packages = {p.metadata['Name']: p.version for p in importlib.metadata.distributions()}
assert packages.get('llama-cpp-python') == '0.3.16', packages
lock = tomllib.loads(Path('/code/uv.lock').read_text())
locked = {p['name']: p['version'] for p in lock['package']}
for name, version in packages.items():
    canonical = name.lower().replace('_', '-').replace('.', '-')
    if canonical in locked:
        assert version == locked[canonical], (name, version, locked[canonical])
print(json.dumps({'artifact_manifest': m,
                  'python': platform.python_version(), 'packages': packages}))
"""
    result = loads(
        checked(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--entrypoint",
                "python3",
                image,
                "-c",
                code,
            ],
            timeout=300,
        )
    )
    if result["artifact_manifest"] != expected:
        raise ContractError("image manifest differs from the prepared context")
    write_json(output, result)
    return result


def stage(source: Path, destination: Path, *, limit: int | None) -> dict:
    """Copy the existing raw-input allowlist; root labels are never opened or mounted."""
    split = load_split(source)
    questions = split.questions if limit is None else split.questions[:limit]
    ids = [q.id for q in questions]
    businesses = {q.business_id for q in questions}
    files = {
        name: path
        for name, path in input_files(source).items()
        if not name.startswith("data/") or Path(name).parts[1] in businesses
    }
    before = {name: digest(no_symlinks(path)) for name, path in files.items()}
    destination.mkdir(parents=True)
    for name, path in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "questions.jsonl" and limit is not None:
            records = [r for _, r in read_jsonl(path) if r["id"] in set(ids)]
            target.write_text("".join(dumps(r) + "\n" for r in records), encoding="utf-8")
        else:
            shutil.copyfile(path, target)
        target.chmod(0o444)
    if before != {name: digest(path) for name, path in files.items()}:
        raise ContractError("input changed while staging")
    hashes = {name: digest(path) for name, path in input_files(destination).items()}
    if any(hashes[name] != value for name, value in before.items() if name != "questions.jsonl"):
        raise ContractError("staged business/authority bytes differ")
    return {
        "ids": ids,
        "sha256": hashes,
        "source_sha256": before,
        "labels_mounted": False,
        "mode": "full" if limit is None else "smoke",
    }


def validate_run(data: Path, output: Path) -> dict:
    """Existing strict Decimal/typed validator, plus completeness and official log checks."""
    ids = [q.id for q in load_split(data).questions]
    rows = [row for _, row in read_jsonl(output / "submission.jsonl")]
    if [r.get("id") for r in rows] != ids:
        raise ContractError("missing, duplicate, unexpected or reordered output IDs")
    manifest = loads((output / "run_manifest.json").read_text())
    if (
        manifest.get("status") != "completed"
        or manifest.get("question_errors") != 0
        or manifest.get("questions_written") != len(ids)
        or manifest.get("questions_total") != len(ids)
        or manifest.get("adapter") != "local-planner"
    ):
        raise ContractError("incomplete/error/non-LLM run cannot certify submission")
    paths = sorted((output / "logs").glob("*.json"))
    if len(paths) != len(ids):
        raise ContractError("one per-question log is required")
    generation, retries, attempt_errors, question_times = [], 0, Counter(), []
    worker_rss, execution_diagnostics = [], []
    for index, (row, path) in enumerate(zip(rows, paths, strict=True)):
        validate_answer(row)
        record = loads(no_symlinks(path).read_text())
        if (
            path.name != log_name(index, row["id"])
            or record.get("id") != row["id"]
            or record.get("answer") != row
            or record.get("error") is not None
            or record.get("identity") != manifest["identity"]["sha256"]
        ):
            raise ContractError("question log identity/answer/error mismatch")
        elapsed = record.get("elapsed_ms")
        if type(elapsed) is not int or elapsed < 0 or "thinking" not in record:
            raise ContractError("log requires nonnegative integer elapsed_ms and thinking")
        thinking = record["thinking"]
        if (
            thinking is not None
            and not isinstance(thinking, str)
            and not (isinstance(thinking, list) and all(isinstance(s, str) for s in thinking))
        ):
            raise ContractError("invalid thinking trace type")
        if not record.get("code") or "inputs" not in record or "output" not in record:
            raise ContractError("predefined computation must log code, actual inputs and output")
        question_times.append(record["elapsed_seconds"])
        execution_diagnostics.append(record.get("execution"))
        plan = record.get("plan", {})
        attempts = plan.get("planning", plan).get("attempts", [])
        retries += max(0, len(attempts) - 1)
        generation.extend(a["generation_seconds"] for a in attempts if "generation_seconds" in a)
        worker_rss.extend(a["peak_rss_mib"] for a in attempts if "peak_rss_mib" in a)
        attempt_errors.update(a["error"] for a in attempts if a.get("error"))
    if any("local_model_worker" in e or "local_model_timeout" in e for e in attempt_errors):
        raise ContractError(
            "model worker failed during inference; abstains do not certify readiness"
        )
    if any(p.get("read_complete") is False for p in manifest.get("preparation", {}).values()):
        raise ContractError("business source ingestion was incomplete")
    model = (manifest.get("planner_metadata") or {}).get("model", {})
    return {
        "questions": len(ids),
        "types": dict(Counter(r["type"] for r in rows)),
        "submission_sha256": digest(output / "submission.jsonl"),
        "identity": manifest["identity"],
        "model": model,
        "runtime_elapsed_seconds": manifest.get("elapsed_seconds"),
        "question_elapsed_seconds": question_times,
        "generation_seconds": sum(generation),
        "retries": retries,
        "attempt_errors": dict(attempt_errors),
        "timeouts": sum(n for e, n in attempt_errors.items() if "timeout" in e.casefold()),
        "preparation_by_business": manifest.get("preparation"),
        "execution_diagnostics": execution_diagnostics,
        "worker_peak_rss_mib": max([model.get("peak_rss_mib", 0), *worker_rss]),
        "gpu_peak_memory": "NOT_MEASURED; shared host samples are separate",
    }


def monitor(name: str, stop: threading.Event, path: Path, gpu: bool) -> None:
    with path.open("x") as stream:
        while not stop.wait(2):
            sample = {"at": now()}
            for key, command in [
                ("container", ["docker", "stats", "--no-stream", "--format", "{{json .}}", name]),
                (
                    "host_gpu",
                    [
                        "nvidia-smi",
                        "--query-gpu=name,memory.used,utilization.gpu",
                        "--format=csv,noheader,nounits",
                    ]
                    if gpu
                    else [],
                ),
            ]:
                if not command:
                    continue
                try:
                    sample[key] = checked(command, timeout=5)
                except (OSError, subprocess.TimeoutExpired, ContractError) as exc:
                    sample[key] = {"unavailable": str(exc)}
            stream.write(dumps(sample) + "\n")
            stream.flush()


def run_container(image: str, data: Path, output: Path, cache: Path, args) -> dict:
    output.mkdir(parents=True)
    cache.mkdir(parents=True)
    name = "datamate-submission-" + uuid4().hex[:12]
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--shm-size=16g",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        f"/tmp:rw,nosuid,size={args.tmpfs_size}",
        "--mount",
        f"type=bind,source={data},target=/data,readonly",
        "--mount",
        f"type=bind,source={output},target=/result",
        "--mount",
        f"type=bind,source={cache},target=/cache",
        "--env",
        f"DATAMATE_GPU_LAYERS={0 if args.gpus == 'none' else -1}",
        "--env",
        "DATAMATE_CACHE=/cache",
    ]
    if args.gpus != "none":
        command.extend(["--gpus", args.gpus])
    # The scoring path is deliberately exercised without inference CLI arguments.
    command.extend(["--entrypoint", "bash", image, "/code/predict.sh"])
    stop = threading.Event()
    watcher = threading.Thread(
        target=monitor,
        args=(name, stop, output / "resources.jsonl", args.gpus != "none"),
        daemon=True,
    )
    started, exit_status, timed_out = time.monotonic(), None, False
    watcher.start()
    try:
        with (
            (output / "stdout.log").open("x") as stdout,
            (output / "stderr.log").open("x") as stderr,
        ):
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            try:
                exit_status = process.wait(timeout=args.timeout)
            except BaseException as exc:
                timed_out = isinstance(exc, subprocess.TimeoutExpired)
                # Stop only our uniquely named container; never prune host resources.
                try:
                    subprocess.run(
                        ["docker", "stop", "--time", "5", name],
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                finally:
                    process.kill()
                    process.wait()
                if not timed_out:
                    raise
    finally:
        stop.set()
        watcher.join(timeout=15)
        summary = {
            "command": command,
            "wall_seconds": time.monotonic() - started,
            "exit_status": exit_status,
            "timed_out": timed_out,
            "docker_startup_seconds": "NOT_MEASURED separately; included in wall_seconds",
            "gpu": "NOT_TESTED" if args.gpus == "none" else "requested",
        }
        write_json(output / "container.json", summary)
    if timed_out or exit_status != 0:
        raise ContractError(f"Docker inference failed; retain logs in {output}")
    result = validate_run(data, output)
    model = result["model"]
    if args.gpus != "none" and model.get("layers_offloaded", 0) <= 0:
        raise ContractError("GPU requested but no verified layer offload")
    result.update(summary)
    result["throughput_questions_per_second"] = (
        len(result["question_elapsed_seconds"]) / summary["wall_seconds"]
    )
    result["private_500_question_estimate_seconds"] = model.get(
        "startup_seconds_including_hash", 0
    ) + (sum(result["question_elapsed_seconds"]) / result["questions"] * 500)
    result["estimate_only_not_sla"] = True
    result["within_six_hours_for_this_run"] = summary["wall_seconds"] <= 21600
    write_json(output / "verification.json", result)
    return result


def smoke(image: str, output: Path, args, *, repeats: int) -> dict:
    output.mkdir(parents=True)
    if args.cache_dir:
        no_symlinks(args.cache_dir).mkdir(parents=True, exist_ok=True)
        cache_root = fresh(args.cache_dir / output.name, args.input, output)
    else:
        cache_root = output / "cache"
    cache_root.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="datamate-submission-", dir=args.temp_dir) as scratch:
        data = Path(scratch) / "data"
        staged = stage(args.input, data, limit=args.limit if args.mode == "smoke" else None)
        write_json(output / "input_manifest.json", staged)
        runs = [
            run_container(image, data, output / f"run-{i}", cache_root / f"cold-{i}", args)
            for i in range(repeats)
        ]
        if staged["sha256"] != {n: digest(p) for n, p in input_files(data).items()}:
            raise ContractError("staged input mutated during inference")
        if len({r["submission_sha256"] for r in runs}) != 1:
            raise ContractError("identical image/input produced different predictions")
        if any(r["identity"] != runs[0]["identity"] for r in runs):
            raise ContractError("source/model/input/config identity changed between repeats")
    result = {
        "input": staged,
        "runs": runs,
        "identical_predictions": True,
        "cache": "fresh independent directory per run",
    }
    write_json(output / "smoke.json", result)
    return result


def archive_check(path: Path, image: str, image_id: str) -> dict:
    """Read gzip to EOF (CRC), count actual tar bytes, inspect all layers without extraction."""
    no_symlinks(path)
    uncompressed = 0
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            uncompressed += len(chunk)
            if uncompressed > MAX_TAR_BYTES:
                raise ContractError("uncompressed docker save tar exceeds 100 GB")
    if uncompressed == 0:
        raise ContractError("empty image archive")
    saved, config_hashes, layers = None, set(), 0
    forbidden = {"answers.jsonl", "questions.jsonl", "calendar.csv", "rulebook.md", "facts.sqlite"}
    with tarfile.open(path, "r|gz") as bundle:
        for member in bundle:
            if member.name == "manifest.json":
                if saved is not None or member.size > 1024 * 1024:
                    raise ContractError("invalid Docker save manifest")
                saved = loads(bundle.extractfile(member).read().decode())
    if not isinstance(saved, list) or len(saved) != 1 or saved[0].get("RepoTags") != [image]:
        raise ContractError("archive must contain exactly the tested image/tag")
    layer_names = set(saved[0].get("Layers", []))
    seen_layers = set()
    with tarfile.open(path, "r|gz") as bundle:
        for member in bundle:
            if member.name == saved[0]["Config"] and member.isfile() and member.size < 10_000_000:
                config_hashes.add(
                    "sha256:" + hashlib.sha256(bundle.extractfile(member).read()).hexdigest()
                )
            elif member.isfile() and member.name in layer_names:
                layers += 1
                seen_layers.add(member.name)
                with tarfile.open(fileobj=bundle.extractfile(member), mode="r|*") as layer:
                    for item in layer:
                        parts = Path(item.name).parts
                        if any(
                            p in {"dataset", ".artifacts", ".git", ".aws", ".env"}
                            or p.removeprefix(".wh.") in forbidden
                            for p in parts
                        ):
                            raise ContractError(
                                f"unwanted data/secret/cache in image layer: {item.name}"
                            )
    if image_id not in config_hashes or not layers or seen_layers != layer_names:
        raise ContractError("archive must contain exactly the tested image/tag and Docker layers")
    return {
        "uncompressed_tar_bytes": uncompressed,
        "layers_checked": layers,
        "image_reference": image,
        "image_id": image_id,
        "status": "passed",
    }


def export_image(image: str, image_id: str, output: Path) -> dict:
    if image_info(image)["id"] != image_id:
        raise ContractError("image tag changed after testing")
    archive = output / "image.tar.gz"
    with (
        archive.open("xb") as target,
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as compressed,
    ):
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["docker", "save", image], stdout=subprocess.PIPE, stderr=errors
            )
            try:
                shutil.copyfileobj(process.stdout, compressed, length=4 * 1024 * 1024)
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                process.stdout.close()
            if process.wait() != 0:
                errors.seek(0)
                raise ContractError("docker save failed: " + errors.read().decode(errors="replace"))
    check = archive_check(archive, image, image_id)
    hashes = {}
    for algorithm in ["md5", "sha256"]:
        with archive.open("rb") as source:
            hashes[algorithm] = hashlib.file_digest(source, algorithm).hexdigest()
        (output / f"image.tar.gz.{algorithm}").write_text(
            f"{hashes[algorithm]}  {archive.name}\n", encoding="utf-8"
        )
    return {"file": archive.name, "bytes": archive.stat().st_size, **hashes, **check}


def load_archive(package: Path, manifest: dict, output: Path) -> None:
    archive = package / manifest["archive"]["file"]
    if archive.parent.resolve() != package.resolve():
        raise ContractError("archive must be directly inside package")
    if archive.stat().st_size != manifest["archive"]["bytes"]:
        raise ContractError("archive byte size mismatch")
    for algorithm in ["md5", "sha256"]:
        with no_symlinks(archive).open("rb") as stream:
            if hashlib.file_digest(stream, algorithm).hexdigest() != manifest["archive"][algorithm]:
                raise ContractError(f"archive {algorithm} mismatch; do not load")
    check = archive_check(archive, manifest["image_reference"], manifest["image"]["id"])
    # Refuse docker load when it would move an existing tag to a different image.
    found = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", manifest["image_reference"]],
        capture_output=True,
        text=True,
    )
    if found.returncode == 0 and found.stdout.strip() != manifest["image"]["id"]:
        raise ContractError("docker load would overwrite another image tag; use a clean daemon")
    text = checked(["docker", "load", "-i", str(archive)], timeout=3600)
    if image_info(manifest["image_reference"])["id"] != manifest["image"]["id"]:
        raise ContractError("reloaded image identity mismatch")
    write_json(
        output / "reload.json",
        {
            **check,
            "docker_load": text,
            "image_tag_was_absent": found.returncode != 0,
            "clean_daemon": "NOT_VERIFIED",
        },
    )


def package_checks(package: Path, manifest: dict) -> None:
    for name, sha in manifest["package_files"].items():
        path = package / name
        if not path.resolve().is_relative_to(package.resolve()) or digest(no_symlinks(path)) != sha:
            raise ContractError(f"package evidence checksum mismatch: {name}")


def prepare(args) -> None:
    output = args.output
    output.mkdir(parents=True)
    # Build only from an immutable, locally present parent; never pull or mutate it.
    base = image_info(args.base_image)
    model = context(output / "build", args.model_manifest, base["id"])
    started = time.monotonic()
    with (
        (output / "build/context.tar").open("rb") as source,
        (output / "build.log").open("x") as log,
    ):
        result = subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--network=none",
                "--pull=false",
                "--target",
                "submission",
                "--build-arg",
                f"BASE_IMAGE={args.base_image}",
                "--build-arg",
                f"SUBMISSION_MODEL={model['models']['models'][0]['image_path']}",
                "-t",
                args.image,
                "-",
            ],
            stdin=source,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=args.build_timeout,
        )
    if result.returncode:
        raise ContractError("Docker build failed; inspect build.log")
    if image_info(args.base_image)["id"] != base["id"]:
        raise ContractError("base image tag changed during build")
    image = image_info(args.image)
    inventory(image["id"], model, output / "image_inventory.json")
    evidence = smoke(image["id"], output / "before_export", args, repeats=2)
    for run in evidence["runs"]:
        if run["model"].get("sha256") != model["models"]["models"][0]["sha256"]:
            raise ContractError("loaded model differs from declared weights")
        for field, declared in model["inference_config"].items():
            key = {"model_threads": "threads"}.get(field, field)
            actual = (
                run["identity"]["dependencies"]["adapter"]["metadata"].get(
                    "question_timeout_seconds"
                )
                if field == "question_timeout"
                else run["model"].get(key)
            )
            if actual != declared:
                raise ContractError(f"inference configuration differs from declaration: {field}")
    archive = export_image(args.image, image["id"], output)
    manifest = {
        "schema": "datamate-package-v1",
        "created_at": now(),
        "image_reference": args.image,
        "image": image,
        "base_image": base,
        "model_and_source": model,
        "archive": archive,
        "before_export": evidence,
        "elapsed_prepare_seconds": time.monotonic() - started,
        "status": "EXPORTED_RELOAD_PENDING",
        "upload": "MANUAL_PENDING",
        "submission": "MANUAL_PENDING",
    }
    # Record before reload: failures leave a concrete, recoverable package and logs.
    (output / "docs").mkdir()
    (output / "config").mkdir()
    for name in DOCS:
        shutil.copyfile(ROOT / "docs" / name, output / "docs" / name)
    shutil.copyfile(ROOT / "docs/model-license.txt", output / "docs/model-license.txt")
    shutil.copyfile(ROOT / "README.md", output / "README.md")
    write_json(output / "config/submission_model.json", model["models"])
    manifest["package_files"] = {
        str(p.relative_to(output)): digest(p)
        for p in sorted(output.rglob("*"))
        if p.is_file()
        and p != output / "image.tar.gz"
        and "cache" not in p.relative_to(output).parts
    }
    write_json(output / "submission_manifest.json", manifest)
    verify_package(args, output, output / "after_reload", manifest)


def verify_package(args, package: Path, output: Path, manifest: dict) -> None:
    package_checks(package, manifest)
    output.mkdir(parents=True)
    load_archive(package, manifest, output)
    inventory(
        manifest["image"]["id"], manifest["model_and_source"], output / "image_inventory.json"
    )
    result = smoke(manifest["image"]["id"], output / "smoke", args, repeats=1)
    previous = manifest["before_export"]
    if result["input"]["sha256"] != previous["input"]["sha256"]:
        raise ContractError(
            "reload verification must use the original input and smoke/full selection"
        )
    if result["runs"][0]["submission_sha256"] != previous["runs"][0]["submission_sha256"]:
        raise ContractError("reloaded image predictions differ from before export")
    if result["runs"][0]["identity"] != previous["runs"][0]["identity"]:
        raise ContractError("reloaded image source/model/input/config identity differs")
    write_json(
        output / "verification.json",
        {
            "status": "VERIFIED",
            "verified_at": now(),
            "archive_sha256": manifest["archive"]["sha256"],
            "same_predictions_before_after_reload": True,
            "gpu": "NOT_TESTED" if args.gpus == "none" else "tested on this host only",
            "reference_hardware_sla": "NOT_TESTED",
            "upload": "MANUAL_PENDING",
            "submission": "MANUAL_PENDING",
        },
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ["context", "prepare", "smoke", "verify", "validate"]:
        command = commands.add_parser(name)
        command.add_argument(
            "--output",
            required=True,
            type=Path,
            help="fresh evidence/package directory; validate: existing run directory",
        )
        if name == "validate":
            command.add_argument("--input", type=Path, required=True)
            continue
        command.add_argument(
            "--execute", action="store_true", help="default is a read-only dry run"
        )
        if name in {"context", "prepare"}:
            command.add_argument(
                "--base-image", required=True, help="local packaged model/runtime image"
            )
            command.add_argument(
                "--model-manifest", type=Path, default=ROOT / "config/submission_model.json"
            )
        if name in {"prepare", "smoke"}:
            command.add_argument("--image", required=True)
        if name == "verify":
            command.add_argument("--package", required=True, type=Path)
        if name != "context":
            command.add_argument("--input", required=True, type=Path)
            command.add_argument("--mode", choices=["smoke", "full"], default="smoke")
            command.add_argument(
                "--limit", type=int, default=3, help="first N questions; smoke only"
            )
            command.add_argument(
                "--gpus", default="none", help="none, all, or Docker device selector"
            )
            command.add_argument(
                "--timeout", type=float, default=21600, help="wall seconds per run"
            )
            command.add_argument("--build-timeout", type=float, default=3600)
            command.add_argument(
                "--cache-dir", type=Path, help="fresh external cache root, never reused"
            )
            command.add_argument(
                "--temp-dir", type=Path, help="existing directory for staging input"
            )
            command.add_argument("--tmpfs-size", default="256m")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            print(dumps(validate_run(args.input, args.output)))
            return 0
        protected = [args.input] if hasattr(args, "input") else []
        if hasattr(args, "package"):
            protected.append(args.package)
        args.output = fresh(args.output, *protected)
        if hasattr(args, "input"):
            no_symlinks(args.input)
            load_split(args.input)
            if args.limit < 1 or any(
                not math.isfinite(t) or t <= 0 for t in (args.timeout, args.build_timeout)
            ):
                raise ContractError("limit and timeouts must be positive")
            if args.cache_dir:
                fresh(args.cache_dir, args.input, args.output)
            if args.temp_dir:
                no_symlinks(args.temp_dir)
                if args.temp_dir.resolve().is_relative_to(args.input.resolve()):
                    raise ContractError("temporary staging must be outside input")
        if hasattr(args, "model_manifest"):
            model_declaration(args.model_manifest)
        if hasattr(args, "image") and not re.fullmatch(
            r"[a-z0-9][a-z0-9._:/-]*:[A-Za-z0-9_.-]+", args.image
        ):
            raise ContractError("use an explicit image name:tag")
        if not args.execute:
            print(
                dumps(
                    {
                        "dry_run": True,
                        "command": args.command,
                        "output": str(args.output),
                        "actions": "context/build, cold inference, export/load as applicable",
                        "upload": False,
                        "run_with": "add --execute after review",
                    }
                )
            )
            return 0
        if args.command == "context":
            context(args.output, args.model_manifest, args.base_image)
        elif args.command == "prepare":
            found = subprocess.run(["docker", "image", "inspect", args.image], capture_output=True)
            if found.returncode == 0:
                raise ContractError("target image tag already exists; choose a new tag")
            prepare(args)
        elif args.command == "smoke":
            info = image_info(args.image)
            smoke(info["id"], args.output, args, repeats=2)
        else:
            manifest = loads((no_symlinks(args.package) / "submission_manifest.json").read_text())
            verify_package(args, args.package, args.output, manifest)
        print(dumps({"status": "completed", "output": str(args.output), "upload": False}))
        return 0
    except (
        ValueError,
        OSError,
        EOFError,
        subprocess.SubprocessError,
        tarfile.TarError,
        KeyError,
        TypeError,
    ) as exc:
        print(dumps({"status": "FAILED", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
