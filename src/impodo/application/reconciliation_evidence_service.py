"""Store and verify exact field-level load-reconciliation evidence locally."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.application.workspace.access import WorkspaceAccessService
from impodo.domain.reconciliation import ReconciliationRun
from impodo.domain.reconciliation_detail import (
    ReconciliationDetailArtifact,
    ReconciliationDetailManifest,
)
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.errors import WorkspaceError


RECONCILIATION_DETAIL_STORAGE_NAME = "fallout-detail.json"


@dataclass(frozen=True, slots=True)
class ReconciliationDetailCandidate:
    """Plain local JSON plus its value-free integrity manifest."""

    manifest: ReconciliationDetailManifest
    content: bytes


class ReconciliationEvidenceService:
    """Keep local reconciliation detail access-controlled and hash-verified."""

    def __init__(
        self,
        authorization: WorkspaceAccessService,
        artifacts: WorkspaceArtifactStore,
    ) -> None:
        self._authorization = authorization
        self._artifacts = artifacts

    def prepare(
        self,
        report: ReconciliationRun,
        detail: ReconciliationDetailArtifact,
        *,
        actor: Actor,
    ) -> ReconciliationDetailCandidate:
        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_MANAGE,
            workspace_id=report.workspace_id,
        )
        _require_detail_binding(report, detail)
        content = detail.to_json().encode("utf-8")
        return ReconciliationDetailCandidate(
            manifest=ReconciliationDetailManifest(
                reconciliation_id=report.reconciliation_id,
                storage_name=RECONCILIATION_DETAIL_STORAGE_NAME,
                logical_hash=detail.logical_hash,
                artifact_hash="sha256:" + sha256(content).hexdigest(),
                size_bytes=len(content),
                difference_count=len(detail.differences),
            ),
            content=content,
        )

    def publish(
        self,
        report: ReconciliationRun,
        candidate: ReconciliationDetailCandidate,
    ) -> None:
        if candidate.manifest.reconciliation_id != report.reconciliation_id:
            raise WorkspaceError("Fallout detail belongs to another result")
        self._artifacts.write_report(
            report.workspace_id,
            report.reconciliation_id,
            candidate.manifest.storage_name,
            candidate.content,
        )

    def delete(self, report: ReconciliationRun) -> None:
        self._artifacts.delete_report(
            report.workspace_id,
            report.reconciliation_id,
            RECONCILIATION_DETAIL_STORAGE_NAME,
        )

    def open(
        self,
        report: ReconciliationRun,
        manifest: ReconciliationDetailManifest,
        *,
        actor: Actor,
    ) -> ReconciliationDetailArtifact:
        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            workspace_id=report.workspace_id,
        )
        if manifest.reconciliation_id != report.reconciliation_id:
            raise WorkspaceError("Fallout detail belongs to another result")
        with self._artifacts.materialize_report(
            report.workspace_id,
            report.reconciliation_id,
            manifest.storage_name,
        ) as path:
            content = path.read_bytes()
        if len(content) != manifest.size_bytes:
            raise WorkspaceError("Fallout detail size changed")
        if "sha256:" + sha256(content).hexdigest() != manifest.artifact_hash:
            raise WorkspaceError("Fallout detail hash changed")
        try:
            detail = ReconciliationDetailArtifact.from_json(content.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError) as error:
            raise WorkspaceError("Fallout detail is invalid") from error
        _require_detail_binding(report, detail)
        if (
            detail.logical_hash != manifest.logical_hash
            or len(detail.differences) != manifest.difference_count
        ):
            raise WorkspaceError("Fallout detail binding changed")
        return detail


def _require_detail_binding(
    report: ReconciliationRun,
    detail: ReconciliationDetailArtifact,
) -> None:
    if (
        detail.reconciliation_id != report.reconciliation_id
        or detail.workspace_id != report.workspace_id
        or detail.execution_run_id != report.execution_run_id
        or detail.snapshot_hash != report.snapshot_hash
        or detail.target_hash != report.target_hash
    ):
        raise WorkspaceError("Fallout detail does not match verification")


__all__ = [
    "RECONCILIATION_DETAIL_STORAGE_NAME",
    "ReconciliationDetailCandidate",
    "ReconciliationEvidenceService",
]
