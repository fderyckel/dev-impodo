"""Application-facing ports for owner-qualified artifact evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ContextManager, Protocol


class ArtifactStoreError(RuntimeError):
    """Raised when governed artifact storage cannot complete an operation."""


class ArtifactSizeError(ArtifactStoreError):
    """Raised when a streamed artifact exceeds its configured limit."""


class ArtifactPathTooLongError(ArtifactStoreError):
    """Raised before a governed artifact exceeds the portable Windows limit."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Storage-neutral evidence returned after a successful upload."""

    storage_key: str
    size_bytes: int
    sha256: str


ArtifactValidator = Callable[[Path], None]


class DataVersionSourceArtifactStore(Protocol):
    """Store immutable source evidence owned by one DataVersion."""

    def store_source(
        self,
        data_version_id: str,
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
        data_version_id: str,
        storage_key: str,
    ) -> ContextManager[Path]:
        """Temporarily expose one validated immutable source path for reading."""
        ...

    def delete_source(self, data_version_id: str, storage_key: str) -> None:
        """Delete contained source bytes after intake rollback or governed removal."""
        ...

    def prepare_source_snapshot(
        self,
        data_version_id: str,
    ) -> ContextManager[Path]:
        """Yield one contained temporary workspace for a Parquet snapshot."""
        ...

    def ensure_source_snapshot_capacity(
        self,
        data_version_id: str,
        *,
        required_bytes: int,
    ) -> None:
        """Fail before target I/O when snapshot publication lacks disk headroom."""
        ...

    def publish_source_snapshot(
        self,
        data_version_id: str,
        temporary_file: Path,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> None:
        """Atomically publish one verified immutable snapshot file."""
        ...

    def materialize_source_snapshot(
        self,
        data_version_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> ContextManager[Path]:
        """Expose one hash-verified immutable source snapshot for reading."""
        ...

    def source_snapshot_size(self, data_version_id: str, storage_key: str) -> int:
        """Return contained immutable snapshot bytes without rehashing them."""
        ...

    def cleanup_source_snapshots(
        self,
        data_version_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        """Remove only temporary and unregistered source-snapshot files."""
        ...


class WorkspaceArtifactStore(Protocol):
    """Store derived evidence and reports owned by one MigrationWorkspace."""

    def prepare_prepared_snapshot(
        self,
        workspace_id: str,
    ) -> ContextManager[Path]:
        """Yield one contained workspace for a prepared Parquet snapshot."""
        ...

    def publish_prepared_snapshot(
        self,
        workspace_id: str,
        temporary_file: Path,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> None:
        """Atomically publish one verified immutable prepared snapshot."""
        ...

    def materialize_prepared_snapshot(
        self,
        workspace_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> ContextManager[Path]:
        """Expose one hash-verified prepared snapshot for reading."""
        ...

    def cleanup_prepared_snapshots(
        self,
        workspace_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        """Remove only temporary and unregistered prepared snapshots."""
        ...

    def prepare_derived_value_artifact(
        self,
        workspace_id: str,
    ) -> ContextManager[Path]:
        """Yield one contained workspace for a derived-value artifact."""
        ...

    def publish_derived_value_artifact(
        self,
        workspace_id: str,
        temporary_file: Path,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> bool:
        """Publish verified bytes and report whether a new file was created."""
        ...

    def materialize_derived_value_artifact(
        self,
        workspace_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> ContextManager[Path]:
        """Expose one hash-verified derived-value artifact for reading."""
        ...

    def delete_derived_value_artifact(
        self,
        workspace_id: str,
        storage_key: str,
    ) -> None:
        """Delete one unbound derived-value artifact after publication failure."""
        ...

    def cleanup_derived_value_artifacts(
        self,
        workspace_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        """Remove only temporary and unregistered derived-value artifacts."""
        ...

    def write_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
        content: bytes,
    ) -> None:
        """Atomically write one bounded in-memory report projection."""
        ...

    def prepare_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> ContextManager[Path]:
        """Yield a partial path and atomically publish it on successful exit."""
        ...

    def report_exists(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> bool:
        """Return whether one non-symlink report projection exists."""
        ...

    def delete_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> None:
        """Remove a failed/unpublished projection without deleting run evidence."""
        ...

    def materialize_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> ContextManager[Path]:
        """Temporarily expose one validated report projection for reading."""
        ...


class GovernedArtifactStores(
    DataVersionSourceArtifactStore,
    WorkspaceArtifactStore,
    Protocol,
):
    """Provide both explicit owner-specific ports at composition boundaries."""
