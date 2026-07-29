"""Governed source-file intake for migration projects."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import BinaryIO
from uuid import uuid4

from .access import Actor
from .artifacts import ArtifactStore, ArtifactStoreError
from .projects import ProjectError, ProjectService, SourceFile
from .source import SourceLoadError
from .source_worker import validate_source_file_isolated


MAX_SOURCE_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".csv", ".xlsx"})


class SourceIntakeError(ProjectError):
    """Raised when an uploaded source file is unsafe or unsupported."""


class SourceIntakeService:
    """Stream, validate, hash, and register one browser-uploaded source file."""

    def __init__(
        self,
        projects: ProjectService,
        artifacts: ArtifactStore,
    ) -> None:
        self.projects = projects
        self.artifacts = artifacts

    def accept(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        display_name: str,
        stream: BinaryIO,
    ) -> SourceFile:
        safe_name = _safe_display_name(display_name)
        extension = Path(safe_name).suffix.casefold()
        if extension not in ALLOWED_EXTENSIONS:
            raise SourceIntakeError("Only CSV and XLSX files are accepted")

        file_id = str(uuid4())
        try:
            stored = self.artifacts.store_source(
                project_id,
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
                self.projects.add_source_file(
                    project_id,
                    actor=actor,
                    expected_revision=expected_revision,
                    source_file=source_file,
                )
            except Exception:
                self.artifacts.delete_source(project_id, stored.storage_key)
                raise
            return source_file
        except (ArtifactStoreError, SourceLoadError) as error:
            raise SourceIntakeError(str(error)) from error


def _safe_display_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 255:
        raise SourceIntakeError("Source filename is missing or too long")
    if PurePath(name).name != name or "/" in name or "\\" in name:
        raise SourceIntakeError("Source filename must not contain a path")
    if any(ord(character) < 32 for character in name):
        raise SourceIntakeError("Source filename contains control characters")
    return name
