"""Validate and register immutable Stage A/B source-file evidence.

Layer: application service at the artifact boundary.

``SourceIntakeService.accept`` is called by the project setup router. It streams
an upload through the ``DataVersionSourceArtifactStore`` and isolated file validator, then asks
``WorkspaceStateService`` to attach the resulting size/hash evidence. The original
display name is never used as the storage key. Early-stage removal first retires
the governed database reference and then deletes its opaque stored bytes.

See ``docs/architecture/python-code-map.md``,
``tests/test_data_version_source_packages.py``, and
``tests/test_project_authoring.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import BinaryIO
from uuid import uuid4

from .access import Actor, Capability
from .artifacts import DataVersionSourceArtifactStore, ArtifactStoreError
from .workspace_access import WorkspaceAccessService
from .workspace_state import WorkspaceStateError, WorkspaceStateService, SourceFile
from .source import SourceLoadError
from .source_worker import validate_source_file_isolated


MAX_SOURCE_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".csv", ".xlsx"})


class SourceIntakeError(WorkspaceStateError):
    """Raised when an uploaded source file is unsafe or unsupported."""


class SourceIntakeService:
    """Coordinate safe artifact storage with project source registration.

    Storage and project persistence form a compensated operation: if project
    registration fails after storage succeeds, the newly written artifact is
    deleted before the error is returned.
    """

    def __init__(
        self,
        workspaces: WorkspaceStateService,
        artifacts: DataVersionSourceArtifactStore,
        workspace_access: WorkspaceAccessService,
    ) -> None:
        self.workspaces = workspaces
        self.artifacts = artifacts
        self.workspace_access = workspace_access

    def accept(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        display_name: str,
        stream: BinaryIO,
    ) -> SourceFile:
        """Accept one bounded CSV/XLSX stream as immutable source evidence.

        Returns:
            The registered ``SourceFile`` containing its generated ID, opaque
            storage key, byte count, SHA-256 hash, and receipt time.

        Raises:
            SourceIntakeError: If the name, format, size, bytes, or artifact
                operation is unsafe or unsupported.
            WorkspaceStateError: If project authorization, lifecycle, or optimistic
                revision validation rejects the attachment.
        """

        safe_name = _safe_display_name(display_name)
        extension = Path(safe_name).suffix.casefold()
        if extension not in ALLOWED_EXTENSIONS:
            raise SourceIntakeError("Only CSV and XLSX files are accepted")

        context = self.workspace_access.require(
            actor,
            Capability.PROJECT_EDIT,
            workspace_id=workspace_id,
        )
        data_version_id = context.data_version_id
        file_id = str(uuid4())
        try:
            stored = self.artifacts.store_source(
                data_version_id,
                artifact_id=file_id,
                suffix=extension,
                stream=stream,
                maximum_bytes=MAX_SOURCE_BYTES,
                chunk_bytes=CHUNK_BYTES,
                validator=validate_source_file_isolated,
            )
            source_file = SourceFile(
                file_id=file_id,
                display_name=safe_name,
                stored_name=stored.storage_key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                received_at=datetime.now(timezone.utc),
            )
            try:
                self.workspaces.add_source_file(
                    workspace_id,
                    actor=actor,
                    expected_revision=expected_revision,
                    source_file=source_file,
                )
            except Exception:
                self.artifacts.delete_source(data_version_id, stored.storage_key)
                raise
            return source_file
        except (ArtifactStoreError, SourceLoadError) as error:
            raise SourceIntakeError(str(error)) from error

    def remove(
        self,
        workspace_id: str,
        file_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> SourceFile:
        """Remove one unfrozen source record and its contained stored bytes."""

        context = self.workspace_access.require(
            actor,
            Capability.PROJECT_EDIT,
            workspace_id=workspace_id,
        )
        source_file = self.workspaces.remove_source_file(
            workspace_id,
            file_id,
            actor=actor,
            expected_revision=expected_revision,
        )
        try:
            self.artifacts.delete_source(
                context.data_version_id,
                source_file.stored_name,
            )
        except ArtifactStoreError as error:
            raise SourceIntakeError(
                "The file was removed from the project, but its stored copy "
                "could not be deleted. Contact support before continuing."
            ) from error
        return source_file


def _safe_display_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 255:
        raise SourceIntakeError("Source filename is missing or too long")
    if PurePath(name).name != name or "/" in name or "\\" in name:
        raise SourceIntakeError("Source filename must not contain a path")
    if any(ord(character) < 32 for character in name):
        raise SourceIntakeError("Source filename contains control characters")
    return name
