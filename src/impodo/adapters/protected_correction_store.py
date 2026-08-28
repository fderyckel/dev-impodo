"""Store correction plans and confirmations as protected Project evidence."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanError,
)

from .protected_project_evidence_store import ProtectedProjectEvidenceStore


@dataclass(frozen=True, slots=True)
class StoredCorrectionPlan:
    project_id: str
    plan_id: str
    plan_hash: str
    storage_key: str
    size_bytes: int
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class StoredCorrectionConfirmation:
    project_id: str
    confirmation_id: str
    confirmation_hash: str
    storage_key: str
    size_bytes: int
    artifact_hash: str


class ProtectedCorrectionStore:
    """Typed correction facade over the existing authenticated evidence store."""

    def __init__(self, evidence: ProtectedProjectEvidenceStore) -> None:
        self._evidence = evidence

    def put_plan(self, plan: CorrectionPlan) -> StoredCorrectionPlan:
        stored = self._evidence.put_artifact(
            plan.project_id,
            artifact_kind="correction-plans",
            artifact_id=plan.plan_id,
            logical_hash=plan.plan_hash,
            payload=plan.protected_json(),
        )
        return StoredCorrectionPlan(
            project_id=plan.project_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            storage_key=stored.storage_key,
            size_bytes=stored.size_bytes,
            artifact_hash=stored.artifact_hash,
        )

    def read_plan(self, reference: StoredCorrectionPlan) -> CorrectionPlan:
        payload = self._evidence.read(
            reference.project_id,
            storage_key=reference.storage_key,
            logical_hash=reference.plan_hash,
            expected_artifact_hash=reference.artifact_hash,
        )
        plan = CorrectionPlan.from_protected_json(payload)
        if (
            plan.project_id != reference.project_id
            or plan.plan_id != reference.plan_id
            or plan.plan_hash != reference.plan_hash
        ):
            raise CorrectionPlanError(
                "Stored correction plan does not match its reference"
            )
        return plan

    def put_confirmation(
        self,
        plan: CorrectionPlan,
        confirmation: CorrectionConfirmation,
    ) -> StoredCorrectionConfirmation:
        if (
            confirmation.plan_id != plan.plan_id
            or confirmation.plan_hash != plan.plan_hash
            or confirmation.target_hash != plan.target_hash
        ):
            raise CorrectionPlanError(
                "Correction confirmation does not match its protected plan"
            )
        stored = self._evidence.put_artifact(
            plan.project_id,
            artifact_kind="correction-confirmations",
            artifact_id=confirmation.confirmation_id,
            logical_hash=confirmation.confirmation_hash,
            payload=confirmation.protected_json(),
        )
        return StoredCorrectionConfirmation(
            project_id=plan.project_id,
            confirmation_id=confirmation.confirmation_id,
            confirmation_hash=confirmation.confirmation_hash,
            storage_key=stored.storage_key,
            size_bytes=stored.size_bytes,
            artifact_hash=stored.artifact_hash,
        )

    def read_confirmation(
        self,
        reference: StoredCorrectionConfirmation,
    ) -> CorrectionConfirmation:
        payload = self._evidence.read(
            reference.project_id,
            storage_key=reference.storage_key,
            logical_hash=reference.confirmation_hash,
            expected_artifact_hash=reference.artifact_hash,
        )
        confirmation = CorrectionConfirmation.from_protected_json(payload)
        if (
            confirmation.confirmation_id != reference.confirmation_id
            or confirmation.confirmation_hash != reference.confirmation_hash
        ):
            raise CorrectionPlanError(
                "Stored correction confirmation does not match its reference"
            )
        return confirmation


__all__ = [
    "ProtectedCorrectionStore",
    "StoredCorrectionConfirmation",
    "StoredCorrectionPlan",
]
