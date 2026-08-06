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


class CanonicalJsonObjectHasher:
    """Incrementally hash one canonical JSON object.

    Large evidence collections can feed already encoded array items one at a
    time.  The emitted byte sequence is identical to ``canonical_json`` for an
    object whose keys are supplied in sorted order, without constructing the
    complete document in memory.
    """

    __slots__ = (
        "_digest",
        "_finished",
        "_first_item",
        "_in_array",
        "_last_key",
        "_array_first_item",
    )

    def __init__(self) -> None:
        self._digest = sha256()
        self._digest.update(b"{")
        self._finished = False
        self._first_item = True
        self._in_array = False
        self._last_key: str | None = None
        self._array_first_item = True

    def add_value(self, key: str, value: object) -> None:
        """Add one ordinary value under the next lexicographically sorted key."""

        self._start_key(key)
        self._digest.update(canonical_json(value).encode("utf-8"))

    def start_array(self, key: str) -> None:
        """Start an array whose encoded items will be supplied incrementally."""

        self._start_key(key)
        self._digest.update(b"[")
        self._in_array = True
        self._array_first_item = True

    def add_encoded_array_item(self, encoded: str | bytes) -> None:
        """Hash one complete canonical JSON value inside the current array."""

        if self._finished or not self._in_array:
            raise RuntimeError("canonical JSON array is not open")
        if not self._array_first_item:
            self._digest.update(b",")
        self._digest.update(encoded.encode("utf-8") if isinstance(encoded, str) else encoded)
        self._array_first_item = False

    def end_array(self) -> None:
        if self._finished or not self._in_array:
            raise RuntimeError("canonical JSON array is not open")
        self._digest.update(b"]")
        self._in_array = False

    def finish(self) -> str:
        """Close the object and return its deterministic SHA-256 identifier."""

        if self._finished:
            raise RuntimeError("canonical JSON object is already finished")
        if self._in_array:
            raise RuntimeError("canonical JSON array is still open")
        self._digest.update(b"}")
        self._finished = True
        return "sha256:" + self._digest.hexdigest()

    def _start_key(self, key: str) -> None:
        if self._finished or self._in_array:
            raise RuntimeError("canonical JSON object cannot accept this key")
        if self._last_key is not None and key <= self._last_key:
            raise ValueError("canonical JSON object keys must be unique and sorted")
        if not self._first_item:
            self._digest.update(b",")
        self._digest.update(canonical_json(key).encode("utf-8"))
        self._digest.update(b":")
        self._first_item = False
        self._last_key = key
