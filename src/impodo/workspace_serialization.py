"""Canonical serialization shared by workspace evidence contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json


def canonical_json(payload: object) -> str:
    def default(value: object):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, StrEnum):
            return value.value
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    return json.dumps(
        payload,
        default=default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(payload: object) -> str:
    serialized = canonical_json(payload).encode("utf-8")
    return "sha256:" + sha256(serialized).hexdigest()
