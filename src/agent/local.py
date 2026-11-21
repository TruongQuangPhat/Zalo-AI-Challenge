"""One persistent local vLLM worker with greedy guided JSON decoding and a hard timeout."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from copy import deepcopy
from pathlib import Path

from src.data.cache import fingerprint
from src.data.catalog import digest
from src.jsonio import ContractError

MAX_MODEL_LEN = 24576  # mapping prompts list every column; code fallback adds rulebook + previews
GPU_MEMORY_UTILIZATION = 0.85
REQUIRED_FILES = ("config.json", "tokenizer.json", "tokenizer_config.json")


def guided_schema(schema: dict) -> dict:
    """Adapt the plan schema for the vLLM xgrammar backend.

    Tuple arrays become draft 2020-12 ``prefixItems``. String ``maxLength`` is dropped:
    xgrammar expands each bound into repetition states, so the plan schema took from
    seconds to over 30 minutes to compile. The plan validator still enforces it after
    parsing, and ``max_tokens`` bounds the output.
    """

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: convert(value) for key, value in node.items() if key != "maxLength"}
        if isinstance(node.get("items"), list):
            result["prefixItems"] = result.pop("items")
            result["items"] = False
        return result

    return convert(deepcopy(schema))


def _worker(
    pipe, model_dir: str, max_model_len: int, gpu_memory_utilization: float, tensor_parallel: int
) -> None:
    # Keep the engine in this process so terminating the worker releases the GPU.
    os.environ["VLLM_ENABLE_V1_MULTIPROCESSING"] = "0"
    os.environ.setdefault("VLLM_NO_USAGE_STATS", "1")
    try:
        import torch
        import vllm
        from vllm import LLM, SamplingParams
        from vllm.sampling_params import GuidedDecodingParams

        started = time.monotonic()
        model = LLM(
            model=model_dir,
            tokenizer=model_dir,
            dtype="auto",
            seed=0,
            max_model_len=max_model_len,
            tensor_parallel_size=tensor_parallel,
            gpu_memory_utilization=gpu_memory_utilization,
            generation_config="vllm",
            trust_remote_code=False,
            disable_log_stats=True,
        )
        pipe.send(
            {
                "ready": True,
                "load_seconds": time.monotonic() - started,
                "runtime": {"vllm": vllm.__version__, "torch": torch.__version__},
                "cuda": torch.version.cuda,
                "device": torch.cuda.get_device_name(0),
                "compute_capability": list(torch.cuda.get_device_capability(0)),
            }
        )
        while True:
            request = pipe.recv()
            if request is None:
                break
            if request.get("free"):
                # Free-text samples (code fallback): no grammar, n samples in one batch.
                try:
                    started = time.monotonic()
                    params = SamplingParams(
                        n=request.get("n", 1),
                        temperature=request.get("temperature", 0.0),
                        top_p=0.95 if request.get("temperature") else 1.0,
                        seed=request.get("seed", 0),
                        max_tokens=request["max_tokens"],
                    )
                    result = model.chat(
                        request["messages"],
                        params,
                        use_tqdm=False,
                        chat_template_kwargs={"enable_thinking": bool(request.get("thinking"))},
                    )[0]
                    pipe.send(
                        {
                            "texts": [o.text for o in result.outputs],
                            "finish_reasons": [o.finish_reason for o in result.outputs],
                            "usage": {
                                "prompt_tokens": len(result.prompt_token_ids or []),
                                "completion_tokens": sum(len(o.token_ids) for o in result.outputs),
                            },
                            "seconds": time.monotonic() - started,
                        }
                    )
                except Exception as exc:
                    pipe.send({"request_error": f"{type(exc).__name__}: {exc}"})
                continue
            try:
                started = time.monotonic()
                # Two aggregates plus refs need more JSON than the S1 scalar draft.
                token_budget = request.get("max_tokens") or (
                    384 if request["schema"]["properties"]["aggregations"]["maxItems"] > 1 else 256
                )
                params = SamplingParams(
                    temperature=0,
                    seed=0,
                    max_tokens=token_budget,
                    guided_decoding=GuidedDecodingParams(json=guided_schema(request["schema"])),
                )
                # Qwen3 templates think by default, which the JSON grammar forbids; other
                # templates ignore the unknown variable.
                result = model.chat(
                    request["messages"],
                    params,
                    use_tqdm=False,
                    chat_template_kwargs={"enable_thinking": False},
                )[0]
                completion = result.outputs[0]
                prompt_tokens = len(result.prompt_token_ids or [])
                pipe.send(
                    {
                        "text": completion.text,
                        "finish_reason": completion.finish_reason,
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": len(completion.token_ids),
                            "total_tokens": prompt_tokens + len(completion.token_ids),
                        },
                        "output_token_budget": token_budget,
                        "seconds": time.monotonic() - started,
                    }
                )
            except Exception as exc:  # one bad request (e.g. prompt too long) keeps the engine
                pipe.send({"request_error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:
        pipe.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        pipe.close()


def model_identity(path: Path) -> dict:
    """Hash every packaged model file; ``sha256`` makes runs on these weights resumable."""
    if (
        not path.is_dir()
        or path.is_symlink()
        or not all((path / name).is_file() for name in REQUIRED_FILES)
        or not list(path.glob("*.safetensors"))
    ):
        raise ContractError("provide an existing local model directory with safetensors")
    files = {p.name: digest(p) for p in sorted(path.iterdir()) if p.is_file()}
    return {"directory": path.name, "files": files, "sha256": fingerprint(files)}


class LocalModel:
    """Packaged weights only: no model ID, downloader, HTTP client or remote code."""

    def __init__(
        self,
        path: Path,
        *,
        load_timeout: float = 900,
        tensor_parallel: int = 1,
        max_model_len: int = MAX_MODEL_LEN,
    ):
        started = time.monotonic()
        identity = model_identity(path)
        if load_timeout <= 0 or tensor_parallel < 1:
            raise ContractError("model load timeout and tensor parallel size must be positive")
        self.metadata = {
            **identity,
            "backend": "vllm",
            "load_timeout_seconds": load_timeout,
            "max_model_len": max_model_len,
            "gpu_memory_utilization": GPU_MEMORY_UTILIZATION,
            "max_output_tokens": 256,
            "max_difference_output_tokens": 384,
            "seed": 0,
            "temperature": 0,
            "guided_decoding": "json_schema",
            "tensor_parallel_size": tensor_parallel,
            "enable_thinking": False,
        }
        mp = multiprocessing.get_context("spawn")
        self.pipe, child = mp.Pipe()
        self.process = mp.Process(
            target=_worker,
            args=(
                child,
                str(path.resolve()),
                max_model_len,
                GPU_MEMORY_UTILIZATION,
                tensor_parallel,
            ),
        )
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
        if "request_error" in result:
            raise ContractError(result["request_error"])
        return result

    def generate(
        self, messages: list[dict], schema: dict, timeout: float, *, max_tokens: int | None = None
    ) -> dict:
        if not self.process.is_alive():
            raise ContractError("local_model_worker_unavailable")
        self.pipe.send({"messages": messages, "schema": schema, "max_tokens": max_tokens})
        return self._receive(timeout)

    def sample(
        self,
        messages: list[dict],
        timeout: float,
        *,
        n: int,
        temperature: float,
        max_tokens: int,
        thinking: bool = False,
    ) -> dict:
        """n free-text completions of one prompt (no JSON grammar)."""
        if not self.process.is_alive():
            raise ContractError("local_model_worker_unavailable")
        self.pipe.send(
            {
                "free": True,
                "messages": messages,
                "n": n,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": thinking,
            }
        )
        return self._receive(timeout)

    def close(self) -> None:
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)
        self.pipe.close()


def output_budget(schema: dict) -> int:
    """Generous token bound for a JSON object whose keys are echoed back (column names,
    status values): about one token per two characters plus label and punctuation."""

    def walk(node) -> int:
        if not isinstance(node, dict):
            return 0
        properties = node.get("properties") or {}
        return sum(len(key) // 2 + 16 + walk(value) for key, value in properties.items())

    return 256 + walk(schema)


class MappingProposer:
    """Column-meaning proposals (``llm_mapping``) from the already loaded planner model."""

    def __init__(self, model: LocalModel, *, timeout: float = 600):
        self.model, self.timeout = model, timeout
        self.model_id = model.metadata["sha256"]
        self.calls: list[dict] = []

    def __call__(self, prompt: str, schema: dict) -> dict:
        result = self.model.generate(
            [{"role": "user", "content": prompt}],
            schema,
            self.timeout,
            max_tokens=output_budget(schema),
        )
        self.calls.append({k: v for k, v in result.items() if k != "text"})
        if result["finish_reason"] != "stop":
            raise ContractError(f"mapping proposal incomplete: {result['finish_reason']}")
        try:
            return json.loads(result["text"])
        except ValueError as exc:
            raise ContractError("mapping proposal is not JSON") from exc
