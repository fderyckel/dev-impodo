"""Persist Data version root records through the shared registry coordinator."""

from __future__ import annotations

from ...access import Actor
from ...data_versions import DataVersion
from ...migration_foundation import (
    MigrationConflictError,
    require_revision,
    require_uuid,
)


class FoundationDataVersionRecords:
    """Own Data version numbering, reads, and optimistic-revision persistence."""

    def next_data_version_number(self, project_id: str) -> int:
        return self._next_number(project_id, "data_version", "version_number")

    def get_data_version(self, data_version_id: str) -> DataVersion:
        data_version = self._get_data_version_registry(data_version_id)
        self.database.ensure_data_version_store(data_version)
        return data_version

    def _get_data_version_registry(self, data_version_id: str) -> DataVersion:
        data_version_id = require_uuid(data_version_id, "data_version_id")
        with self.database.connect(self.registry_path) as connection:
            row = self._exact_row(
                connection,
                table="data_version",
                id_column="data_version_id",
                identity=data_version_id,
                expected_kind="DATA_VERSION",
            )
        return self._data_version_from_row(row)

    def list_data_versions(self, project_id: str) -> tuple[DataVersion, ...]:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            self._require_project(connection, project_id)
            rows = self._rows(
                connection,
                "SELECT * FROM data_version WHERE project_id = ? "
                "ORDER BY version_number",
                [project_id],
            )
        return tuple(self._data_version_from_row(row) for row in rows)

    def save_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> DataVersion:
        expected_revision = require_revision(expected_revision)
        if data_version.optimistic_revision != expected_revision:
            raise MigrationConflictError("DataVersion revision is stale")
        new_revision = expected_revision + 1
        with self._registry_transactions.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE data_version
                   SET label = ?, export_as_of = ?, state = ?,
                       source_package_hash = ?, optimistic_revision = ?,
                       updated_at = ?, frozen_at = ?
                 WHERE data_version_id = ? AND optimistic_revision = ?
                 RETURNING data_version_id
                """,
                [
                    data_version.label,
                    data_version.export_as_of,
                    data_version.state.value,
                    data_version.source_package_hash,
                    new_revision,
                    data_version.updated_at.isoformat(),
                    self._time(data_version.frozen_at),
                    data_version.data_version_id,
                    expected_revision,
                ],
            ).fetchone()
            if updated is None:
                raise MigrationConflictError("DataVersion changed; reload and retry")
            self._insert_event(
                connection,
                project_id=data_version.project_id,
                aggregate_kind="DATA_VERSION",
                aggregate_id=data_version.data_version_id,
                aggregate_revision=new_revision,
                event_type=event_type,
                detail={},
                actor=actor,
                occurred_at=data_version.updated_at,
            )
        return self.get_data_version(data_version.data_version_id)
