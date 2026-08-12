"""Authorized publication and reading of protected Odoo provenance."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Iterable, Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..adapters.protected_odoo_provenance import (
    decode_capture_provenance,
    encode_capture_provenance,
)
from ..domain.odoo_capture import OdooCaptureSelection
from ..domain.odoo_provenance import (
    OdooCaptureManifest,
    OdooCaptureOriginHeader,
    OdooOriginBatch,
    OdooProvenanceBinding,
)
from ..projects import MigrationProject, ProjectRepository, SourceMode
from ..secrets import SecretStore, SecretStoreError
from ..workspace_errors import WorkspaceError


class OdooProvenanceStore(Protocol):
    """Protected repository port kept separate from portable source storage."""

    def get_current(self, project_id: str) -> OdooCaptureManifest | None: ...

    def history(self, project_id: str) -> tuple[OdooCaptureManifest, ...]: ...

    def read_encrypted(
        self,
        project_id: str,
        manifest: OdooCaptureManifest,
    ) -> bytes: ...

    def invalidate_current(
        self,
        project_id: str,
        *,
        reason: str,
        actor: Actor,
    ) -> bool: ...

    def purge_expired_history(
        self,
        project_id: str,
        *,
        now: datetime,
        actor: Actor,
    ) -> int: ...


class OdooCaptureSelectionReader(Protocol):
    def get_current_odoo_capture_selection(
        self,
        project_id: str,
    ) -> OdooCaptureSelection | None: ...


@dataclass(frozen=True, slots=True)
class OdooCaptureProvenanceCandidate:
    """Encrypted bounded origins and the complete manifest awaiting promotion."""

    manifest: OdooCaptureManifest
    encrypted_bytes: bytes


class OdooProvenanceService:
    """Enforce authorization, encryption-key isolation, and retention."""

    def __init__(
        self,
        projects: ProjectRepository,
        selections: OdooCaptureSelectionReader,
        provenance: OdooProvenanceStore,
        secrets: SecretStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._projects = projects
        self._selections = selections
        self._provenance = provenance
        self._secrets = secrets
        self._authorization = authorization

    def prepare_capture_origins(
        self,
        project_id: str,
        *,
        actor: Actor,
        header: OdooCaptureOriginHeader,
        batches: Iterable[OdooOriginBatch],
        row_count: int,
        data_logical_hash: str,
        data_sha256: str,
        data_storage_key: str,
        data_size_bytes: int,
        capture_started_at: datetime,
        capture_finished_at: datetime,
    ) -> OdooCaptureProvenanceCandidate:
        """Encode origins once and return a candidate for atomic publication."""

        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_MANAGE,
            project_id=project_id,
        )
        project = self._projects.get(project_id)
        self._require_odoo_project(project)
        selection = self._selections.get_current_odoo_capture_selection(project_id)
        if selection is None:
            raise WorkspaceError("Current Odoo capture selection is missing")
        if row_count < 0 or row_count > selection.max_rows:
            raise WorkspaceError("Odoo capture row count exceeds its selection")
        if capture_started_at.tzinfo is None or capture_finished_at.tzinfo is None:
            raise WorkspaceError("Odoo capture times must be timezone-aware")
        if capture_finished_at < capture_started_at:
            raise WorkspaceError("Odoo capture time range is invalid")

        # Derive the bounded dataset/field identities once per manifest. They
        # are never evaluated inside the origin batch encoder.
        dataset_id = selection.dataset_id
        column_stable_keys = selection.column_stable_keys
        manifest_id = str(uuid4())
        binding = OdooProvenanceBinding(
            manifest_id=manifest_id,
            project_id=project_id,
            selection_hash=selection.content_hash,
            dataset_id=dataset_id,
            model=selection.model,
            connection_target_hash=selection.connection_target_hash,
            schema_scope_hash=selection.schema_scope_hash,
            read_principal_hash=selection.read_principal_hash,
            context_hash=selection.context_hash,
        )
        key = self._project_key(project_id, create=True)
        encoded = encode_capture_provenance(
            binding=binding,
            header=header,
            batches=batches,
            key=key,
        )
        if encoded.row_count != row_count:
            raise WorkspaceError("Odoo origin and values row counts differ")
        retention_until = capture_finished_at.astimezone(timezone.utc) + timedelta(
            days=project.retention_days
        )
        storage_key = f"captures/{encoded.artifact_hash.removeprefix('sha256:')}.iprv"
        manifest = OdooCaptureManifest.create(
            manifest_id=manifest_id,
            selection=selection,
            dataset_id=dataset_id,
            column_stable_keys=column_stable_keys,
            row_count=row_count,
            data_logical_hash=data_logical_hash,
            data_sha256=data_sha256,
            data_storage_key=data_storage_key,
            data_size_bytes=data_size_bytes,
            provenance_logical_hash=encoded.logical_hash,
            provenance_sha256=encoded.artifact_hash,
            provenance_storage_key=storage_key,
            provenance_size_bytes=len(encoded.encrypted_bytes),
            capture_started_at=capture_started_at,
            capture_finished_at=capture_finished_at,
            retention_until=retention_until,
            created_by=(f"{actor.identity.issuer}:{actor.identity.subject_id}"),
        )
        return OdooCaptureProvenanceCandidate(
            manifest=manifest,
            encrypted_bytes=encoded.encrypted_bytes,
        )

    def current_manifest(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> OdooCaptureManifest | None:
        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            project_id=project_id,
        )
        return self._provenance.get_current(project_id)

    def history(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[OdooCaptureManifest, ...]:
        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            project_id=project_id,
        )
        return self._provenance.history(project_id)

    def read_current_origins(
        self,
        project_id: str,
        *,
        actor: Actor,
        now: datetime | None = None,
    ) -> tuple[OdooCaptureOriginHeader, tuple[OdooOriginBatch, ...]] | None:
        """Decrypt an authorized, unexpired current sidecar with bounded output."""

        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            project_id=project_id,
        )
        manifest = self._provenance.get_current(project_id)
        if manifest is None:
            return None
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise WorkspaceError("Odoo retention time must be timezone-aware")
        if manifest.retention_until <= current_time:
            raise WorkspaceError("Protected Odoo provenance has expired")
        encrypted = self._provenance.read_encrypted(project_id, manifest)
        return decode_capture_provenance(
            binding=manifest.provenance_binding,
            encrypted_bytes=encrypted,
            expected_logical_hash=manifest.provenance_logical_hash,
            expected_artifact_hash=manifest.provenance_sha256,
            expected_row_count=manifest.row_count,
            key=self._project_key(project_id, create=False),
        )

    def invalidate_current(
        self,
        project_id: str,
        *,
        actor: Actor,
        reason: str,
    ) -> bool:
        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_MANAGE,
            project_id=project_id,
        )
        return self._provenance.invalidate_current(
            project_id,
            reason=reason,
            actor=actor,
        )

    def enforce_retention(
        self,
        project_id: str,
        *,
        actor: Actor,
        now: datetime | None = None,
    ) -> int:
        """Invalidate an expired current pointer, then purge expired history."""

        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_MANAGE,
            project_id=project_id,
        )
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise WorkspaceError("Odoo retention time must be timezone-aware")
        current = self._provenance.get_current(project_id)
        if current is not None and current.retention_until <= current_time:
            self._provenance.invalidate_current(
                project_id,
                reason="RETENTION_EXPIRED",
                actor=actor,
            )
        return self._provenance.purge_expired_history(
            project_id,
            now=current_time,
            actor=actor,
        )

    def delete_project_key(self, project_id: str, *, actor: Actor) -> None:
        """Remove the application-encryption key during governed deletion."""

        self._authorization.require(
            actor,
            Capability.PROJECT_DELETE,
            project_id=project_id,
        )
        self._secrets.delete(_key_id(project_id))

    def _project_key(self, project_id: str, *, create: bool) -> bytes:
        key_id = _key_id(project_id)
        encoded = self._secrets.get(key_id)
        if encoded is None:
            if not create:
                raise SecretStoreError("Protected Odoo evidence key is missing")
            key = os.urandom(32)
            encoded = base64.urlsafe_b64encode(key).decode("ascii")
            self._secrets.set(key_id, encoded, persistent=True)
            return key
        try:
            key = base64.b64decode(
                encoded.encode("ascii"), altchars=b"-_", validate=True
            )
        except (ValueError, UnicodeError) as error:
            raise SecretStoreError("Protected Odoo evidence key is invalid") from error
        if len(key) != 32:
            raise SecretStoreError("Protected Odoo evidence key is invalid")
        return key

    @staticmethod
    def _require_odoo_project(project: MigrationProject) -> None:
        if project.source_mode is not SourceMode.ODOO:
            raise WorkspaceError("Protected Odoo provenance requires an Odoo source")


def _key_id(project_id: str) -> str:
    return f"{project_id}:protected:origin-v1"
