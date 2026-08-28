"""Store correction plans and confirmations as protected Project evidence."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanError,
)
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
    CorrectionTargetIndex,
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
class StoredCorrectionTargetIndex:
    project_id: str
    index_id: str
    index_hash: str
    storage_key: str
    size_bytes: int
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class StoredCorrectionOrigin:
    project_id: str
    manifest_id: str
    manifest_hash: str
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

    def put_target_index(
        self,
        index: CorrectionTargetIndex,
    ) -> StoredCorrectionTargetIndex:
        stored = self._evidence.put_artifact(
            index.project_id,
            artifact_kind="correction-target-indexes",
            artifact_id=index.index_id,
            logical_hash=index.index_hash,
            payload=index.protected_json(),
        )
        return StoredCorrectionTargetIndex(
            project_id=index.project_id,
            index_id=index.index_id,
            index_hash=index.index_hash,
            storage_key=stored.storage_key,
            size_bytes=stored.size_bytes,
            artifact_hash=stored.artifact_hash,
        )

    def read_target_index(
        self,
        reference: StoredCorrectionTargetIndex,
    ) -> CorrectionTargetIndex:
        payload = self._evidence.read(
            reference.project_id,
            storage_key=reference.storage_key,
            logical_hash=reference.index_hash,
            expected_artifact_hash=reference.artifact_hash,
        )
        index = CorrectionTargetIndex.from_protected_json(payload)
        if (
            index.project_id != reference.project_id
            or index.index_id != reference.index_id
            or index.index_hash != reference.index_hash
        ):
            raise CorrectionOriginError(
                "Stored correction target index does not match its reference"
            )
        return index

    def put_origin(
        self,
        manifest: CorrectionOriginManifest,
    ) -> StoredCorrectionOrigin:
        stored = self._evidence.put_artifact(
            manifest.project_id,
            artifact_kind="correction-origins",
            artifact_id=manifest.manifest_id,
            logical_hash=manifest.manifest_hash,
            payload=manifest.protected_json(),
        )
        return StoredCorrectionOrigin(
            project_id=manifest.project_id,
            manifest_id=manifest.manifest_id,
            manifest_hash=manifest.manifest_hash,
            storage_key=stored.storage_key,
            size_bytes=stored.size_bytes,
            artifact_hash=stored.artifact_hash,
        )

    def read_origin(
        self,
        reference: StoredCorrectionOrigin,
    ) -> CorrectionOriginManifest:
        payload = self._evidence.read(
            reference.project_id,
            storage_key=reference.storage_key,
            logical_hash=reference.manifest_hash,
            expected_artifact_hash=reference.artifact_hash,
        )
        manifest = CorrectionOriginManifest.from_protected_json(payload)
        if (
            manifest.project_id != reference.project_id
            or manifest.manifest_id != reference.manifest_id
            or manifest.manifest_hash != reference.manifest_hash
        ):
            raise CorrectionOriginError(
                "Stored correction origin does not match its reference"
            )
        return manifest

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
    "StoredCorrectionOrigin",
    "StoredCorrectionPlan",
    "StoredCorrectionTargetIndex",
]
