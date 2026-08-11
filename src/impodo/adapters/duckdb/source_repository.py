"""Persist Stage B catalogs, confirmations, and frozen selections in DuckDB.

Layer: adapter. ``SourceRepository`` binds every source decision to registered
file and catalog hashes and atomically invalidates dependent current evidence
when files are reinterpreted, reconfirmed, or refrozen. It also projects the
physical selection plus current derived plan as the effective mapping source.

See ``docs/architecture/python-code-map.md`` and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)
from typing import Iterable


from ...access import Actor
from ...derived_entities import mapping_source_selection
from ...domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from ...inspection import SourceFileCatalog, SourceInspectionError
from ...projects import ProjectNotFoundError
from ...workspace_contracts import (
    SourceConfiguration,
    SourceDataset,
    SourceSelection,
)
from ...workspace_errors import WorkspaceError
from .database import DuckDbDatabase
from .derived_entity_repository import DerivedEntityRepository
from .repository import DuckDbRepository







class SourceRepository(DuckDbRepository):
    """Own current Stage B workspace evidence and its invalidation boundary."""

    def __init__(
        self,
        database: DuckDbDatabase,
        derived_entities: DerivedEntityRepository,
    ) -> None:
        super().__init__(database)
        self._derived_entities = derived_entities

    def get_source_catalogs(
        self,
        project_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        """Load source catalogs in the same order as registered source files."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            rows = connection.execute(
                """
                SELECT catalog.catalog_json
                  FROM source_file AS source
                  JOIN source_catalog AS catalog
                    ON catalog.file_id = source.file_id
                 ORDER BY source.received_at, source.file_id
                """
            ).fetchall()
        return tuple(SourceFileCatalog.from_json(str(row[0])) for row in rows)
    def save_source_catalogs(
        self,
        project_id: str,
        catalogs: Iterable[SourceFileCatalog],
        *,
        actor: Actor,
    ) -> None:
        """Atomically replace the complete hash-bound catalog set."""

        catalog_set = tuple(catalogs)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            source_rows = connection.execute(
                "SELECT file_id, sha256 FROM source_file"
            ).fetchall()
            registered = {str(row[0]): str(row[1]) for row in source_rows}
            supplied = {
                catalog.file_id: catalog.source_sha256
                for catalog in catalog_set
            }
            if supplied != registered or len(supplied) != len(catalog_set):
                raise SourceInspectionError(
                    "Source catalogs do not match the registered project files"
                )
            revision_row = connection.execute(
                "SELECT revision FROM project"
            ).fetchone()
            if revision_row is None:
                raise ProjectNotFoundError("Project not found")

            connection.begin()
            try:
                connection.execute("DELETE FROM source_catalog")
                connection.execute("DELETE FROM source_configuration")
                connection.execute("DELETE FROM source_selection")
                connection.execute("DELETE FROM source_snapshot_current")
                connection.execute("DELETE FROM derived_entity_plan_current")
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="SOURCE_FILES_REINSPECTED",
                )
                for catalog in catalog_set:
                    connection.execute(
                        """
                        INSERT INTO source_catalog
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            catalog.file_id,
                            catalog.source_sha256,
                            catalog.contract_version,
                            catalog.inspected_at.isoformat(),
                            catalog.to_json(),
                        ],
                    )
                connection.execute(
                    """
                    INSERT INTO audit_event (
                        event_id, event_type, project_revision, occurred_at,
                        detail, actor_issuer, actor_subject, actor_display_name
                    )
                    VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "SOURCE_FILES_INSPECTED",
                        int(revision_row[0]),
                        datetime.now(timezone.utc).isoformat(),
                        f"{len(catalog_set)} source file(s)",
                        actor.identity.issuer,
                        actor.identity.subject_id,
                        actor.identity.display_name,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    def save_source_catalog(
        self,
        project_id: str,
        catalog: SourceFileCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Replace one catalog and invalidate every dependent source decision."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            source = connection.execute(
                "SELECT sha256 FROM source_file WHERE file_id = ?",
                [catalog.file_id],
            ).fetchone()
            if source is None or str(source[0]) != catalog.source_sha256:
                raise SourceInspectionError(
                    "Source catalog does not match the registered project file"
                )
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO source_catalog
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        catalog.file_id,
                        catalog.source_sha256,
                        catalog.contract_version,
                        catalog.inspected_at.isoformat(),
                        catalog.to_json(),
                    ],
                )
                connection.execute(
                    "DELETE FROM source_configuration WHERE file_id = ?",
                    [catalog.file_id],
                )
                connection.execute("DELETE FROM source_selection")
                connection.execute("DELETE FROM source_snapshot_current")
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="SOURCE_FILE_REINSPECTED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="SOURCE_FILE_REINSPECTED",
                    detail=catalog.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    def get_source_configurations(
        self,
        project_id: str,
    ) -> tuple[SourceConfiguration, ...]:
        """Load confirmations in the same order as registered source files."""

        return tuple(
            SourceConfiguration.from_json(value)
            for value in self._read_json_rows(
                project_id,
                """
                SELECT configuration.configuration_json
                  FROM source_file AS source
                  JOIN source_configuration AS configuration
                    ON configuration.file_id = source.file_id
                 ORDER BY source.received_at, source.file_id
                """,
            )
        )
    def save_source_configuration(
        self,
        project_id: str,
        configuration: SourceConfiguration,
        *,
        actor: Actor,
    ) -> None:
        """Confirm one exact catalog and invalidate selection/mapping/staging."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT source_sha256, catalog_json
                  FROM source_catalog
                 WHERE file_id = ?
                """,
                [configuration.file_id],
            ).fetchone()
            if row is None:
                raise WorkspaceError("Inspect the source file before confirming it")
            catalog = SourceFileCatalog.from_json(str(row[1]))
            if (
                str(row[0]) != configuration.source_sha256
                or catalog.content_hash != configuration.catalog_hash
            ):
                raise WorkspaceError("Source confirmation does not match its catalog")
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO source_configuration
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        configuration.file_id,
                        configuration.source_sha256,
                        configuration.catalog_hash,
                        configuration.to_json(),
                    ],
                )
                connection.execute("DELETE FROM source_selection")
                connection.execute("DELETE FROM source_snapshot_current")
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="SOURCE_CONFIGURATION_CHANGED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="SOURCE_CONFIGURATION_CONFIRMED",
                    detail=configuration.file_id,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    def get_source_selection(self, project_id: str) -> SourceSelection | None:
        """Return the current frozen physical selection, when available."""

        value = self._read_singleton_json(
            project_id,
            "SELECT selection_json FROM source_selection WHERE singleton_id = 1",
        )
        return SourceSelection.from_json(value) if value else None
    def get_mapping_source_selection(
        self,
        project_id: str,
    ) -> SourceSelection | None:
        """Return physical or prepared logical datasets used by mapping."""

        selection = self.get_source_selection(project_id)
        if selection is None:
            return None
        return mapping_source_selection(
            selection,
            self._derived_entities.get_derived_entity_plan(project_id),
            self.get_source_catalogs(project_id),
        )

    def get_current_source_snapshots(
        self,
        project_id: str,
    ) -> tuple[SourceSnapshot, ...]:
        """Return current immutable snapshot manifests by dataset identity."""

        return tuple(
            SourceSnapshot.from_json(value)
            for value in self._read_json_rows(
                project_id,
                """
                SELECT manifest.manifest_json
                  FROM source_snapshot_current AS current
                  JOIN source_snapshot_manifest AS manifest
                    ON manifest.content_hash = current.content_hash
                 ORDER BY current.dataset_id
                """,
            )
        )

    def find_source_snapshot(
        self,
        project_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> SourceSnapshot | None:
        """Find a previously registered exact logical snapshot for reuse."""

        values = self._read_json_rows(
            project_id,
            """
            SELECT manifest_json
              FROM source_snapshot_manifest
             WHERE dataset_id = ? AND logical_hash = ?
             ORDER BY created_at DESC, content_hash
             LIMIT 1
            """,
            [dataset_id, logical_hash],
        )
        return SourceSnapshot.from_json(values[0]) if values else None

    def source_snapshot_storage_keys(self, project_id: str) -> frozenset[str]:
        """Return every immutable file referenced by a historical manifest."""

        return frozenset(
            self._read_json_rows(
                project_id,
                """
                SELECT parquet_storage_key
                  FROM source_snapshot_manifest
                 ORDER BY parquet_storage_key
                """,
            )
        )

    def publish_source_selection_with_snapshots(
        self,
        project_id: str,
        selection: SourceSelection,
        snapshots: Iterable[SourceSnapshot],
        *,
        actor: Actor,
    ) -> None:
        """Register manifests and advance selection/snapshot pointers atomically."""

        manifests = tuple(snapshots)
        datasets = {item.dataset_id: item for item in selection.datasets}
        expected_ids = {item.dataset_id for item in selection.datasets}
        supplied_ids = {item.dataset_id for item in manifests}
        if (
            selection.project_id != project_id
            or len(datasets) != len(selection.datasets)
            or supplied_ids != expected_ids
            or len(supplied_ids) != len(manifests)
            or any(
                item.project_id != project_id
                or item.physical_selection_hash != selection.content_hash
                or not _snapshot_matches_dataset(item, datasets[item.dataset_id])
                for item in manifests
            )
        ):
            raise WorkspaceError(
                "Source snapshots do not match the frozen source selection"
            )
        self._publish_source_selection(
            project_id,
            selection,
            manifests,
            actor=actor,
        )
    def save_source_selection(
        self,
        project_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None:
        """Publish one frozen selection and retire dependent current pointers."""

        self._publish_source_selection(project_id, selection, (), actor=actor)

    def _publish_source_selection(
        self,
        project_id: str,
        selection: SourceSelection,
        snapshots: tuple[SourceSnapshot, ...],
        *,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            revision = self._project_revision(connection)
            connection.begin()
            try:
                for snapshot in snapshots:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO source_snapshot_manifest
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            snapshot.content_hash,
                            snapshot.dataset_id,
                            snapshot.logical_hash,
                            snapshot.parquet_sha256,
                            snapshot.parquet_storage_key,
                            snapshot.created_at.isoformat(),
                            snapshot.to_json(),
                        ],
                    )
                    registered = connection.execute(
                        """
                        SELECT dataset_id, logical_hash, parquet_sha256,
                               parquet_storage_key
                          FROM source_snapshot_manifest
                         WHERE content_hash = ?
                        """,
                        [snapshot.content_hash],
                    ).fetchone()
                    if registered != (
                        snapshot.dataset_id,
                        snapshot.logical_hash,
                        snapshot.parquet_sha256,
                        snapshot.parquet_storage_key,
                    ):
                        raise WorkspaceError(
                            "Stored source snapshot manifest is inconsistent"
                        )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO source_selection
                    VALUES (1, ?)
                    """,
                    [selection.to_json()],
                )
                connection.execute("DELETE FROM source_snapshot_current")
                for snapshot in snapshots:
                    connection.execute(
                        """
                        INSERT INTO source_snapshot_current
                        VALUES (?, ?)
                        """,
                        [snapshot.dataset_id, snapshot.content_hash],
                    )
                connection.execute("DELETE FROM derived_entity_plan_current")
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="SOURCE_SELECTION_CHANGED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="SOURCE_SELECTION_FROZEN",
                    detail=(
                        f"version {selection.version}: "
                        f"{len(selection.datasets)} dataset(s), "
                        f"{len(snapshots)} snapshot(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise


def _snapshot_matches_dataset(
    snapshot: SourceSnapshot,
    dataset: SourceDataset,
) -> bool:
    expected_schema = SourceSnapshotSchema.create(
        SourceSnapshotColumn.create(
            ordinal=column.ordinal,
            stable_key=column.stable_key,
            source_name=column.source_name,
            candidate_type=column.candidate_type,
        )
        for column in dataset.columns
    )
    return (
        snapshot.dataset_name == dataset.name
        and snapshot.file_id == dataset.file_id
        and snapshot.table_key == dataset.table_key
        and snapshot.source_sha256
        == f"sha256:{dataset.source_sha256.removeprefix('sha256:').casefold()}"
        and snapshot.catalog_hash == dataset.catalog_hash
        and snapshot.row_count == dataset.row_count
        and snapshot.schema == expected_schema
    )
