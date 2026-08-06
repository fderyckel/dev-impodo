"""Define governed artifact storage and its contained filesystem adapter.

Migration stages: source evidence in A/B and report projections in H. The port
uses opaque generated keys, bounded streaming, context-managed materialization,
partial files, and atomic replacement. Callers never receive a generic path
write capability outside one validated project/run boundary.

See ``docs/architecture/security-and-infrastructure.md`` and
``tests/test_projects.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, ContextManager, Protocol
from uuid import UUID


class ArtifactStoreError(RuntimeError):
    """Raised when governed artifact storage cannot complete an operation."""


class ArtifactSizeError(ArtifactStoreError):
    """Raised when a streamed artifact exceeds its configured limit."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Storage-neutral evidence returned after a successful upload."""

    storage_key: str
    size_bytes: int
    sha256: str


ArtifactValidator = Callable[[Path], None]


class ArtifactStore(Protocol):
    """Port for immutable source files and replaceable report projections."""

    def store_source(
        self,
        project_id: str,
        *,
        artifact_id: str,
        suffix: str,
        stream: BinaryIO,
        maximum_bytes: int,
        chunk_bytes: int,
        validator: ArtifactValidator,
    ) -> StoredArtifact:
        """Stream, bound, validate, hash, and atomically publish source bytes."""
        ...

    def materialize_source(
        self,
        project_id: str,
        storage_key: str,
    ) -> ContextManager[Path]:
        """Temporarily expose one validated immutable source path for reading."""
        ...

    def delete_source(self, project_id: str, storage_key: str) -> None:
        """Delete newly stored source bytes during a failed compensated intake."""
        ...

    def write_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
        content: bytes,
    ) -> None:
        """Atomically write one bounded in-memory report projection."""
        ...

    def prepare_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> ContextManager[Path]:
        """Yield a partial path and atomically publish it on successful exit."""
        ...

    def report_exists(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> bool:
        """Return whether one non-symlink report projection exists."""
        ...

    def delete_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> None:
        """Remove a failed/unpublished projection without deleting run evidence."""
        ...

    def materialize_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> ContextManager[Path]:
        """Temporarily expose one validated report projection for reading."""
        ...


class LocalArtifactStore:
    """Store project artifacts below one validated local root.

    Every project/run/key is canonicalized before path construction. Symlinks
    and path traversal are rejected, and partial files are removed on failure.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def store_source(
        self,
        project_id: str,
        *,
        artifact_id: str,
        suffix: str,
        stream: BinaryIO,
        maximum_bytes: int,
        chunk_bytes: int,
        validator: ArtifactValidator,
    ) -> StoredArtifact:
        """Write validated immutable source bytes under a generated opaque key."""

        canonical_artifact_id = str(UUID(artifact_id))
        if suffix not in {".csv", ".xlsx"}:
            raise ArtifactStoreError("Unsupported source artifact suffix")
        inbox = self._inbox_directory(project_id, create=True)
        storage_key = f"{canonical_artifact_id}{suffix}"
        partial = inbox / f".{canonical_artifact_id}.partial{suffix}"
        final = inbox / storage_key
        if final.exists() or final.is_symlink():
            raise ArtifactStoreError("Source artifact already exists")
        digest = sha256()
        size = 0
        try:
            with partial.open("xb") as target:
                while True:
                    chunk = stream.read(chunk_bytes)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise ArtifactSizeError(
                            f"Source file exceeds {maximum_bytes} bytes"
                        )
                    digest.update(chunk)
                    target.write(chunk)
            if size == 0:
                raise ArtifactStoreError("Source file is empty")
            validator(partial)
            partial.replace(final)
            return StoredArtifact(
                storage_key=storage_key,
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
        finally:
            partial.unlink(missing_ok=True)

    @contextmanager
    def materialize_source(
        self,
        project_id: str,
        storage_key: str,
    ) -> Iterator[Path]:
        """Yield one contained regular source file for read-only use."""

        path = self._source_path(project_id, storage_key)
        if path.is_symlink():
            raise ArtifactStoreError(
                "Stored source artifacts must not be symbolic links"
            )
        if not path.is_file():
            raise ArtifactStoreError("Stored source artifact is missing")
        yield path

    def delete_source(self, project_id: str, storage_key: str) -> None:
        """Remove one contained source artifact if present."""

        self._source_path(project_id, storage_key).unlink(missing_ok=True)

    def write_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
        content: bytes,
    ) -> None:
        """Publish one report from in-memory bytes via the partial-file boundary."""

        with self.prepare_report(project_id, run_id, filename) as partial:
            partial.write_bytes(content)

    @contextmanager
    def prepare_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> Iterator[Path]:
        """Yield a partial report path and replace the final path atomically."""

        path = self._report_path(project_id, run_id, filename, create=True)
        partial = path.with_name(f".{path.name}.partial")
        try:
            yield partial
            if not partial.is_file():
                raise ArtifactStoreError("Report writer did not create an artifact")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)

    def report_exists(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> bool:
        """Check for one contained regular report without following a symlink."""

        path = self._report_path(project_id, run_id, filename, create=False)
        return not path.is_symlink() and path.is_file()

    def delete_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> None:
        """Remove a failed unpublished projection without touching run history."""

        path = self._report_path(
            project_id, run_id, filename, create=False
        )
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass

    @contextmanager
    def materialize_report(
        self,
        project_id: str,
        run_id: str,
        filename: str,
    ) -> Iterator[Path]:
        """Yield one contained regular report artifact for reading."""

        path = self._report_path(project_id, run_id, filename, create=False)
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError("Stored report artifact is missing")
        yield path

    def _source_path(self, project_id: str, storage_key: str) -> Path:
        name = Path(storage_key)
        if name.name != storage_key or name.suffix.casefold() not in {".csv", ".xlsx"}:
            raise ArtifactStoreError("Invalid source artifact key")
        inbox = self._inbox_directory(project_id, create=False)
        target = (inbox / storage_key).resolve()
        if target.parent != inbox:
            raise ArtifactStoreError("Invalid source artifact key")
        return target

    def _project_directory(self, project_id: str) -> Path:
        try:
            canonical = str(UUID(project_id))
        except (ValueError, AttributeError) as error:
            raise ArtifactStoreError("Invalid project identifier") from error
        target = (self.root / canonical).resolve()
        if target.parent != self.root:
            raise ArtifactStoreError("Invalid project identifier")
        return target

    def _inbox_directory(self, project_id: str, *, create: bool) -> Path:
        project = self._project_directory(project_id)
        if create:
            project.mkdir(parents=False, exist_ok=True)
        inbox_candidate = project / "inbox"
        if inbox_candidate.is_symlink():
            raise ArtifactStoreError("Project inbox must not be a symbolic link")
        if create:
            inbox_candidate.mkdir(exist_ok=True)
        inbox = inbox_candidate.resolve()
        if inbox.parent != project:
            raise ArtifactStoreError("Project inbox escapes the artifact root")
        return inbox

    def _report_path(
        self,
        project_id: str,
        run_id: str,
        filename: str,
        *,
        create: bool,
    ) -> Path:
        canonical_run_id = str(UUID(run_id))
        name = Path(filename)
        if name.name != filename or not filename or name.suffix not in {".json", ".xlsx"}:
            raise ArtifactStoreError("Invalid report artifact name")
        project = self._project_directory(project_id)
        reports = project / "reports"
        if reports.is_symlink():
            raise ArtifactStoreError("Project reports must not be a symbolic link")
        if create:
            reports.mkdir(exist_ok=True)
        run_directory = reports / canonical_run_id
        if run_directory.is_symlink():
            raise ArtifactStoreError("Report run must not be a symbolic link")
        if create:
            run_directory.mkdir(exist_ok=True)
        target = (run_directory / filename).resolve()
        if target.parent != run_directory.resolve():
            raise ArtifactStoreError("Report artifact escapes the project root")
        return target
