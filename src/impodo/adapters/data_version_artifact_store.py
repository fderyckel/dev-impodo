"""Route source artifacts through DataVersion ownership for workspace engines."""

from __future__ import annotations

from contextlib import AbstractContextManager
from ..artifacts import ArtifactStore, StoredArtifact
from ..migration_foundation import (
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
)
from .duckdb.migration_foundation_repository import MigrationFoundationRepository


class DataVersionAwareArtifactStore:
    """Resolve only source bytes/snapshots from workspace to DataVersion ID."""

    def __init__(
        self,
        store: ArtifactStore,
        foundation: MigrationFoundationRepository,
    ) -> None:
        self.store = store
        self.foundation = foundation

    def store_source(self, project_id: str, **kwargs) -> StoredArtifact:
        return self.store.store_source(self._source_owner(project_id), **kwargs)

    def materialize_source(
        self,
        project_id: str,
        storage_key: str,
    ) -> AbstractContextManager:
        return self.store.materialize_source(
            self._source_owner(project_id),
            storage_key,
        )

    def delete_source(self, project_id: str, storage_key: str) -> None:
        self.store.delete_source(self._source_owner(project_id), storage_key)

    def prepare_source_snapshot(self, project_id: str) -> AbstractContextManager:
        return self.store.prepare_source_snapshot(self._source_owner(project_id))

    def ensure_source_snapshot_capacity(
        self,
        project_id: str,
        *,
        required_bytes: int,
    ) -> None:
        self.store.ensure_source_snapshot_capacity(
            self._source_owner(project_id),
            required_bytes=required_bytes,
        )

    def publish_source_snapshot(
        self,
        project_id: str,
        temporary_file,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> None:
        self.store.publish_source_snapshot(
            self._source_owner(project_id),
            temporary_file,
            storage_key,
            expected_sha256=expected_sha256,
        )

    def materialize_source_snapshot(
        self,
        project_id: str,
        storage_key: str,
        *,
        expected_sha256: str,
    ) -> AbstractContextManager:
        return self.store.materialize_source_snapshot(
            self._source_owner(project_id),
            storage_key,
            expected_sha256=expected_sha256,
        )

    def source_snapshot_size(self, project_id: str, storage_key: str) -> int:
        return self.store.source_snapshot_size(
            self._source_owner(project_id),
            storage_key,
        )

    def cleanup_source_snapshots(
        self,
        project_id: str,
        referenced_storage_keys: frozenset[str],
    ) -> int:
        return self.store.cleanup_source_snapshots(
            self._source_owner(project_id),
            referenced_storage_keys,
        )

    def __getattr__(self, name: str):
        """Delegate non-source prepared/report artifacts by workspace ID."""

        return getattr(self.store, name)

    def _source_owner(self, identity: str) -> str:
        try:
            return self.foundation.get_migration_workspace(identity).data_version_id
        except MigrationIdentifierConfusionError:
            return self.foundation.get_data_version(identity).data_version_id
        except MigrationNotFoundError:
            return self.foundation.get_data_version(identity).data_version_id


class FixedDataVersionArtifactStore(DataVersionAwareArtifactStore):
    """Route source artifacts for one authorized worker without a registry."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        workspace_id: str,
        data_version_id: str,
    ) -> None:
        self.store = store
        self.workspace_id = workspace_id
        self.data_version_id = data_version_id

    def _source_owner(self, identity: str) -> str:
        if identity not in {self.workspace_id, self.data_version_id}:
            raise MigrationIdentifierConfusionError(
                "The worker was asked to open another source package"
            )
        return self.data_version_id
