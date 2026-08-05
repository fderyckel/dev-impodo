"""Deterministic JSON and hashes for immutable domain evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Mapping


def portable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): portable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [portable(item) for item in value]
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def canonical_json(payload: object) -> str:
    return json.dumps(
        portable(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(payload: object) -> str:
    return "sha256:" + sha256(canonical_json(payload).encode("utf-8")).hexdigest()
