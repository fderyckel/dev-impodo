"""Portable row outcomes from practical post-write Odoo read-back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import re

from ..models import canonical_json_bytes


RECONCILIATION_CONTRACT_VERSION = 3
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class ReconciliationRunStatus(StrEnum):
    VERIFIED = "VERIFIED"
    FALLOUT = "FALLOUT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ReconciliationRowStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_WRITTEN = "NOT_WRITTEN"
    NOT_APPLIED = "NOT_APPLIED"
    MISSING = "MISSING"
    DIFFERENT = "DIFFERENT"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    """One write candidate's read-back result without business values."""

    row_id: str
    dataset: str
    source_row: int
    target_model: str
    operation: str
    execution_status: str
    status: ReconciliationRowStatus
    odoo_id: int | None = None
    differing_fields: tuple[str, ...] = ()
    message: str = ""
    retry_safe: bool = False

    def portable_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "target_model": self.target_model,
            "operation": self.operation,
            "execution_status": self.execution_status,
            "status": self.status.value,
            "odoo_id": self.odoo_id,
            "differing_fields": list(self.differing_fields),
            "message": self.message,
            "retry_safe": self.retry_safe,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ReconciliationRow":
        return cls(
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            target_model=str(payload["target_model"]),
            operation=str(payload["operation"]),
            execution_status=str(payload["execution_status"]),
            status=ReconciliationRowStatus(str(payload["status"])),
            odoo_id=(
                int(payload["odoo_id"])
                if payload.get("odoo_id") is not None
                else None
            ),
            differing_fields=tuple(
                str(item) for item in payload.get("differing_fields", ())
            ),
            message=str(payload.get("message", "")),
            retry_safe=bool(payload.get("retry_safe", False)),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    """Hash-bound result of reading one completed execution run back."""

    reconciliation_id: str
    workspace_id: str
    execution_run_id: str
    snapshot_hash: str
    target_hash: str
    target_database: str
    status: ReconciliationRunStatus
    verified_at: datetime
    verified_by: str
    unchanged_count: int
    rows: tuple[ReconciliationRow, ...]
    verification_credential_binding_hash: str = ""
    verification_principal_hash: str = ""
    verification_permission_hash: str = ""
    verification_context_hash: str = ""
    contract_version: int = RECONCILIATION_CONTRACT_VERSION

    @property
    def verified_write_count(self) -> int:
        return sum(
            item.status is ReconciliationRowStatus.VERIFIED for item in self.rows
        )

    @property
    def verified_count(self) -> int:
        return self.unchanged_count + self.verified_write_count

    @property
    def unknown_count(self) -> int:
        return sum(
            item.status is ReconciliationRowStatus.OUTCOME_UNKNOWN
            for item in self.rows
        )

    @property
    def retry_safe_count(self) -> int:
        return sum(item.retry_safe for item in self.rows)

    @property
    def fallout_count(self) -> int:
        return sum(
            item.status is not ReconciliationRowStatus.VERIFIED
            for item in self.rows
        )

    @property
    def total_count(self) -> int:
        return self.unchanged_count + len(self.rows)

    @property
    def semantic_hash(self) -> str:
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
            "target_database": self.target_database,
            "status": self.status.value,
            "verified_at": self.verified_at.isoformat(),
            "verified_by": self.verified_by,
            "unchanged_count": self.unchanged_count,
            "verification_credential_binding_hash": (
                self.verification_credential_binding_hash
            ),
            "verification_principal_hash": self.verification_principal_hash,
            "verification_permission_hash": self.verification_permission_hash,
            "verification_context_hash": self.verification_context_hash,
            "rows": [item.portable_dict() for item in self.rows],
        }
        if include_hash:
            payload["semantic_hash"] = self.semantic_hash
        return payload

    def to_json(self) -> str:
        return canonical_json_bytes(self.portable_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "ReconciliationRun":
        payload = json.loads(value)
        if int(payload["contract_version"]) != RECONCILIATION_CONTRACT_VERSION:
            raise ValueError("Reconciliation contract version is unsupported")
        run = cls(
            reconciliation_id=str(payload["reconciliation_id"]),
            workspace_id=str(payload["workspace_id"]),
            execution_run_id=str(payload["execution_run_id"]),
            snapshot_hash=str(payload["snapshot_hash"]),
            target_hash=str(payload["target_hash"]),
            target_database=str(payload["target_database"]),
            status=ReconciliationRunStatus(str(payload["status"])),
            verified_at=datetime.fromisoformat(str(payload["verified_at"])),
            verified_by=str(payload["verified_by"]),
            unchanged_count=int(payload["unchanged_count"]),
            verification_credential_binding_hash=str(
                payload["verification_credential_binding_hash"]
            ),
            verification_principal_hash=str(
                payload["verification_principal_hash"]
            ),
            verification_permission_hash=str(
                payload["verification_permission_hash"]
            ),
            verification_context_hash=str(
                payload["verification_context_hash"]
            ),
            rows=tuple(
                ReconciliationRow.from_dict(dict(item))
                for item in payload.get("rows", ())
            ),
        )
        if str(payload.get("semantic_hash", "")) != run.semantic_hash:
            raise ValueError("Reconciliation result hash is invalid")
        _validate_run(run)
        return run


def _validate_run(run: ReconciliationRun) -> None:
    if run.unchanged_count < 0 or len({item.row_id for item in run.rows}) != len(
        run.rows
    ):
        raise ValueError("Reconciliation row accounting is invalid")
    if any(
        item.source_row < 1
        or len(item.message) > 500
        or len(set(item.differing_fields)) != len(item.differing_fields)
        or (item.retry_safe and item.status is not ReconciliationRowStatus.NOT_APPLIED)
        for item in run.rows
    ):
        raise ValueError("Reconciliation row result is invalid")
    expected = (
        ReconciliationRunStatus.OUTCOME_UNKNOWN
        if run.unknown_count
        else (
            ReconciliationRunStatus.FALLOUT
            if run.fallout_count
            else ReconciliationRunStatus.VERIFIED
        )
    )
    if run.status is not expected:
        raise ValueError("Reconciliation run status is invalid")
    verification_hashes = (
        run.verification_credential_binding_hash,
        run.verification_principal_hash,
        run.verification_permission_hash,
        run.verification_context_hash,
    )
    if any(verification_hashes) and not all(
        _SHA256.fullmatch(value) for value in verification_hashes
    ):
        raise ValueError("Reconciliation credential evidence is invalid")
