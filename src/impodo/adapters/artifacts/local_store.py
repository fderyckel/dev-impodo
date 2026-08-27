"""Define governed artifact storage and its contained filesystem adapter.

Migration stages: source evidence in A/B, prepared and derived evidence in E,
and report projections in H. The port uses opaque generated keys, bounded
streaming, context-managed materialization, partial files, and atomic
replacement. Callers never receive a generic path write capability outside one
validated DataVersion, workspace, or run boundary.

See ``docs/architecture/security-and-infrastructure.md`` and
``tests/integration/artifacts/test_stores.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
from typing import BinaryIO, ContextManager, Protocol
from uuid import UUID, uuid4

from impodo.domain.derived_value_artifact import (
    DERIVED_VALUE_ARTIFACT_STORAGE_LAYOUT_VERSION,
)
from impodo.domain.prepared_snapshot import PREPARED_SNAPSHOT_STORAGE_LAYOUT_VERSION
from impodo.domain.source_snapshot import SOURCE_SNAPSHOT_STORAGE_LAYOUT_VERSION


_DATASET_SNAPSHOT_SEGMENT = re.compile(r"[0-9a-f]{24}")
_SHA256_SEGMENT = re.compile(r"[0-9a-f]{64}")
_PARQUET_SNAPSHOT_FILE = re.compile(r"[0-9a-f]{64}\.parquet")
WINDOWS_PORTABLE_PATH_LIMIT = 259


from impodo.application.shared.artifacts import (
    ArtifactPathTooLongError,
    ArtifactSizeError,
    ArtifactStoreError,
    ArtifactValidator,
    StoredArtifact,
)


class LocalArtifactStore:
    """Store artifacts below explicit DataVersion and workspace roots.

    Every owner/run/key is canonicalized before path construction. Source and
    workspace evidence cannot collide because their roots are distinct.
    Symlinks, path traversal, and partial publication are rejected.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

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
        """Write validated immutable source bytes under a generated opaque key."""

        canonical_artifact_id = str(UUID(artifact_id))
        if suffix not in {".csv", ".xlsx"}:
            raise ArtifactStoreError("Unsupported source artifact suffix")
        inbox = self._inbox_directory(data_version_id, create=True)
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
        data_version_id: str,
        storage_key: str,
    ) -> Iterator[Path]:
        """Yield one contained regular source file for read-only use."""

        path = self._source_path(data_version_id, storage_key)
        if path.is_symlink():
            raise ArtifactStoreError(
                "Stored source artifacts must not be symbolic links"
            )
        if not path.is_file():
            raise ArtifactStoreError("Stored source artifact is missing")
        yield path

    def delete_source(self, data_version_id: str, storage_key: str) -> None:
        """Remove one contained source artifact if present."""

        try:
            self._source_path(data_version_id, storage_key).unlink(missing_ok=True)
        except OSError as error:
            raise ArtifactStoreError(
                "Stored source artifact could not be deleted"
            ) from error

    @contextmanager
    def prepare_source_snapshot(self, data_version_id: str) -> Iterator[Path]:
        """Create and always remove one DataVersion-contained work directory."""

        work_root = self._source_snapshot_root(data_version_id, create=True) / ".work"
        if work_root.is_symlink():
            raise ArtifactStoreError("Snapshot work directory must not be a symlink")
        work_root.mkdir(exist_ok=True)
        workspace = work_root / str(uuid4())
        workspace.mkdir()
        try:
            yield workspace
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def ensure_source_snapshot_capacity(
        self,
        data_version_id: str,
        *,
        required_bytes: int,
    ) -> None:
        """Check conservative free-space headroom without touching source data."""

        if required_bytes < 1:
            raise ArtifactStoreError("Snapshot capacity requirement must be positive")
        data_version = self._data_version_directory(data_version_id)
        if data_version.is_symlink():
            raise ArtifactStoreError("DataVersion artifact directory is unsafe")
        # Odoo-sourced DataVersions have no uploaded inbox file to provision
        # their owner root before the first immutable capture.
        data_version.mkdir(exist_ok=True)
        if shutil.disk_usage(data_version).free < required_bytes:
            raise ArtifactSizeError(
                "Insufficient free space for bounded source snapshot publication"
            )

    def publish_source_snapshot(
        self,
        data_version_id: str,
        temporary_file: Path,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> None:
        """Hash-check and atomically rename a completed Parquet snapshot."""

        source = temporary_file.resolve()
        work_root = self._source_snapshot_root(data_version_id, create=True) / ".work"
        try:
            source.relative_to(work_root.resolve())
        except ValueError as error:
            raise ArtifactStoreError(
                "Snapshot publication source escapes its work directory"
            ) from error
        if temporary_file.is_symlink() or not source.is_file():
            raise ArtifactStoreError("Snapshot writer did not create a regular file")
        actual_hash = _file_sha256(source)
        if actual_hash != _canonical_sha256(expected_sha256):
            raise ArtifactStoreError("Snapshot file hash changed before publication")

        final = self._source_snapshot_path(data_version_id, storage_key, create=True)
        if final.is_symlink():
            raise ArtifactStoreError("Source snapshots must not be symbolic links")
        if final.exists():
            if not final.is_file() or _file_sha256(final) != actual_hash:
                raise ArtifactStoreError(
                    "A different snapshot already occupies the immutable path"
                )
            source.unlink()
            return
        source.replace(final)

    @contextmanager
    def materialize_source_snapshot(
        self,
        data_version_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> Iterator[Path]:
        """Yield one contained snapshot only after verifying its exact bytes."""

        path = self._source_snapshot_path(data_version_id, storage_key, create=False)
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError("Stored source snapshot is missing")
        if _file_sha256(path) != _canonical_sha256(expected_sha256):
            raise ArtifactStoreError("Stored source snapshot failed hash verification")
        yield path

    def source_snapshot_size(self, data_version_id: str, storage_key: str) -> int:
        """Read exact artifact size after its publication hash check."""

        path = self._source_snapshot_path(data_version_id, storage_key, create=False)
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError("Stored source snapshot is missing")
        return path.stat().st_size

    def cleanup_source_snapshots(
        self,
        data_version_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        """Delete work remnants and immutable files absent from DuckDB manifests."""

        root = self._source_snapshot_root(data_version_id, create=True)
        referenced = {
            self._source_snapshot_path(data_version_id, key, create=False)
            for key in referenced_storage_keys
        }
        removed = 0
        work_root = root / ".work"
        if work_root.is_symlink():
            work_root.unlink()
            removed += 1
        elif work_root.is_dir():
            for child in tuple(work_root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
        for candidate in tuple(root.rglob("*.parquet")):
            if candidate.is_symlink() or candidate.resolve() not in referenced:
                candidate.unlink(missing_ok=True)
                removed += 1
        _prune_empty_directories(root)
        return removed

    @contextmanager
    def prepare_prepared_snapshot(self, workspace_id: str) -> Iterator[Path]:
        """Create and always remove one contained prepared-snapshot workspace."""

        work_root = self._prepared_snapshot_root(workspace_id, create=True) / ".work"
        if work_root.is_symlink():
            raise ArtifactStoreError(
                "Prepared snapshot work directory must not be a symlink"
            )
        work_root.mkdir(exist_ok=True)
        workspace = work_root / str(uuid4())
        workspace.mkdir()
        try:
            yield workspace
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def publish_prepared_snapshot(
        self,
        workspace_id: str,
        temporary_file: Path,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> None:
        """Hash-check and atomically publish one prepared Parquet snapshot."""

        source = temporary_file.resolve()
        work_root = self._prepared_snapshot_root(workspace_id, create=True) / ".work"
        try:
            source.relative_to(work_root.resolve())
        except ValueError as error:
            raise ArtifactStoreError(
                "Prepared snapshot source escapes its work directory"
            ) from error
        if temporary_file.is_symlink() or not source.is_file():
            raise ArtifactStoreError(
                "Prepared snapshot writer did not create a regular file"
            )
        actual_hash = _file_sha256(source)
        if actual_hash != _canonical_sha256(expected_sha256):
            raise ArtifactStoreError("Prepared snapshot changed before publication")
        final = self._prepared_snapshot_path(
            workspace_id,
            storage_key,
            create=True,
        )
        if final.is_symlink():
            raise ArtifactStoreError("Prepared snapshots must not be symbolic links")
        if final.exists():
            if not final.is_file() or _file_sha256(final) != actual_hash:
                raise ArtifactStoreError(
                    "A different prepared snapshot occupies the immutable path"
                )
            source.unlink()
            return
        source.replace(final)

    @contextmanager
    def materialize_prepared_snapshot(
        self,
        workspace_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> Iterator[Path]:
        """Yield one contained prepared snapshot after exact hash verification."""

        path = self._prepared_snapshot_path(
            workspace_id,
            storage_key,
            create=False,
        )
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError("Stored prepared snapshot is missing")
        if _file_sha256(path) != _canonical_sha256(expected_sha256):
            raise ArtifactStoreError(
                "Stored prepared snapshot failed hash verification"
            )
        yield path

    def cleanup_prepared_snapshots(
        self,
        workspace_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        """Delete work remnants and prepared files absent from every manifest."""

        root = self._prepared_snapshot_root(workspace_id, create=True)
        referenced = {
            self._prepared_snapshot_path(workspace_id, key, create=False)
            for key in referenced_storage_keys
        }
        removed = 0
        work_root = root / ".work"
        if work_root.is_symlink():
            work_root.unlink()
            removed += 1
        elif work_root.is_dir():
            for child in tuple(work_root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
        for candidate in tuple(root.rglob("*.parquet")):
            if candidate.is_symlink() or candidate.resolve() not in referenced:
                candidate.unlink(missing_ok=True)
                removed += 1
        _prune_empty_directories(root)
        return removed

    @contextmanager
    def prepare_derived_value_artifact(self, workspace_id: str) -> Iterator[Path]:
        """Create and always remove one contained derived-value workspace."""

        work_root = self._derived_value_root(workspace_id, create=True) / ".work"
        if work_root.is_symlink():
            raise ArtifactStoreError(
                "Derived-value work directory must not be a symlink"
            )
        work_root.mkdir(exist_ok=True)
        workspace = work_root / str(uuid4())
        workspace.mkdir()
        try:
            yield workspace
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def publish_derived_value_artifact(
        self,
        workspace_id: str,
        temporary_file: Path,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> bool:
        """Hash-check and atomically publish one derived-value artifact."""

        source = temporary_file.resolve()
        work_root = self._derived_value_root(workspace_id, create=True) / ".work"
        try:
            source.relative_to(work_root.resolve())
        except ValueError as error:
            raise ArtifactStoreError(
                "Derived-value source escapes its work directory"
            ) from error
        if temporary_file.is_symlink() or not source.is_file():
            raise ArtifactStoreError(
                "Derived-value writer did not create a regular file"
            )
        actual_hash = _file_sha256(source)
        if actual_hash != _canonical_sha256(expected_sha256):
            raise ArtifactStoreError(
                "Derived-value artifact changed before publication"
            )
        final = self._derived_value_path(workspace_id, storage_key, create=True)
        if final.is_symlink():
            raise ArtifactStoreError(
                "Derived-value artifacts must not be symbolic links"
            )
        if final.exists():
            if not final.is_file() or _file_sha256(final) != actual_hash:
                raise ArtifactStoreError(
                    "A different derived-value artifact occupies the immutable path"
                )
            source.unlink()
            return False
        source.replace(final)
        return True

    @contextmanager
    def materialize_derived_value_artifact(
        self,
        workspace_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> Iterator[Path]:
        """Yield one derived-value artifact after exact hash verification."""

        path = self._derived_value_path(workspace_id, storage_key, create=False)
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError("Stored derived-value artifact is missing")
        if _file_sha256(path) != _canonical_sha256(expected_sha256):
            raise ArtifactStoreError(
                "Stored derived-value artifact failed hash verification"
            )
        yield path

    def delete_derived_value_artifact(
        self,
        workspace_id: str,
        storage_key: str,
    ) -> None:
        """Remove one contained, not-yet-bound derived-value artifact."""

        try:
            self._derived_value_path(
                workspace_id,
                storage_key,
                create=False,
            ).unlink(missing_ok=True)
        except OSError as error:
            raise ArtifactStoreError(
                "Stored derived-value artifact could not be deleted"
            ) from error

    def cleanup_derived_value_artifacts(
        self,
        workspace_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        """Delete work remnants and derived files absent from every manifest."""

        root = self._derived_value_root(workspace_id, create=True)
        referenced = {
            self._derived_value_path(workspace_id, key, create=False)
            for key in referenced_storage_keys
        }
        removed = 0
        work_root = root / ".work"
        if work_root.is_symlink():
            work_root.unlink()
            removed += 1
        elif work_root.is_dir():
            for child in tuple(work_root.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
        for candidate in tuple(root.rglob("*.parquet")):
            if candidate.is_symlink() or candidate.resolve() not in referenced:
                candidate.unlink(missing_ok=True)
                removed += 1
        _prune_empty_directories(root)
        return removed

    def write_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
        content: bytes,
    ) -> None:
        """Publish one report from in-memory bytes via the partial-file boundary."""

        with self.prepare_report(workspace_id, run_id, filename) as partial:
            partial.write_bytes(content)

    @contextmanager
    def prepare_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> Iterator[Path]:
        """Yield a partial report path and replace the final path atomically."""

        path = self._report_path(workspace_id, run_id, filename, create=True)
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
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> bool:
        """Check for one contained regular report without following a symlink."""

        path = self._report_path(workspace_id, run_id, filename, create=False)
        return not path.is_symlink() and path.is_file()

    def delete_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> None:
        """Remove a failed unpublished projection without touching run history."""

        path = self._report_path(workspace_id, run_id, filename, create=False)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass

    @contextmanager
    def materialize_report(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
    ) -> Iterator[Path]:
        """Yield one contained regular report artifact for reading."""

        path = self._report_path(workspace_id, run_id, filename, create=False)
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError("Stored report artifact is missing")
        yield path

    def _source_path(self, data_version_id: str, storage_key: str) -> Path:
        name = Path(storage_key)
        if name.name != storage_key or name.suffix.casefold() not in {".csv", ".xlsx"}:
            raise ArtifactStoreError("Invalid source artifact key")
        inbox = self._inbox_directory(data_version_id, create=False)
        target = (inbox / storage_key).resolve()
        if target.parent != inbox:
            raise ArtifactStoreError("Invalid source artifact key")
        return target

    def _source_snapshot_root(self, data_version_id: str, *, create: bool) -> Path:
        data_version = self._data_version_directory(data_version_id)
        if create:
            data_version.mkdir(exist_ok=True)
        snapshots = data_version / "snapshots"
        if snapshots.is_symlink():
            raise ArtifactStoreError(
                "DataVersion snapshots must not be a symbolic link"
            )
        if create:
            snapshots.mkdir(exist_ok=True)
        source = snapshots / "source"
        if source.is_symlink():
            raise ArtifactStoreError("Source snapshots must not be a symbolic link")
        if create:
            source.mkdir(exist_ok=True)
        resolved = source.resolve()
        if resolved.parent != snapshots.resolve():
            raise ArtifactStoreError(
                "Source snapshot directory escapes the DataVersion"
            )
        return resolved

    def _source_snapshot_path(
        self,
        data_version_id: str,
        storage_key: str,
        *,
        create: bool,
    ) -> Path:
        key = PurePosixPath(storage_key)
        parts = key.parts
        if (
            key.is_absolute()
            or str(key) != storage_key
            or len(parts) != 5
            or parts[:3]
            != (
                "snapshots",
                "source",
                f"v{SOURCE_SNAPSHOT_STORAGE_LAYOUT_VERSION}",
            )
            or _DATASET_SNAPSHOT_SEGMENT.fullmatch(parts[3]) is None
            or _PARQUET_SNAPSHOT_FILE.fullmatch(parts[4]) is None
        ):
            raise ArtifactStoreError("Invalid source snapshot key")
        root = self._source_snapshot_root(data_version_id, create=create)
        parent = root / parts[2] / parts[3]
        current = root
        for segment in parts[2:4]:
            current = current / segment
            if current.is_symlink():
                raise ArtifactStoreError(
                    "Source snapshot path must not contain symbolic links"
                )
            if create:
                current.mkdir(exist_ok=True)
        unresolved_target = parent / parts[4]
        _require_portable_windows_path(unresolved_target)
        target = unresolved_target.resolve()
        if target.parent != parent.resolve():
            raise ArtifactStoreError("Source snapshot escapes the DataVersion")
        return target

    def _prepared_snapshot_root(self, workspace_id: str, *, create: bool) -> Path:
        workspace = self._workspace_directory(workspace_id)
        if create:
            workspace.mkdir(exist_ok=True)
        snapshots = workspace / "snapshots"
        if snapshots.is_symlink():
            raise ArtifactStoreError("Workspace snapshots must not be a symbolic link")
        if create:
            snapshots.mkdir(exist_ok=True)
        prepared = snapshots / "prepared"
        if prepared.is_symlink():
            raise ArtifactStoreError("Prepared snapshots must not be a symbolic link")
        if create:
            prepared.mkdir(exist_ok=True)
        resolved = prepared.resolve()
        if resolved.parent != snapshots.resolve():
            raise ArtifactStoreError(
                "Prepared snapshot directory escapes the workspace"
            )
        return resolved

    def _prepared_snapshot_path(
        self,
        workspace_id: str,
        storage_key: str,
        *,
        create: bool,
    ) -> Path:
        key = PurePosixPath(storage_key)
        parts = key.parts
        if (
            key.is_absolute()
            or str(key) != storage_key
            or len(parts) != 5
            or parts[:3]
            != (
                "snapshots",
                "prepared",
                f"v{PREPARED_SNAPSHOT_STORAGE_LAYOUT_VERSION}",
            )
            or _DATASET_SNAPSHOT_SEGMENT.fullmatch(parts[3]) is None
            or _PARQUET_SNAPSHOT_FILE.fullmatch(parts[4]) is None
        ):
            raise ArtifactStoreError("Invalid prepared snapshot key")
        root = self._prepared_snapshot_root(workspace_id, create=create)
        parent = root / parts[2] / parts[3]
        current = root
        for segment in parts[2:4]:
            current = current / segment
            if current.is_symlink():
                raise ArtifactStoreError(
                    "Prepared snapshot path must not contain symbolic links"
                )
            if create:
                current.mkdir(exist_ok=True)
        unresolved_target = parent / parts[4]
        _require_portable_windows_path(unresolved_target)
        target = unresolved_target.resolve()
        if target.parent != parent.resolve():
            raise ArtifactStoreError("Prepared snapshot escapes the workspace")
        return target

    def _derived_value_root(self, workspace_id: str, *, create: bool) -> Path:
        workspace = self._workspace_directory(workspace_id)
        if create:
            workspace.mkdir(exist_ok=True)
        snapshots = workspace / "snapshots"
        if snapshots.is_symlink():
            raise ArtifactStoreError("Workspace snapshots must not be a symbolic link")
        if create:
            snapshots.mkdir(exist_ok=True)
        derived = snapshots / "derived"
        if derived.is_symlink():
            raise ArtifactStoreError(
                "Derived-value artifacts must not be a symbolic link"
            )
        if create:
            derived.mkdir(exist_ok=True)
        resolved = derived.resolve()
        if resolved.parent != snapshots.resolve():
            raise ArtifactStoreError("Derived-value directory escapes the workspace")
        return resolved

    def _derived_value_path(
        self,
        workspace_id: str,
        storage_key: str,
        *,
        create: bool,
    ) -> Path:
        key = PurePosixPath(storage_key)
        parts = key.parts
        if (
            key.is_absolute()
            or str(key) != storage_key
            or len(parts) != 5
            or parts[:3]
            != (
                "snapshots",
                "derived",
                f"v{DERIVED_VALUE_ARTIFACT_STORAGE_LAYOUT_VERSION}",
            )
            or _DATASET_SNAPSHOT_SEGMENT.fullmatch(parts[3]) is None
            or _PARQUET_SNAPSHOT_FILE.fullmatch(parts[4]) is None
        ):
            raise ArtifactStoreError("Invalid derived-value artifact key")
        root = self._derived_value_root(workspace_id, create=create)
        parent = root / parts[2] / parts[3]
        current = root
        for segment in parts[2:4]:
            current = current / segment
            if current.is_symlink():
                raise ArtifactStoreError(
                    "Derived-value path must not contain symbolic links"
                )
            if create:
                current.mkdir(exist_ok=True)
        unresolved_target = parent / parts[4]
        _require_portable_windows_path(unresolved_target)
        target = unresolved_target.resolve()
        if target.parent != parent.resolve():
            raise ArtifactStoreError("Derived-value artifact escapes the workspace")
        return target

    def _data_version_directory(self, data_version_id: str) -> Path:
        return self._owner_directory("dv", data_version_id)

    def _workspace_directory(self, workspace_id: str) -> Path:
        return self._owner_directory("ws", workspace_id)

    def _owner_directory(self, owner_kind: str, owner_id: str) -> Path:
        try:
            canonical = str(UUID(owner_id))
        except (ValueError, AttributeError) as error:
            raise ArtifactStoreError("Invalid artifact owner identifier") from error
        owner_root = (self.root / owner_kind).resolve()
        if owner_root.parent != self.root:
            raise ArtifactStoreError("Invalid artifact owner kind")
        owner_root.mkdir(exist_ok=True)
        target = (owner_root / canonical).resolve()
        if target.parent != owner_root:
            raise ArtifactStoreError("Invalid artifact owner identifier")
        return target

    def _inbox_directory(self, data_version_id: str, *, create: bool) -> Path:
        data_version = self._data_version_directory(data_version_id)
        if create:
            data_version.mkdir(parents=False, exist_ok=True)
        inbox_candidate = data_version / "inbox"
        if inbox_candidate.is_symlink():
            raise ArtifactStoreError("DataVersion inbox must not be a symbolic link")
        if create:
            inbox_candidate.mkdir(exist_ok=True)
        inbox = inbox_candidate.resolve()
        if inbox.parent != data_version:
            raise ArtifactStoreError("DataVersion inbox escapes the artifact root")
        return inbox

    def _report_path(
        self,
        workspace_id: str,
        run_id: str,
        filename: str,
        *,
        create: bool,
    ) -> Path:
        canonical_run_id = str(UUID(run_id))
        name = Path(filename)
        if (
            name.name != filename
            or not filename
            or name.suffix not in {".json", ".xlsx"}
        ):
            raise ArtifactStoreError("Invalid report artifact name")
        workspace = self._workspace_directory(workspace_id)
        if create:
            workspace.mkdir(exist_ok=True)
        reports = workspace / "reports"
        if reports.is_symlink():
            raise ArtifactStoreError("Workspace reports must not be a symbolic link")
        if create:
            reports.mkdir(exist_ok=True)
        run_directory = reports / canonical_run_id
        if run_directory.is_symlink():
            raise ArtifactStoreError("Report run must not be a symbolic link")
        if create:
            run_directory.mkdir(exist_ok=True)
        target = (run_directory / filename).resolve()
        if target.parent != run_directory.resolve():
            raise ArtifactStoreError("Report artifact escapes the workspace root")
        return target


def _canonical_sha256(value: str) -> str:
    digest = value.removeprefix("sha256:").casefold()
    if _SHA256_SEGMENT.fullmatch(digest) is None:
        raise ArtifactStoreError("Invalid SHA-256 evidence")
    return f"sha256:{digest}"


def _require_portable_windows_path(path: Path) -> None:
    """Fail clearly before using a path unavailable on baseline Windows."""

    if os.name != "nt":
        return
    path_units = len(os.fspath(path).encode("utf-16-le")) // 2
    if path_units > WINDOWS_PORTABLE_PATH_LIMIT:
        raise ArtifactPathTooLongError(
            "ARTIFACT_PATH_TOO_LONG "
            f"path_units={path_units} "
            f"portable_limit={WINDOWS_PORTABLE_PATH_LIMIT}"
        )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _prune_empty_directories(root: Path) -> None:
    directories = sorted(
        (item for item in root.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
