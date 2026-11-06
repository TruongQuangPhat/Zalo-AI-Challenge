from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.submission import pipeline
from src.jsonio import ContractError, loads, write_json
from src.runtime import run_inference


def saved_image(tmp_path, *, layer_paths=("code/src/runtime.py",), blob_layout=False, images=1):
    config = b'{"architecture":"amd64","os":"linux","rootfs":{"diff_ids":[]}}'
    image_id = "sha256:" + hashlib.sha256(config).hexdigest()
    tag = "zaic2026_datamate_test:review1"
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        for name in layer_paths:
            pipeline.add_bytes(archive, name, b"fixture bytes")
    config_name = "blobs/sha256/config" if blob_layout else "config.json"
    layer_name = "blobs/sha256/layer" if blob_layout else "layer/layer.tar"
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as archive:
        pipeline.add_bytes(archive, config_name, config)
        pipeline.add_bytes(archive, layer_name, layer.getvalue())
        manifest = [{"Config": config_name, "RepoTags": [tag], "Layers": [layer_name]}] * images
        pipeline.add_bytes(archive, "manifest.json", json.dumps(manifest).encode())
    path = tmp_path / "image.tar.gz"
    path.write_bytes(gzip.compress(tar.getvalue(), mtime=0))
    return path, tag, image_id, tar.getvalue()


def test_dry_run_checks_inputs_but_never_calls_docker_or_writes(
    synthetic_split, tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run invoked a subprocess")

    monkeypatch.setattr(pipeline.subprocess, "run", forbidden)
    output = tmp_path / "new-package"
    assert (
        pipeline.main(
            [
                "prepare",
                "--base-image",
                "local-parent:review",
                "--image",
                "local-candidate:review",
                "--input",
                str(synthetic_split),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert not output.exists()


def test_fresh_outputs_cannot_overlap_source_inputs_or_symlinks(synthetic_split, tmp_path):
    for path in [
        synthetic_split / "out",
        synthetic_split.parent,
        pipeline.ROOT / "dataset/out",
        pipeline.ROOT / "src/out",
    ]:
        with pytest.raises(ContractError, match="disjoint"):
            pipeline.fresh(path, synthetic_split)
    with pytest.raises(ContractError, match="already exists"):
        pipeline.fresh(tmp_path)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink"):
        pipeline.fresh(tmp_path / "link/out")


def test_staging_never_reads_labels_preserves_inputs_and_selects_in_order(
    synthetic_split, tmp_path, monkeypatch
):
    original = Path.open
    before = {p: p.read_bytes() for p in synthetic_split.rglob("*") if p.is_file()}

    def guarded(path, *args, **kwargs):
        if path.name == "answers.jsonl":
            raise AssertionError("submission packaging read labels")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    output = tmp_path / "staged"
    summary = pipeline.stage(synthetic_split, output, limit=1)
    assert summary["ids"] == ["q_one"] and not summary["labels_mounted"]
    assert not (output / "answers.jsonl").exists()
    assert len((output / "questions.jsonl").read_text().splitlines()) == 1
    assert (output / "data/shop_a/sales.csv").read_bytes() == before[
        synthetic_split / "data/shop_a/sales.csv"
    ]
    monkeypatch.setattr(Path, "open", original)
    assert before == {p: p.read_bytes() for p in synthetic_split.rglob("*") if p.is_file()}


def test_context_has_fixed_entrypoint_provenance_and_no_dataset(tmp_path):
    output = tmp_path / "context"
    manifest = pipeline.context(
        output, pipeline.ROOT / "config/submission_model.json", "local:base"
    )
    with tarfile.open(output / "context.tar") as archive:
        names = archive.getnames()
        assert not any(name.startswith(("dataset/", ".artifacts/")) for name in names)
        assert not any(Path(name).name == "answers.jsonl" for name in names)
        assert "predict.sh" in names and "src/runtime.py" in names
        assert "submission/predict.sh" not in names
        assert "src/agent/mock.py" not in names
        assert "llm/model.gguf" not in names  # Retained by the packaged parent, not recopied.
        internal = loads(archive.extractfile("submission/artifact_manifest.json").read().decode())
        assert internal == manifest
        training = loads(
            archive.extractfile("submission/training_code/data_manifest.json").read().decode()
        )
        assert training["performed"] is False and training["files"] == []
        assert "/opt/model/LICENSE" in manifest["files"]
        assert "/code/predict.sh" in manifest["files"]
        assert archive.getmember("predict.sh").mode == 0o555


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(fine_tuning=True),
        lambda v: v["models"][0].update(parameter_budget_upper_bound=15_000_000_001),
        lambda v: v["models"][0].update(public_release_date="2026-10-26"),
        lambda v: v["models"][0].update(sha256="not-a-hash"),
    ],
)
def test_ineligible_or_trained_declarations_are_rejected(tmp_path, change):
    value = json.loads((pipeline.ROOT / "config/submission_model.json").read_text())
    change(value)
    path = tmp_path / "model.json"
    write_json(path, value)
    with pytest.raises(ContractError):
        pipeline.model_declaration(path)


class PredefinedPlanner:
    name = "local-planner"
    metadata = {
        "question_timeout_seconds": 120,
        "model": {
            "sha256": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
            "load_seconds": 0,
            "seed": 0,
            "temperature": 0,
            "n_ctx": 4096,
            "n_batch": 128,
            "threads": 4,
        },
    }

    def decide(self, question, *, context):
        return {
            "answer": {"id": question.id, "type": "number", "answer": 12},
            "plan": {"status": "synthetic-test", "planning": {"attempts": []}},
            "execution": {"status": "verified", "rounded_value": 12},
        }


def test_official_logging_preserves_predictions_actual_inputs_and_return(synthetic_split, tmp_path):
    output = tmp_path / "run"
    run_inference(synthetic_split, output, planner=PredefinedPlanner())
    report = pipeline.validate_run(synthetic_split, output)
    assert report["questions"] == 2 and report["types"] == {"number": 2}
    for path in (output / "logs").glob("*.json"):
        record = loads(path.read_text())
        assert type(record["elapsed_ms"]) is int and record["elapsed_ms"] >= 0
        assert record["thinking"] is None
        assert record["code"].endswith("::PredefinedPlanner.decide")
        assert record["inputs"]["question"]["id"] == record["id"]
        assert record["output"]["answer"] == record["answer"]


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "numeric_bool",
        "missing_log",
        "thinking",
        "elapsed",
        "incomplete",
        "wrong_planner",
    ],
)
def test_validator_rejects_incomplete_or_invalid_evidence(synthetic_split, tmp_path, damage):
    output = tmp_path / "run"
    run_inference(synthetic_split, output, planner=PredefinedPlanner())
    prediction = output / "submission.jsonl"
    log = sorted((output / "logs").glob("*.json"))[0]
    manifest = output / "run_manifest.json"
    if damage == "duplicate":
        lines = prediction.read_text().splitlines()
        prediction.write_text(lines[0] + "\n" + lines[0] + "\n")
    elif damage == "numeric_bool":
        prediction.write_text(prediction.read_text().replace('"answer":12', '"answer":true'))
    elif damage == "missing_log":
        log.unlink()
    elif damage in {"thinking", "elapsed"}:
        value = loads(log.read_text())
        value.pop("thinking") if damage == "thinking" else value.update(elapsed_ms=True)
        write_json(log, value)
    else:
        value = loads(manifest.read_text())
        value.update(status="interrupted") if damage == "incomplete" else value.update(
            adapter="unexpected"
        )
        write_json(manifest, value)
    with pytest.raises(ContractError):
        pipeline.validate_run(synthetic_split, output)


@pytest.mark.parametrize("blob_layout", [False, True])
def test_archive_counts_actual_tar_bytes_and_checks_single_image(tmp_path, blob_layout):
    path, tag, image_id, raw = saved_image(tmp_path, blob_layout=blob_layout)
    report = pipeline.archive_check(path, tag, image_id)
    assert report["uncompressed_tar_bytes"] == len(raw)
    assert report["layers_checked"] == 1 and report["status"] == "passed"
    with pytest.raises(ContractError, match="tested image"):
        pipeline.archive_check(path, tag, "sha256:" + "0" * 64)


@pytest.mark.parametrize(
    "unwanted",
    [
        "dataset/train/a.csv",
        "app/answers.jsonl",
        "app/.wh.answers.jsonl",
        "app/.artifacts/prepared/facts.sqlite",
        "root/.aws/credentials",
    ],
)
def test_archive_rejects_data_even_in_inherited_or_whiteout_layers(tmp_path, unwanted):
    path, tag, image_id, _ = saved_image(tmp_path, layer_paths=(unwanted,))
    with pytest.raises(ContractError, match="unwanted"):
        pipeline.archive_check(path, tag, image_id)


def test_archive_size_limit_corruption_and_multiple_images(tmp_path, monkeypatch):
    path, tag, image_id, _ = saved_image(tmp_path, images=2)
    with pytest.raises(ContractError, match="exactly"):
        pipeline.archive_check(path, tag, image_id)
    path, tag, image_id, _ = saved_image(tmp_path)
    monkeypatch.setattr(pipeline, "MAX_TAR_BYTES", 100)
    with pytest.raises(ContractError, match="100 GB"):
        pipeline.archive_check(path, tag, image_id)
    path.write_bytes(path.read_bytes()[:-5])
    with pytest.raises(EOFError):
        pipeline.archive_check(path, tag, image_id)


def test_failed_docker_save_is_not_certified(tmp_path, monkeypatch):
    popen = subprocess.Popen
    monkeypatch.setattr(pipeline, "image_info", lambda _: {"id": "fixture-id"})
    monkeypatch.setattr(
        pipeline.subprocess,
        "Popen",
        lambda *a, **kw: popen(
            [sys.executable, "-c", "import sys; print('partial archive'); sys.exit(8)"], **kw
        ),
    )
    with pytest.raises(ContractError, match="docker save failed"):
        pipeline.export_image("local:test", "fixture-id", tmp_path)
    assert not (tmp_path / "image.tar.gz.md5").exists()


def test_export_writes_both_checksums_and_reload_refuses_tag_conflict(tmp_path, monkeypatch):
    archive, tag, image_id, raw = saved_image(tmp_path)
    archive.unlink()
    raw_path = tmp_path / "raw-tar"
    raw_path.write_bytes(raw)
    popen = subprocess.Popen
    monkeypatch.setattr(pipeline, "image_info", lambda _: {"id": image_id})
    monkeypatch.setattr(
        pipeline.subprocess,
        "Popen",
        lambda *a, **kw: popen(
            [
                sys.executable,
                "-c",
                "import sys;from pathlib import Path;"
                "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
                str(raw_path),
            ],
            **kw,
        ),
    )
    result = pipeline.export_image(tag, image_id, tmp_path)
    assert result["uncompressed_tar_bytes"] == len(raw)
    assert (tmp_path / "image.tar.gz.md5").read_text().startswith(result["md5"])
    assert (tmp_path / "image.tar.gz.sha256").read_text().startswith(result["sha256"])
    manifest = {"archive": result, "image_reference": tag, "image": {"id": image_id}}
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="sha256:different\n"),
    )
    with pytest.raises(ContractError, match="overwrite another image tag"):
        pipeline.load_archive(tmp_path, manifest, tmp_path)
    result["sha256"] = "0" * 64
    with pytest.raises(ContractError, match="sha256 mismatch"):
        pipeline.load_archive(tmp_path, manifest, tmp_path)


def test_container_uses_official_no_args_readonly_offline_and_cpu_flag(
    synthetic_split, tmp_path, monkeypatch
):
    commands = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            mounts = {
                part.split("target=")[1]: Path(part.split("source=")[1].split(",")[0])
                for part in command
                if part.startswith("type=bind,") and "target=" in part
            }
            run_inference(synthetic_split, mounts["/result"], planner=PredefinedPlanner())

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(pipeline.subprocess, "Popen", Process)
    args = SimpleNamespace(gpus="none", timeout=10, tmpfs_size="64m")
    report = pipeline.run_container(
        "sha256:test", synthetic_split, tmp_path / "run", tmp_path / "cache", args
    )
    command = commands[0]
    assert "--network=none" in command and "--read-only" in command
    assert "--shm-size=16g" in command and "DATAMATE_GPU_LAYERS=0" in command
    assert "DATAMATE_CACHE=/cache" in command and "--gpus" not in command
    assert command[-4:] == ["--entrypoint", "bash", "sha256:test", "/code/predict.sh"]
    assert any("target=/data,readonly" in part for part in command)
    assert report["gpu"] == "NOT_TESTED" and report["exit_status"] == 0


def test_cli_defaults_use_one_real_planner_and_close_model(synthetic_split, tmp_path, monkeypatch):
    from src.cli import main

    calls = []

    class Model:
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))

        def close(self):
            calls.append("closed")

    monkeypatch.setenv("DATAMATE_MODEL", str(tmp_path / "configured.gguf"))
    monkeypatch.setenv("DATAMATE_GPU_LAYERS", "0")
    monkeypatch.setenv("DATAMATE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr("src.agent.local.LocalModel", Model)
    monkeypatch.setattr(
        "src.agent.planner.LocalPlanner", lambda model, **kwargs: PredefinedPlanner()
    )
    assert (
        main(["predict", "--data-dir", str(synthetic_split), "--output-dir", str(tmp_path / "run")])
        == 0
    )
    assert calls[0][0] == tmp_path / "configured.gguf"
    assert calls[0][1]["gpu_layers"] == 0 and calls[-1] == "closed"
    assert (tmp_path / "cache").is_dir()
    assert pipeline.validate_run(synthetic_split, tmp_path / "run")["questions"] == 2


def test_prepare_verify_roundtrip_with_synthetic_docker_archive(
    synthetic_split, tmp_path, monkeypatch
):
    """Exercise real staging/inference/validation/hashes/reload comparison; no Docker or LLM."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _, tag, image_id, raw = saved_image(seed)
    raw_path = seed / "raw.tar"
    raw_path.write_bytes(raw)
    actual_run, actual_popen = subprocess.run, subprocess.Popen

    def run(command, **kwargs):
        if command[:2] == ["docker", "build"]:
            assert "--target" in command and "submission" in command
            assert "--network=none" in command
            with tarfile.open(fileobj=kwargs["stdin"], mode="r") as archive:
                assert "submission/artifact_manifest.json" in archive.getnames()
            return SimpleNamespace(returncode=0)
        if command[:2] == ["docker", "image"]:
            return SimpleNamespace(returncode=0, stdout=image_id + "\n")
        return actual_run(command, **kwargs)

    def popen(command, **kwargs):
        if command[:2] == ["docker", "save"]:
            return actual_popen(
                [
                    sys.executable,
                    "-c",
                    "import sys;from pathlib import Path;"
                    "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())",
                    str(raw_path),
                ],
                **kwargs,
            )
        return actual_popen(command, **kwargs)

    def inventory(image, manifest, output):
        write_json(output, {"synthetic_inventory": True})

    def container(image, data, output, cache, args):
        cache.mkdir(parents=True)
        run_inference(data, output, planner=PredefinedPlanner(), cache_dir=cache)
        return pipeline.validate_run(data, output)

    monkeypatch.setattr(pipeline, "image_info", lambda _: {"id": image_id, "config": {}, "size": 1})
    monkeypatch.setattr(pipeline, "inventory", inventory)
    monkeypatch.setattr(pipeline, "run_container", container)
    monkeypatch.setattr(pipeline.subprocess, "run", run)
    monkeypatch.setattr(pipeline.subprocess, "Popen", popen)
    monkeypatch.setattr(pipeline, "checked", lambda *a, **k: "synthetic Docker load completed")
    output = tmp_path / "package"
    cache = tmp_path / "external-cache"
    args = pipeline.parser().parse_args(
        [
            "prepare",
            "--base-image",
            "local:parent",
            "--image",
            tag,
            "--input",
            str(synthetic_split),
            "--output",
            str(output),
            "--cache-dir",
            str(cache),
            "--limit",
            "1",
            "--execute",
        ]
    )
    pipeline.prepare(args)
    manifest = loads((output / "submission_manifest.json").read_text())
    proof = loads((output / "after_reload/verification.json").read_text())
    assert proof["status"] == "VERIFIED" and proof["same_predictions_before_after_reload"]
    assert len(manifest["before_export"]["runs"]) == 2
    assert manifest["upload"] == "MANUAL_PENDING"
    assert (cache / "before_export/cold-0").is_dir()
    assert (cache / "smoke/cold-0").is_dir()
    pipeline.package_checks(output, manifest)
    # A successful prior package must never conceal changed input bytes.
    source = synthetic_split / "data/shop_a/sales.csv"
    source.write_text(source.read_text() + "order_2,200\n")
    args.cache_dir = None
    with pytest.raises(ContractError, match="original input"):
        pipeline.verify_package(args, output, tmp_path / "changed-input-verify", manifest)
