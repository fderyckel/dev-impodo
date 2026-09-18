"""Encrypted storage for bounded Odoo source filter values."""

from __future__ import annotations

import json

from .protected_project_evidence_store import ProtectedProjectEvidenceStore
from ..domain.odoo_capture import OdooCaptureFilterClause, OdooCaptureSelection
from ..domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ..domain.serialization import canonical_json, content_hash
from ..domain.workspace.errors import WorkspaceError


_KIND = "odoo-source-filters"


def _binding(selection_id: str, version: int, data_version_id: str) -> str:
    return content_hash({
        "kind": _KIND,
        "selection_id": selection_id,
        "version": version,
        "data_version_id": data_version_id,
    })


class ProtectedOdooCaptureFilterStore:
    def __init__(self, evidence: ProtectedProjectEvidenceStore) -> None:
        self._evidence = evidence

    def put(
        self,
        project_id: str,
        *,
        selection_id: str,
        version: int,
        data_version_id: str,
        clauses: tuple[OdooCaptureFilterClause, ...],
    ) -> str:
        if not clauses:
            raise WorkspaceError("Choose a source filter before storing it")
        payload = canonical_json({
            "selection_id": selection_id,
            "version": version,
            "data_version_id": data_version_id,
            "clauses": [clause.to_dict() for clause in clauses],
        }).encode("utf-8")
        if len(payload) > CURRENT_ODOO_SOURCE_POLICY.max_filter_bytes + 512:
            raise WorkspaceError("Odoo source filter exceeds the protected size limit")
        try:
            stored = self._evidence.put_artifact(
                project_id,
                artifact_kind=_KIND,
                artifact_id=selection_id,
                logical_hash=_binding(selection_id, version, data_version_id),
                payload=payload,
            )
        except Exception as error:
            raise WorkspaceError(
                "The protected Odoo source filter could not be saved. Try again."
            ) from error
        return stored.artifact_hash

    def read(
        self,
        project_id: str,
        selection: OdooCaptureSelection,
    ) -> tuple[OdooCaptureFilterClause, ...]:
        if selection.protected_filter_artifact_hash is None:
            return selection.filter_clauses
        binding = _binding(
            selection.selection_id, selection.version, selection.data_version_id
        )
        try:
            raw = self._evidence.read(
                project_id,
                storage_key=self._evidence.artifact_storage_key(
                    project_id,
                    artifact_kind=_KIND,
                    artifact_id=selection.selection_id,
                    logical_hash=binding,
                ),
                logical_hash=binding,
                expected_artifact_hash=selection.protected_filter_artifact_hash,
            )
            if len(raw) > CURRENT_ODOO_SOURCE_POLICY.max_filter_bytes + 512:
                raise ValueError("oversize")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {
                "selection_id", "version", "data_version_id", "clauses"
            } or (
                payload["selection_id"] != selection.selection_id
                or payload["version"] != selection.version
                or payload["data_version_id"] != selection.data_version_id
                or not isinstance(payload["clauses"], list)
            ):
                raise ValueError("binding")
            clauses = tuple(
                OdooCaptureFilterClause.from_dict(item)
                for item in payload["clauses"]
            )
            if not clauses:
                raise ValueError("empty")
            return clauses
        except Exception as error:
            raise WorkspaceError(
                "The protected Odoo source filter is unavailable or changed. Save the capture plan again."
            ) from error
