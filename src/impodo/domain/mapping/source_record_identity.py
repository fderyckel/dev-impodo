"""Keep portable record identity independent of ordinary mapped field values.

The source key is complete and ordered. Its namespace belongs to the saved
mapping and Recipe, rather than a file edition or a workspace. External IDs
are derived from canonical text and can therefore be reproduced by the native
preparation projector without an Odoo or database call per record.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

SOURCE_EXTERNAL_IDS_FIELD = "__impodo_source_external_ids"
_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_EXTERNAL_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


@dataclass(frozen=True, slots=True)
class SourceRecordIdentity:
    """Record the explicit source namespace and permission for new identities.

    An optional source column can supply an existing Odoo External ID for an
    initial reconciliation. Its nonblank values must already exist in Odoo;
    they never authorise a create, even when new generated IDs are permitted.
    """

    namespace: str
    allow_new_records: bool = False
    existing_external_id_column_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not _NAMESPACE.fullmatch(self.namespace):
            raise ValueError("Use a stable identity name starting with a letter, followed by letters, digits or underscores (up to 48 characters)")
        if type(self.allow_new_records) is not bool:
            raise ValueError("The decision to create new source records must be explicit")
        if self.existing_external_id_column_key is not None and (
            not isinstance(self.existing_external_id_column_key, str)
            or not self.existing_external_id_column_key.strip()
            or len(self.existing_external_id_column_key) > 500
        ):
            raise ValueError("Choose one source column containing existing Odoo External IDs")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceRecordIdentity:
        """Restore the closed policy shape without discarding unknown settings."""

        if set(payload) != {"namespace", "allow_new_records", "existing_external_id_column_key"}:
            raise ValueError("Source record identity settings are invalid")
        return cls(**payload)


def valid_external_id(value: str) -> bool:
    """Accept a bounded Odoo External ID rather than an executable expression."""

    return bool(len(value) <= 255 and _EXTERNAL_ID.fullmatch(value))


def source_record_external_id(namespace: str, model: str, source_key: Sequence[object]) -> str:
    """Derive one stable ID without depending on file order or field values."""

    if not source_key or any(value is None or not str(value).strip() for value in source_key):
        raise ValueError("Every source record identity column must have a value")
    values = [model, *(str(value).strip() for value in source_key)]
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = re.sub(r"[^a-z0-9_]+", "_", model.casefold())[:36]
    return f"impodo_source_{namespace}.{token}_{sha256(encoded).hexdigest()}"


def source_record_external_ids(values: Mapping[str, Any]) -> tuple[str, ...]:
    """Return captured aliases without interpreting a numeric Odoo record ID."""

    raw = values.get(SOURCE_EXTERNAL_IDS_FIELD, ())
    if not isinstance(raw, (tuple, list)) or any(not isinstance(item, str) or not valid_external_id(item) for item in raw):
        raise ValueError("Captured source record External IDs are invalid")
    return tuple(raw)
