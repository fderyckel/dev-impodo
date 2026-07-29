"""Governed source-file intake for migration projects."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePath
from typing import BinaryIO
from uuid import uuid4

from .project_store import DuckDbProjectRepository
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
        repository: DuckDbProjectRepository,
        projects: ProjectService,
    ) -> None:
        self.repository = repository
        self.projects = projects

    def accept(
        self,
        project_id: str,
        *,
        expected_revision: int,
        display_name: str,
        stream: BinaryIO,
    ) -> SourceFile:
        safe_name = _safe_display_name(display_name)
        extension = Path(safe_name).suffix.casefold()
        if extension not in ALLOWED_EXTENSIONS:
            raise SourceIntakeError("Only CSV and XLSX files are accepted")

        file_id = str(uuid4())
        stored_name = f"{file_id}{extension}"
        inbox = self.repository.project_directory(project_id) / "inbox"
        partial_path = inbox / f".{file_id}.partial{extension}"
        final_path = inbox / stored_name
        digest = sha256()
        size = 0
        try:
            with partial_path.open("xb") as target:
                while True:
                    chunk = stream.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise SourceIntakeError(
                            f"Source file exceeds {MAX_SOURCE_BYTES} bytes"
                        )
                    digest.update(chunk)
                    target.write(chunk)
            if size == 0:
                raise SourceIntakeError("Source file is empty")
            validate_source_file_isolated(partial_path)
            partial_path.replace(final_path)

            source_file = SourceFile(
                file_id=file_id,
                display_name=safe_name,
                stored_name=stored_name,
                size_bytes=size,
                sha256=digest.hexdigest(),
                received_at=datetime.now(timezone.utc),
            )
            try:
                self.projects.add_source_file(
                    project_id,
                    expected_revision=expected_revision,
                    source_file=source_file,
                )
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
            return source_file
        except SourceLoadError as error:
            raise SourceIntakeError(str(error)) from error
        finally:
            partial_path.unlink(missing_ok=True)


def _safe_display_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 255:
        raise SourceIntakeError("Source filename is missing or too long")
    if PurePath(name).name != name or "/" in name or "\\" in name:
        raise SourceIntakeError("Source filename must not contain a path")
    if any(ord(character) < 32 for character in name):
        raise SourceIntakeError("Source filename contains control characters")
    return name
