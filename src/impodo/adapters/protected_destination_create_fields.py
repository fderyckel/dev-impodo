"""Encrypted exact Odoo defaults for destination create-field review."""

from __future__ import annotations

from dataclasses import replace

from impodo.domain.workspace.destination_matching import (
    DestinationCreateFieldEvidence,
    DestinationMatchPlan,
)
from impodo.domain.workspace.errors import WorkspaceError

from .protected_project_evidence_store import ProtectedProjectEvidenceStore


_KIND = "destination-create-fields"


class ProtectedDestinationCreateFieldStore:
    """Keep target-bound default values outside portable workspace plans."""

    def __init__(self, evidence: ProtectedProjectEvidenceStore) -> None:
        self._evidence = evidence

    def put(self, project_id: str, plan: DestinationMatchPlan) -> DestinationMatchPlan:
        protected = plan.create_field_evidence
        if protected is None:
            if plan.create_field_evidence_id is not None:
                raise WorkspaceError("Destination create-field evidence is missing")
            return plan
        try:
            stored = self._evidence.put_artifact(
                project_id,
                artifact_kind=_KIND,
                artifact_id=protected.evidence_id,
                logical_hash=protected.content_hash,
                payload=protected.to_json().encode("utf-8"),
            )
            return replace(
                plan,
                protected_create_field_artifact_hash=stored.artifact_hash,
                create_field_evidence=None,
            )
        except Exception as error:
            raise WorkspaceError(
                "The protected destination defaults could not be saved. Check matching again."
            ) from error

    def read(
        self,
        project_id: str,
        plan: DestinationMatchPlan,
    ) -> DestinationCreateFieldEvidence | None:
        if plan.create_field_evidence_id is None:
            return None
        if (
            plan.create_field_evidence_id is None
            or plan.create_field_evidence_hash is None
            or plan.protected_create_field_artifact_hash is None
        ):
            raise WorkspaceError(
                "Protected destination defaults are unavailable. Check matching again."
            )
        try:
            storage_key = self._evidence.artifact_storage_key(
                project_id,
                artifact_kind=_KIND,
                artifact_id=plan.create_field_evidence_id,
                logical_hash=plan.create_field_evidence_hash,
            )
            raw = self._evidence.read(
                project_id,
                storage_key=storage_key,
                logical_hash=plan.create_field_evidence_hash,
                expected_artifact_hash=plan.protected_create_field_artifact_hash,
            )
            protected = DestinationCreateFieldEvidence.from_json(raw.decode("utf-8"))
            if (
                protected.evidence_id != plan.create_field_evidence_id
                or protected.content_hash != plan.create_field_evidence_hash
                or protected.workspace_id != plan.workspace_id
                or protected.source_selection_hash != plan.source_selection_hash
                or protected.source_schema_hash != plan.source_schema_hash
                or protected.destination_target_hash != plan.destination_target_hash
                or protected.destination_read_principal_hash
                != plan.destination_read_principal_hash
                or protected.destination_read_context_hash
                != plan.destination_read_context_hash
                or protected.destination_schema_snapshot_hash
                != plan.destination_schema_snapshot_hash
                or not {item.key for item in plan.create_field_decisions}.issubset(
                    {item.key for item in protected.values}
                )
            ):
                raise ValueError("binding")
            return protected
        except Exception as error:
            raise WorkspaceError(
                "Protected destination defaults are unavailable or changed. "
                "Check matching again."
            ) from error
