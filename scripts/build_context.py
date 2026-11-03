"""Allowlisted Docker context, optionally bundling the locked local LLM runtime."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import platform
import sys
import sysconfig
import tarfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from src.environment import require_project_environment
from src.jsonio import ContractError, write_json

ROOT = Path(__file__).resolve().parents[1]


def runtime_files(
    root: Path, *, cuda_runtime: Path | None = None
) -> tuple[dict[str, Path], dict[str, str]]:
    """Snapshot only the installed LLM dependency closure, checked against uv.lock.

    Never copy an environment wholesale. RECORD hashes are checked, console
    entrypoints/bytecode excluded; licenses and distribution metadata retained.
    These CPython 3.11 Linux binaries still need a real container load test.
    """
    require_project_environment()
    if sys.version_info[:2] != (3, 11) or platform.system() != "Linux":
        raise ContractError("LLM bundle requires the Linux Python 3.11 project environment")
    if platform.machine() != "x86_64":
        raise ContractError("LLM bundle currently targets Linux x86_64")
    locked = {
        canonicalize_name(p["name"]): p["version"]
        for p in tomllib.loads((root / "uv.lock").read_text())["package"]
    }
    site = Path(sysconfig.get_path("purelib")).resolve()
    override = None
    if cuda_runtime:
        candidates = list(importlib.metadata.distributions(path=[str(cuda_runtime)]))
        if (
            len(candidates) != 1
            or canonicalize_name(candidates[0].metadata["Name"]) != "llama-cpp-python"
        ):
            raise ContractError("CUDA artifact must contain only the locked llama-cpp-python wheel")
        override = candidates[0]
    pending = ["llama-cpp-python"]
    packages, files = {}, {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in packages:
            continue
        dist = (
            override
            if override is not None and name == "llama-cpp-python"
            else importlib.metadata.distribution(name)
        )
        package_site = cuda_runtime.resolve() if dist is override else site
        if dist.version != locked.get(name):
            raise ContractError(f"installed runtime differs from uv.lock: {name}")
        packages[name] = dist.version
        for line in dist.requires or []:
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                dependency = importlib.metadata.distribution(req.name)
                if dependency.version not in req.specifier:
                    raise ContractError(f"runtime dependency unsatisfied: {line}")
                pending.append(req.name)
        if not dist.files:
            raise ContractError(f"runtime distribution has no RECORD: {name}")
        for entry in dist.files:
            relative = Path(entry)
            if name == "llama-cpp-python" and relative.parts[0] in {"lib", "include", "share"}:
                continue  # Duplicate CMake install; ctypes uses llama_cpp/lib only.
            if relative.suffix == ".pyc" or "__pycache__" in relative.parts:
                continue
            if relative.parts[:4] == ("..", "..", "..", "bin"):
                continue  # NumPy console tools are not used by inference.
            if relative.is_absolute() or ".." in relative.parts:
                raise ContractError(f"runtime archive path escapes package: {entry}")
            path = Path(dist.locate_file(entry))
            if path.is_symlink() or not path.resolve().is_relative_to(package_site):
                raise ContractError(f"runtime file escapes site-packages: {entry}")
            if entry.hash:
                with path.open("rb") as stream:
                    actual = (
                        base64.urlsafe_b64encode(
                            hashlib.file_digest(stream, entry.hash.mode).digest()
                        )
                        .rstrip(b"=")
                        .decode()
                    )
                if actual != entry.hash.value:
                    raise ContractError(f"runtime RECORD mismatch: {entry}")
            target = "llm/python/" + relative.as_posix()
            if target in files:
                raise ContractError(f"runtime file collision: {target}")
            files[target] = path
    # The slim base already supplies libc/libstdc++/libgcc. OpenMP is the one
    # extra system dependency of the locally built CPU backend; keep its license.
    files["llm/lib/libgomp.so.1"] = Path("/lib/x86_64-linux-gnu/libgomp.so.1").resolve()
    files["llm/licenses/libgomp-copyright"] = Path("/usr/share/doc/libgomp1/copyright")
    if cuda_runtime:
        if "llm/python/llama_cpp/lib/libggml-cuda.so" not in files:
            raise ContractError("CUDA artifact has no ggml CUDA backend")
        for name in ["nvidia-cuda-runtime-cu12", "nvidia-cublas-cu12"]:
            dist = importlib.metadata.distribution(name)
            if dist.version != locked.get(name):
                raise ContractError(f"CUDA runtime differs from uv.lock: {name}")
            packages[name] = dist.version
            for entry in dist.files or []:
                path = Path(dist.locate_file(entry))
                if not path.resolve().is_relative_to(site) or path.is_symlink():
                    raise ContractError("CUDA library escapes installed project dependencies")
                if "/lib/" in str(entry) and ".so" in path.name:
                    if entry.hash:
                        with path.open("rb") as stream:
                            actual = (
                                base64.urlsafe_b64encode(
                                    hashlib.file_digest(stream, entry.hash.mode).digest()
                                )
                                .rstrip(b"=")
                                .decode()
                            )
                        if actual != entry.hash.value:
                            raise ContractError(f"CUDA library RECORD mismatch: {entry}")
                    files["llm/lib/" + path.name] = path
                elif "license" in path.name.lower():
                    files["llm/licenses/" + name + "/" + path.name] = path
    return files, dict(sorted(packages.items()))


def build_context(
    root: Path, output: Path, *, model: Path | None = None, cuda_runtime: Path | None = None
) -> dict:
    root = root.resolve()
    candidates = [root / p for p in ["Dockerfile", ".dockerignore", "predict.sh"]]
    candidates.extend(sorted((root / "src").rglob("*.py")))
    sources = {p.relative_to(root).as_posix(): p for p in candidates}
    packages = {}
    if model is not None and cuda_runtime is not None:
        raise ContractError("build CPU with --model, CUDA separately from the verified CPU image")
    if model is not None:
        if model.is_symlink() or model.suffix.lower() != ".gguf" or not model.is_file():
            raise ContractError("bundle model must be an existing local GGUF file")
        if (root / "dataset").resolve() in model.resolve().parents:
            raise ContractError("model artifact must be outside dataset")
        bundle, packages = runtime_files(root)
        sources.update(bundle)
        sources["llm/model.gguf"] = model
    if cuda_runtime is not None:
        if cuda_runtime.is_symlink() or not cuda_runtime.is_dir():
            raise ContractError("CUDA runtime must be a local artifact directory")
        bundle, packages = runtime_files(root, cuda_runtime=cuda_runtime)
        sources.update(bundle)
    if output.resolve().is_relative_to(root / "dataset") or any(
        output.resolve() == path.resolve() for path in sources.values()
    ):
        raise ContractError("build output must not overwrite source inputs or dataset")
    files = []
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ContractError("Docker context output cannot be a symlink")
    with tarfile.open(output, "w", copybufsize=4 * 1024 * 1024) as archive:
        for relative, path in sorted(sources.items()):
            if path.is_symlink() or (
                not relative.startswith("llm/") and not path.resolve().is_relative_to(root)
            ):
                raise ContractError("source symlink is not permitted in Docker context")
            info = tarfile.TarInfo(relative)
            info.size = path.stat().st_size
            info.mode = 0o555 if relative == "predict.sh" else 0o444
            info.mtime = info.uid = info.gid = 0
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                archive.addfile(info, stream)
            files.append({"path": relative, "bytes": info.size, "sha256": checksum})
    with output.open("rb") as stream:
        context_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "files": files,
        "context_sha256": context_sha256,
        "dataset_included": False,
        "runtime_packages": packages,
        "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
        if model is not None or cuda_runtime is not None
        else None,
        "model_from_cpu_image": cuda_runtime is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--model", type=Path, help="bundle local GGUF and locked installed LLM deps"
    )
    parser.add_argument(
        "--cuda-runtime", type=Path, help="staged CUDA binding, same locked version"
    )
    args = parser.parse_args()
    manifest = build_context(ROOT, args.output, model=args.model, cuda_runtime=args.cuda_runtime)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.manifest, manifest)
    print(f"Docker context: {len(manifest['files'])} allowlisted files; dataset excluded")


if __name__ == "__main__":
    main()
