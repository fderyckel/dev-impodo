"""Local field-level evidence for post-load Odoo reconciliation.

The normal reconciliation projection deliberately contains no business values.
This companion contract is locally access-controlled, hash-verified, and used
only for authorized fallout review artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from impodo.domain.shared.models import (
    canonical_json_bytes,
    portable_value,
    restore_portable_value,
)


RECONCILIATION_DETAIL_CONTRACT_VERSION = 1
MAX_RECONCILIATION_FIELD_DIFFERENCES = 1_000_000
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ReconciliationFieldDifference:
    """One expected/observed field difference with frozen row identity."""

    row_id: str
    dataset: str
    source_row: int
    source_trace_id: str
    target_model: str
    operation: str
    odoo_id: int
    field: str
    expected_value: Any
    observed_value: Any
    field_type: str = ""
    target_digits: tuple[int, int] | None = None
    reason_code: str = "VALUE_DIFFERENT"

    def portable_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "source_trace_id": self.source_trace_id,
            "target_model": self.target_model,
            "operation": self.operation,
            "odoo_id": self.odoo_id,
            "field": self.field,
            "expected_value": portable_value(self.expected_value),
            "observed_value": portable_value(self.observed_value),
            "field_type": self.field_type,
            "target_digits": (
                list(self.target_digits) if self.target_digits is not None else None
            ),
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "ReconciliationFieldDifference":
        raw_digits = payload.get("target_digits")
        digits = None
        if raw_digits is not None:
            if not isinstance(raw_digits, list) or len(raw_digits) != 2:
                raise ValueError("Reconciliation target precision is invalid")
            digits = (int(raw_digits[0]), int(raw_digits[1]))
        return cls(
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            source_trace_id=str(payload["source_trace_id"]),
            target_model=str(payload["target_model"]),
            operation=str(payload["operation"]),
            odoo_id=int(payload["odoo_id"]),
            field=str(payload["field"]),
            expected_value=restore_portable_value(payload.get("expected_value")),
            observed_value=restore_portable_value(payload.get("observed_value")),
            field_type=str(payload.get("field_type", "")),
            target_digits=digits,
            reason_code=str(payload.get("reason_code", "VALUE_DIFFERENT")),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationDetailArtifact:
    """Exact field differences bound to one immutable verification attempt."""

    reconciliation_id: str
    workspace_id: str
    execution_run_id: str
    snapshot_hash: str
    target_hash: str
    differences: tuple[ReconciliationFieldDifference, ...]
    contract_version: int = RECONCILIATION_DETAIL_CONTRACT_VERSION

    @property
    def logical_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(self.portable_dict(include_hash=False))
        ).hexdigest()

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract_version": self.contract_version,
            "reconciliation_id": self.reconciliation_id,
            "workspace_id": self.workspace_id,
            "execution_run_id": self.execution_run_id,
            "snapshot_hash": self.snapshot_hash,
            "target_hash": self.target_hash,
            "differences": [item.portable_dict() for item in self.differences],
        }
        if include_hash:
            payload["logical_hash"] = self.logical_hash
        return payload

    def to_json(self) -> str:
        _validate_detail(self)
        return canonical_json_bytes(self.portable_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "ReconciliationDetailArtifact":
        payload = json.loads(value)
        if (
            int(payload["contract_version"])
            != RECONCILIATION_DETAIL_CONTRACT_VERSION
        ):
            raise ValueError("Reconciliation detail contract is unsupported")
        artifact = cls(
            reconciliation_id=str(payload["reconciliation_id"]),
            workspace_id=str(payload["workspace_id"]),
            execution_run_id=str(payload["execution_run_id"]),
            snapshot_hash=str(payload["snapshot_hash"]),
            target_hash=str(payload["target_hash"]),
            differences=tuple(
                ReconciliationFieldDifference.from_dict(dict(item))
                for item in payload.get("differences", ())
            ),
        )
        if str(payload.get("logical_hash", "")) != artifact.logical_hash:
            raise ValueError("Reconciliation detail hash is invalid")
        _validate_detail(artifact)
        return artifact


@dataclass(frozen=True, slots=True)
class ReconciliationDetailManifest:
    """Value-free integrity binding to one local detail artifact."""

    reconciliation_id: str
    storage_name: str
    logical_hash: str
    artifact_hash: str
    size_bytes: int
    difference_count: int

    def __post_init__(self) -> None:
        if (
            not self.storage_name
            or "/" in self.storage_name
            or "\\" in self.storage_name
            or not self.storage_name.endswith(".json")
            or not _SHA256.fullmatch(self.logical_hash)
            or not _SHA256.fullmatch(self.artifact_hash)
            or self.size_bytes < 1
            or self.difference_count < 0
            or self.difference_count > MAX_RECONCILIATION_FIELD_DIFFERENCES
        ):
            raise ValueError("Reconciliation detail manifest is invalid")


def _validate_detail(artifact: ReconciliationDetailArtifact) -> None:
    if (
        len(artifact.differences) > MAX_RECONCILIATION_FIELD_DIFFERENCES
        or not _SHA256.fullmatch(artifact.snapshot_hash)
        or not _SHA256.fullmatch(artifact.target_hash)
    ):
        raise ValueError("Reconciliation detail binding is invalid")
    identities: set[tuple[str, str]] = set()
    for item in artifact.differences:
        identity = (item.row_id, item.field)
        if (
            identity in identities
            or not item.row_id
            or not item.dataset
            or item.source_row < 1
            or not item.source_trace_id
            or not item.target_model
            or item.odoo_id < 1
            or not item.field
            or len(item.reason_code) > 100
            or (
                item.target_digits is not None
                and (
                    item.target_digits[0] < 1
                    or item.target_digits[1] < 0
                    or item.target_digits[1] > item.target_digits[0]
                )
            )
        ):
            raise ValueError("Reconciliation field difference is invalid")
        identities.add(identity)


__all__ = [
    "ReconciliationDetailArtifact",
    "ReconciliationDetailManifest",
    "ReconciliationFieldDifference",
]
