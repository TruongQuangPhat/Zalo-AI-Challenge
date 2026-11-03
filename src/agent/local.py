"""One persistent CPU llama.cpp worker with local-only loading and a hard timeout."""

from __future__ import annotations

import importlib.metadata
import multiprocessing
import resource
import time
from pathlib import Path

from src.data.catalog import digest
from src.jsonio import ContractError


def _worker(pipe, model_path: str, n_ctx: int, threads: int) -> None:
    # Import only inside the worker: mock/CPU tooling never requires the LLM group.
    try:
        from llama_cpp import Llama, llama_perf_context, llama_perf_context_reset

        started = time.monotonic()
        model = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_batch=128,
            n_threads=threads,
            n_threads_batch=threads,
            n_gpu_layers=0,
            seed=0,
            chat_format="chatml",
            verbose=False,
        )
        pipe.send(
            {
                "ready": True,
                "load_seconds": time.monotonic() - started,
                "runtime": importlib.metadata.version("llama-cpp-python"),
                "batch_threads": model.n_threads_batch,
                "n_batch": model.n_batch,
                "gguf": {
                    key: model.metadata.get(key)
                    for key in (
                        "general.name",
                        "general.architecture",
                        "general.file_type",
                    )
                },
                "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            }
        )
        while True:
            request = pipe.recv()
            if request is None:
                break
            started = time.monotonic()
            llama_perf_context_reset(model.ctx)
            result = model.create_chat_completion(
                messages=request["messages"],
                temperature=0,
                seed=0,
                max_tokens=256,
                response_format={"type": "json_object", "schema": request["schema"]},
            )
            perf = llama_perf_context(model.ctx)
            pipe.send(
                {
                    "text": result["choices"][0]["message"]["content"],
                    "finish_reason": result["choices"][0]["finish_reason"],
                    "usage": result["usage"],
                    "timings": {
                        "prompt_seconds": perf.t_p_eval_ms / 1000,
                        "decode_seconds": perf.t_eval_ms / 1000,
                        "prompt_tokens_evaluated": perf.n_p_eval,
                        "tokens_decoded": perf.n_eval,
                    },
                    "seconds": time.monotonic() - started,
                    "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                }
            )
        model.close()
    except Exception as exc:
        pipe.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        pipe.close()


class LocalModel:
    """No model ID, downloader, HTTP client, remote code, or auto GPU fallback."""

    def __init__(self, path: Path, *, load_timeout: float = 180, threads: int = 4):
        started = time.monotonic()
        if not path.is_file() or path.is_symlink() or path.suffix.lower() != ".gguf":
            raise ContractError("provide an existing local GGUF file")
        if threads < 1 or load_timeout <= 0:
            raise ContractError("threads and model load timeout must be positive")
        self.metadata = {
            "file": path.name,
            "sha256": digest(path),
            "bytes": path.stat().st_size,
            "backend": "llama-cpp-python",
            "device": "cpu",
            "n_ctx": 4096,
            "max_output_tokens": 256,
            "threads": threads,
            "seed": 0,
            "temperature": 0,
            "chat_format": "chatml",
        }
        mp = multiprocessing.get_context("spawn")
        self.pipe, child = mp.Pipe()
        self.process = mp.Process(target=_worker, args=(child, str(path.resolve()), 4096, threads))
        self.process.start()
        child.close()
        try:
            self.metadata.update(self._receive(load_timeout))
            self.metadata["startup_seconds_including_hash"] = time.monotonic() - started
        except BaseException:
            self.close()
            raise

    def _receive(self, timeout: float) -> dict:
        if timeout <= 0 or not self.pipe.poll(timeout):
            self.close()
            raise ContractError("local_model_timeout; worker stopped")
        try:
            result = self.pipe.recv()
        except (EOFError, OSError) as exc:
            self.close()
            raise ContractError("local_model_worker_exited") from exc
        if "error" in result:
            self.close()
            raise ContractError(result["error"])
        return result

    def generate(self, messages: list[dict], schema: dict, timeout: float) -> dict:
        if not self.process.is_alive():
            raise ContractError("local_model_worker_unavailable")
        self.pipe.send({"messages": messages, "schema": schema})
        return self._receive(timeout)

    def close(self) -> None:
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)
        self.pipe.close()
