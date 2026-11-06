from __future__ import annotations

import base64
import hashlib
import tarfile
from importlib.metadata import FileHash, PackagePath
from types import SimpleNamespace

import pytest

from scripts.build_context import build_context, runtime_files
from src.jsonio import ContractError


def source_tree(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    for name in [
        "Dockerfile",
        ".dockerignore",
        "predict.sh",
        "src/__init__.py",
        "pyproject.toml",
        "uv.lock",
    ]:
        (root / name).write_text("fixture\n")
    (root / "dataset").mkdir()
    (root / "dataset/secret.csv").write_text("must not be packaged")
    (root / ".env").write_text("must not be packaged")
    return root


def test_allowlisted_context_is_deterministic_and_excludes_local_inputs(tmp_path):
    root = source_tree(tmp_path)
    a, b = tmp_path / "a.tar", tmp_path / "b.tar"
    assert build_context(root, a) == build_context(root, b)
    with tarfile.open(a) as archive:
        assert set(archive.getnames()) == {
            "Dockerfile",
            ".dockerignore",
            "predict.sh",
            "src/__init__.py",
            "pyproject.toml",
            "uv.lock",
        }
    with pytest.raises(ContractError, match="overwrite"):
        build_context(root, root / "Dockerfile")
    with pytest.raises(ContractError, match="dataset"):
        build_context(root, root / "dataset/output.tar")
    (root / "src/escape.py").symlink_to(root / ".env")
    with pytest.raises(ContractError, match="symlink"):
        build_context(root, b)


def test_llm_bundle_copies_only_explicit_runtime_model_and_licenses(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    (root / "uv.lock").write_text("fixture lock")
    dependency = tmp_path / "dependency.py"
    dependency.write_text("fixture dependency")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF fixture")
    monkeypatch.setattr(
        "scripts.build_context.runtime_files",
        lambda _: ({"llm/python/dependency.py": dependency}, {"fixture": "1.0"}),
    )
    target = tmp_path / "bundle.tar"
    manifest = build_context(root, target, model=model)
    assert manifest["runtime_packages"] == {"fixture": "1.0"}
    with tarfile.open(target) as archive:
        assert archive.extractfile("llm/model.gguf").read() == model.read_bytes()
        assert "dataset/secret.csv" not in archive.getnames()
    link = tmp_path / "link.gguf"
    link.symlink_to(model)
    with pytest.raises(ContractError, match="existing local"):
        build_context(root, target, model=link)


def test_source_refresh_includes_model_license_without_other_documents(tmp_path):
    root = source_tree(tmp_path)
    (root / "docs").mkdir()
    (root / "docs/model-license.txt").write_text("fixture model license")
    (root / "docs/private-notes.md").write_text("must not be packaged")
    output = tmp_path / "licensed.tar"
    build_context(root, output)
    with tarfile.open(output) as archive:
        assert archive.extractfile("docs/model-license.txt").read() == b"fixture model license"
        assert "docs/private-notes.md" not in archive.getnames()


@pytest.mark.parametrize("damage", [None, "record", "version", "escape"])
def test_runtime_bundle_checks_lock_record_and_path_boundary(tmp_path, monkeypatch, damage):
    root = source_tree(tmp_path)
    (root / "uv.lock").write_text("""[[package]]
name = "llama-cpp-python"
version = "0.3.16"
""")
    site = tmp_path / "site"
    site.mkdir()
    module = site / "runtime.py"
    module.write_text("fixture runtime")
    entry = PackagePath("../escape.py" if damage == "escape" else "runtime.py")
    checksum = base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest())
    entry.hash = FileHash("sha256=" + checksum.rstrip(b"=").decode())
    if damage == "record":
        module.write_text("tampered")
    dist = SimpleNamespace(
        version="9.9" if damage == "version" else "0.3.16",
        requires=[],
        files=[entry],
        locate_file=lambda p: site / p,
    )
    monkeypatch.setattr("scripts.build_context.require_project_environment", lambda: None)
    monkeypatch.setattr("scripts.build_context.sysconfig.get_path", lambda _: str(site))
    monkeypatch.setattr("scripts.build_context.importlib.metadata.distribution", lambda _: dist)
    if damage:
        with pytest.raises(
            ContractError,
            match={"record": "RECORD mismatch", "version": "uv.lock", "escape": "escapes package"}[
                damage
            ],
        ):
            runtime_files(root)
    else:
        files, packages = runtime_files(root)
        assert files["llm/python/runtime.py"] == module
        assert packages == {"llama-cpp-python": "0.3.16"}


@pytest.mark.parametrize(
    "requested,supported,layers,ready",
    [(0, False, 0, True), (-1, False, 0, False), (-1, True, 0, False), (-1, True, 29, True)],
)
def test_worker_rejects_silent_gpu_fallback(monkeypatch, requested, supported, layers, ready):
    import sys

    from src.agent.local import _worker

    callback = None
    records = []

    def register(handler, _):
        nonlocal callback
        callback = handler

    def model(**kwargs):
        assert kwargs["n_gpu_layers"] == requested
        callback(1, f"llama_model_load: offloaded {layers}/29 layers to GPU".encode(), None)
        return SimpleNamespace(n_threads_batch=4, n_batch=128, metadata={}, close=lambda: None)

    backend = SimpleNamespace(
        Llama=model,
        llama_log_callback=lambda f: f,
        llama_log_set=register,
        llama_perf_context=lambda _: None,
        llama_perf_context_reset=lambda _: None,
        llama_supports_gpu_offload=lambda: supported,
    )
    monkeypatch.setitem(sys.modules, "llama_cpp", backend)
    monkeypatch.setattr("src.agent.local.importlib.metadata.version", lambda _: "0.3.16")
    pipe = SimpleNamespace(send=records.append, recv=lambda: None, close=lambda: None)
    _worker(pipe, "fixture.gguf", 4096, 4, requested)
    if ready:
        assert records[0]["ready"] and records[0]["layers_offloaded"] == layers
    else:
        assert "CPU fallback rejected" in records[0]["error"]


def test_llm_container_acceptance_rejects_holdout_before_starting_docker(tmp_path, monkeypatch):
    from scripts.verify_runtime import check_llm_image
    from src.jsonio import write_json

    (tmp_path / "config").mkdir()
    write_json(
        tmp_path / "config/validation_split.json",
        {"protocols": {"business": {"partitions": {"dev": {"question_ids": ["dev"]}}}}},
    )
    monkeypatch.setattr("scripts.verify_runtime.ROOT", tmp_path)
    monkeypatch.setattr("scripts.verify_runtime.load_split", lambda _: SimpleNamespace())

    def forbidden(*args, **kwargs):
        raise AssertionError("Docker must not start for a holdout selection")

    monkeypatch.setattr("scripts.verify_runtime.checked", forbidden)
    with pytest.raises(ContractError, match="both dev"):
        check_llm_image("unused", 0, tmp_path, {"expected_intents_and_windows": {"holdout": None}})


@pytest.mark.parametrize("damage", [None, "version", "native_record"])
def test_cuda_bundle_is_locked_and_omits_duplicate_native_install(tmp_path, monkeypatch, damage):
    root = source_tree(tmp_path)
    (root / "uv.lock").write_text("""[[package]]
name = "llama-cpp-python"
version = "0.3.16"
[[package]]
name = "nvidia-cuda-runtime-cu12"
version = "12.4.127"
[[package]]
name = "nvidia-cublas-cu12"
version = "12.4.5.8"
""")
    site, cuda = tmp_path / "site", tmp_path / "cuda"
    site.mkdir()
    cuda.mkdir()

    def distribution(folder, version, paths, name):
        entries = []
        for relative in paths:
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture native library")
            entry = PackagePath(relative)
            digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest())
            entry.hash = FileHash("sha256=" + digest.rstrip(b"=").decode())
            entries.append(entry)
        return SimpleNamespace(
            version=version,
            requires=[],
            files=entries,
            metadata={"Name": name},
            locate_file=lambda p: folder / p,
        )

    binding = distribution(
        cuda,
        "9.9" if damage == "version" else "0.3.16",
        ["llama_cpp/lib/libggml-cuda.so", "lib/libggml-cuda.so"],
        "llama_cpp_python",
    )
    native = {
        "nvidia-cuda-runtime-cu12": distribution(
            site,
            "12.4.127",
            ["nvidia/cuda_runtime/lib/libcudart.so.12"],
            "nvidia-cuda-runtime-cu12",
        ),
        "nvidia-cublas-cu12": distribution(
            site, "12.4.5.8", ["nvidia/cublas/lib/libcublas.so.12"], "nvidia-cublas-cu12"
        ),
    }
    if damage == "native_record":
        (site / "nvidia/cublas/lib/libcublas.so.12").write_bytes(b"corrupt")
    monkeypatch.setattr("scripts.build_context.require_project_environment", lambda: None)
    monkeypatch.setattr("scripts.build_context.sysconfig.get_path", lambda _: str(site))
    monkeypatch.setattr(
        "scripts.build_context.importlib.metadata.distributions", lambda **_: [binding]
    )
    monkeypatch.setattr("scripts.build_context.importlib.metadata.distribution", native.__getitem__)
    if damage:
        with pytest.raises(ContractError, match="uv.lock" if damage == "version" else "RECORD"):
            runtime_files(root, cuda_runtime=cuda)
    else:
        files, packages = runtime_files(root, cuda_runtime=cuda)
        assert "llm/python/lib/libggml-cuda.so" not in files
        assert "llm/python/llama_cpp/lib/libggml-cuda.so" in files
        assert "llm/lib/libcublas.so.12" in files
        assert packages["llama-cpp-python"] == "0.3.16"
