"""Strict JSON with exact decimal reading and finite JSON-number writing."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    """An input or output does not meet the documented contract."""

    def __init__(self, message: str, *, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ContractError(f"non-finite JSON constant: {value}")


def loads(text: str) -> Any:
    try:
        return json.loads(
            text, parse_float=Decimal, parse_constant=_constant, object_pairs_hook=_pairs
        )
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON at column {exc.colno}: {exc.msg}") from exc


def dumps(value: Any) -> str:
    """Emit Decimal as a number, never as a quoted string or approximate float."""
    if value is None or isinstance(value, (str, bool, int)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError("non-finite Decimal")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("non-finite float")
        return json.dumps(value, allow_nan=False)
    if isinstance(value, list):
        return "[" + ",".join(dumps(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ContractError("JSON object keys must be strings")
        return "{" + ",".join(dumps(k) + ":" + dumps(v) for k, v in value.items()) + "}"
    raise ContractError(f"unsupported JSON value type: {type(value).__name__}")


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ContractError(f"{path.name}:{line_number}: blank JSONL line")
            try:
                obj = loads(line)
                if not isinstance(obj, dict):
                    raise ContractError("JSONL record must be an object")
            except ContractError as exc:
                raise ContractError(f"{path.name}:{line_number}: {exc}") from exc
            yield line_number, obj


def write_json(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise ContractError(f"refusing symlink output: {path}")
    path.write_text(dumps(value) + "\n", encoding="utf-8")
