"""Persist the clean M1-M2 Project, source, run, and workspace roots."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Mapping
from uuid import uuid4

import duckdb

from ...access import Actor, ActorIdentity
from ...data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageDataset,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
    WorkspaceSourceProjection,
)
from ...data_versions import DataVersion, DataVersionPurpose, DataVersionState
from ...domain.serialization import canonical_json
from ...domain.source_binding import source_binding_from_dict
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
    MigrationOperationIntent,
    MigrationOperationKind,
    MigrationOperationReplayError,
    MigrationOperationState,
    require_hash,
    require_revision,
    require_uuid,
    utc_now,
)
from ...migration_projects import (
    MigrationDataClassification,
    MigrationProject,
    MigrationProjectStatus,
    MigrationProjectSummary,
)
from ...migration_runs import (
    MigrationRun,
    MigrationRunPurpose,
    MigrationRunState,
)
from ...migration_workspaces import (
    MigrationWorkspace,
    MigrationWorkspaceState,
)
from ...workspace_contracts import SourceDatasetColumn
from .migration_foundation_database import MigrationFoundationDatabase


class MigrationFoundationRepository:
    """Implement the M1-M2 root and source ports over exact stores."""

    def __init__(self, database: MigrationFoundationDatabase) -> None:
        self.database = database

    @property
    def registry_path(self):
        return self.database.registry_path

    def create_project(
        self,
        project: MigrationProject,
        *,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationProject:
        detail = {"project": self._project_dict(project)}
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=project.project_id,
            owner_kind="MIGRATION_PROJECT",
            owner_id=project.project_id,
            kind=MigrationOperationKind.PROJECT_CREATE,
            request_hash=request_hash,
            expected_revision=None,
            detail=detail,
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_project(intent.owner_id)
        stored = self._project_from_dict(dict(intent.detail["project"]))
        self._fault(fault, "INTENT_RESERVED")
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT project_id FROM migration_project WHERE project_id = ?",
                    [stored.project_id],
                ).fetchone()
                if existing is None:
                    self._assert_identity_available(connection, stored.project_id)
                    connection.execute(
                        "INSERT INTO migration_project_identity VALUES (?)",
                        [stored.project_id],
                    )
                    connection.execute(
                        """
                        INSERT INTO migration_project VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        self._project_values(stored),
                    )
                    self._insert_event(
                        connection,
                        project_id=stored.project_id,
                        aggregate_kind="MIGRATION_PROJECT",
                        aggregate_id=stored.project_id,
                        aggregate_revision=stored.optimistic_revision,
                        event_type="MIGRATION_PROJECT_CREATED",
                        detail={},
                        actor=actor,
                        occurred_at=stored.created_at,
                    )
                self._commit_intent(
                    connection,
                    intent.operation_id,
                    stage="REGISTRY_COMMITTED",
                    result={"project_id": stored.project_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fault(fault, "REGISTRY_COMMITTED")
        return self.get_project(stored.project_id)

    def get_project(self, project_id: str) -> MigrationProject:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            row = self._exact_row(
                connection,
                table="migration_project",
                id_column="project_id",
                identity=project_id,
                expected_kind="MIGRATION_PROJECT",
            )
        return self._project_from_row(row)

    def list_project_summaries(self) -> tuple[MigrationProjectSummary, ...]:
        with self.database.connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                WITH data_counts AS (
                    SELECT project_id, count(*) AS item_count
                      FROM data_version GROUP BY project_id
                ), run_counts AS (
                    SELECT project_id, count(*) AS item_count
                      FROM migration_run GROUP BY project_id
                ), workspace_counts AS (
                    SELECT project_id, count(*) AS item_count
                      FROM migration_workspace GROUP BY project_id
                ), recipe_counts AS (
                    SELECT project_id, count(*) AS item_count
                      FROM recipe GROUP BY project_id
                )
                SELECT p.project_id, p.display_name, p.status,
                       p.optimistic_revision,
                       coalesce(d.item_count, 0), coalesce(r.item_count, 0),
                       coalesce(w.item_count, 0), coalesce(x.item_count, 0),
                       p.updated_at
                  FROM migration_project p
             LEFT JOIN data_counts d ON d.project_id = p.project_id
             LEFT JOIN run_counts r ON r.project_id = p.project_id
             LEFT JOIN workspace_counts w ON w.project_id = p.project_id
             LEFT JOIN recipe_counts x ON x.project_id = p.project_id
                 ORDER BY p.updated_at DESC, p.project_id
                """
            ).fetchall()
        return tuple(
            MigrationProjectSummary(
                project_id=str(row[0]),
                display_name=str(row[1]),
                status=MigrationProjectStatus(str(row[2])),
                optimistic_revision=int(row[3]),
                data_version_count=int(row[4]),
                run_count=int(row[5]),
                workspace_count=int(row[6]),
                recipe_count=int(row[7]),
                updated_at=datetime.fromisoformat(str(row[8])),
            )
            for row in rows
        )

    def save_project(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationProject:
        expected_revision = require_revision(expected_revision)
        if project.optimistic_revision != expected_revision:
            raise MigrationConflictError("Project revision is stale")
        new_revision = expected_revision + 1
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                updated = connection.execute(
                    """
                    UPDATE migration_project
                       SET display_name = ?, migration_purpose = ?,
                           source_system_identity = ?, data_classification = ?,
                           retention_days = ?, status = ?,
                           optimistic_revision = ?, updated_at = ?,
                           closed_at = ?, archived_at = ?
                     WHERE project_id = ? AND optimistic_revision = ?
                     RETURNING project_id
                    """,
                    [
                        project.display_name,
                        project.migration_purpose,
                        project.source_system_identity,
                        project.data_classification.value,
                        project.retention_days,
                        project.status.value,
                        new_revision,
                        project.updated_at.isoformat(),
                        self._time(project.closed_at),
                        self._time(project.archived_at),
                        project.project_id,
                        expected_revision,
                    ],
                ).fetchone()
                if updated is None:
                    raise MigrationConflictError("Project changed; reload and retry")
                self._insert_event(
                    connection,
                    project_id=project.project_id,
                    aggregate_kind="MIGRATION_PROJECT",
                    aggregate_id=project.project_id,
                    aggregate_revision=new_revision,
                    event_type=event_type,
                    detail={},
                    actor=actor,
                    occurred_at=project.updated_at,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_project(project.project_id)

    def next_data_version_number(self, project_id: str) -> int:
        return self._next_number(project_id, "data_version", "version_number")

    def create_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_project_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersion:
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=data_version.project_id,
            owner_kind="DATA_VERSION",
            owner_id=data_version.data_version_id,
            kind=MigrationOperationKind.DATA_VERSION_CREATE,
            request_hash=request_hash,
            expected_revision=expected_project_revision,
            detail={"data_version": self._data_version_dict(data_version)},
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_data_version(intent.owner_id)
        stored = self._data_version_from_dict(dict(intent.detail["data_version"]))
        self._fault(fault, "INTENT_RESERVED")
        self._insert_data_version_if_needed(stored, intent, actor)
        self._fault(fault, "REGISTRY_COMMITTED")
        self.database.create_data_version_store(stored)
        self._fault(fault, "STORE_CREATED")
        self._finish_pending_intent(
            intent.operation_id,
            stage="STORE_LINKED",
            result={"data_version_id": stored.data_version_id},
        )
        return self.get_data_version(stored.data_version_id)

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
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
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
                    raise MigrationConflictError(
                        "DataVersion changed; reload and retry"
                    )
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
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_data_version(data_version.data_version_id)

    def get_source_package(
        self,
        data_version_id: str,
    ) -> DataVersionSourcePackage | None:
        data_version = self._get_data_version_registry(data_version_id)
        path = self.database.ensure_data_version_store(data_version)
        with self.database.connect(path) as connection:
            state = connection.execute(
                """
                SELECT revision, state, origin, package_hash, updated_at,
                       frozen_at
                  FROM source_package_state WHERE singleton_id = 1
                """
            ).fetchone()
            if state is None or int(state[0]) == 0:
                return None
            files = tuple(
                SourcePackageFile(
                    file_id=str(row[0]),
                    display_name=str(row[1]),
                    storage_key=str(row[2]),
                    size_bytes=int(row[3]),
                    sha256=str(row[4]),
                    received_at=datetime.fromisoformat(str(row[5])),
                )
                for row in connection.execute(
                    "SELECT * FROM source_package_file ORDER BY file_id"
                ).fetchall()
            )
            catalog_rows = connection.execute(
                "SELECT * FROM source_package_catalog ORDER BY file_id"
            ).fetchall()
            catalogs = tuple(
                SourcePackageCatalog(
                    file_id=str(row[0]),
                    source_sha256=str(row[1]),
                    payload=self._json_mapping(str(row[3])),
                )
                for row in catalog_rows
            )
            if any(
                item.content_hash != str(row[2])
                for item, row in zip(catalogs, catalog_rows, strict=True)
            ):
                raise MigrationConflictError(
                    "Stored source catalogue hash is inconsistent"
                )
            configuration_rows = connection.execute(
                "SELECT * FROM source_package_configuration ORDER BY file_id"
            ).fetchall()
            configurations = tuple(
                SourcePackageConfiguration(
                    file_id=str(row[0]),
                    catalog_hash=str(row[1]),
                    payload=self._json_mapping(str(row[3])),
                )
                for row in configuration_rows
            )
            if any(
                item.content_hash != str(row[2])
                for item, row in zip(
                    configurations,
                    configuration_rows,
                    strict=True,
                )
            ):
                raise MigrationConflictError(
                    "Stored source confirmation hash is inconsistent"
                )
            datasets = tuple(
                SourcePackageDataset(
                    dataset_id=str(row[0]),
                    display_name=str(row[1]),
                    source_file_ids=tuple(json.loads(str(row[2]))),
                    source=source_binding_from_dict(
                        self._json_mapping(str(row[3]))
                    ),
                    row_count=int(row[4]),
                    columns=self._source_columns(str(row[5])),
                    schema_hash=str(row[6]),
                    snapshot_hash=str(row[7]),
                    snapshot_storage_key=str(row[8]),
                    manifest=self._json_mapping(str(row[9])),
                )
                for row in connection.execute(
                    "SELECT * FROM source_package_dataset ORDER BY dataset_id"
                ).fetchall()
            )
        package = DataVersionSourcePackage(
            data_version_id=data_version.data_version_id,
            project_id=data_version.project_id,
            revision=int(state[0]),
            origin=SourcePackageOrigin(str(state[2])),
            state=SourcePackageState(str(state[1])),
            files=files,
            catalogs=catalogs,
            configurations=configurations,
            datasets=datasets,
            updated_at=datetime.fromisoformat(str(state[4])),
            frozen_at=(
                datetime.fromisoformat(str(state[5]))
                if state[5] is not None
                else None
            ),
        )
        if package.content_hash != str(state[3]):
            raise MigrationConflictError(
                "Stored source package hash is inconsistent"
            )
        return package

    def data_version_project_id(self, data_version_id: str) -> str:
        return self._get_data_version_registry(data_version_id).project_id

    def replace_draft_source_package(
        self,
        package: DataVersionSourcePackage,
        *,
        expected_package_revision: int | None,
        actor: Actor,
    ) -> DataVersionSourcePackage:
        if expected_package_revision is not None:
            expected_package_revision = require_revision(
                expected_package_revision,
                "expected_package_revision",
            )
        if package.state is not SourcePackageState.DRAFT:
            raise MigrationConflictError("Frozen source packages are immutable")
        data_version = self._get_data_version_registry(package.data_version_id)
        if (
            data_version.project_id != package.project_id
            or data_version.state is not DataVersionState.DRAFT
        ):
            raise MigrationConflictError(
                "Source package does not belong to an editable DataVersion"
            )
        path = self.database.ensure_data_version_store(data_version)
        with self.database.connect(path) as connection:
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, state FROM source_package_state "
                    "WHERE singleton_id = 1"
                ).fetchone()
                current_revision = int(current[0])
                required_current = expected_package_revision or 0
                if (
                    str(current[1]) != SourcePackageState.DRAFT.value
                    or current_revision != required_current
                    or package.revision != current_revision + 1
                ):
                    raise MigrationConflictError(
                        "Source package changed; reload and retry"
                    )
                connection.execute("DELETE FROM source_package_configuration")
                connection.execute("DELETE FROM source_package_catalog")
                connection.execute("DELETE FROM source_package_file")
                connection.execute("DELETE FROM source_package_dataset")
                self._insert_source_package(connection, package)
                connection.execute(
                    """
                    UPDATE source_package_state
                       SET revision = ?, state = 'DRAFT', origin = ?,
                           package_hash = ?, updated_at = ?, frozen_at = NULL
                     WHERE singleton_id = 1
                    """,
                    [
                        package.revision,
                        package.origin.value,
                        package.content_hash,
                        package.updated_at.isoformat(),
                    ],
                )
                self._insert_source_package_event(
                    connection,
                    revision=package.revision,
                    event_type="SOURCE_PACKAGE_DRAFT_REPLACED",
                    detail={"package_hash": package.content_hash},
                    actor=actor,
                    occurred_at=package.updated_at,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        saved = self.get_source_package(package.data_version_id)
        if saved is None:
            raise MigrationConflictError("Source package was not persisted")
        return saved

    def freeze_source_package(
        self,
        data_version_id: str,
        *,
        expected_data_version_revision: int,
        expected_package_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersionSourcePackage:
        data_version = self._get_data_version_registry(data_version_id)
        package = self.get_source_package(data_version_id)
        if package is None:
            raise MigrationConflictError("Source package is not assembled")
        package.require_acceptance_ready()
        if package.state is SourcePackageState.FROZEN:
            try:
                self.get_operation_intent(operation_id)
            except MigrationNotFoundError as error:
                raise MigrationConflictError(
                    "Frozen source packages cannot be accepted again"
                ) from error
        frozen_at = utc_now()
        detail = {
            "data_version_id": data_version_id,
            "frozen_at": frozen_at.isoformat(),
            "package_hash": package.content_hash,
            "package_revision": expected_package_revision,
        }
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=data_version.project_id,
            owner_kind="DATA_VERSION",
            owner_id=data_version_id,
            kind=MigrationOperationKind.DATA_VERSION_FREEZE,
            request_hash=request_hash,
            expected_revision=expected_data_version_revision,
            detail=detail,
            actor=actor,
        )
        stored = dict(intent.detail)
        self._fault(fault, "INTENT_RESERVED")
        self._freeze_source_store(
            data_version,
            package_hash=str(stored["package_hash"]),
            expected_package_revision=int(stored["package_revision"]),
            frozen_at=datetime.fromisoformat(str(stored["frozen_at"])),
            actor=actor,
        )
        self._fault(fault, "STORE_CREATED")
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT state, source_package_hash, optimistic_revision
                      FROM data_version WHERE data_version_id = ?
                    """,
                    [data_version_id],
                ).fetchone()
                expected = expected_data_version_revision
                if row == (DataVersionState.DRAFT.value, None, expected):
                    connection.execute(
                        """
                        UPDATE data_version
                           SET state = 'FROZEN', source_package_hash = ?,
                               optimistic_revision = ?, updated_at = ?,
                               frozen_at = ?
                         WHERE data_version_id = ?
                        """,
                        [
                            stored["package_hash"],
                            expected + 1,
                            stored["frozen_at"],
                            stored["frozen_at"],
                            data_version_id,
                        ],
                    )
                    self._insert_event(
                        connection,
                        project_id=data_version.project_id,
                        aggregate_kind="DATA_VERSION",
                        aggregate_id=data_version_id,
                        aggregate_revision=expected + 1,
                        event_type="DATA_VERSION_SOURCE_PACKAGE_FROZEN",
                        detail={"package_hash": stored["package_hash"]},
                        actor=actor,
                        occurred_at=datetime.fromisoformat(
                            str(stored["frozen_at"])
                        ),
                    )
                elif row != (
                    DataVersionState.FROZEN.value,
                    stored["package_hash"],
                    expected + 1,
                ):
                    raise MigrationConflictError(
                        "DataVersion changed before source acceptance"
                    )
                self._commit_intent(
                    connection,
                    operation_id,
                    stage="SOURCE_PACKAGE_FROZEN",
                    result={"data_version_id": data_version_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fault(fault, "REGISTRY_COMMITTED")
        frozen = self.get_source_package(data_version_id)
        if frozen is None:
            raise MigrationConflictError("Frozen source package is missing")
        return frozen

    def next_run_number(self, project_id: str) -> int:
        return self._next_number(project_id, "migration_run", "run_number")

    def create_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_project_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationRun:
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=run.project_id,
            owner_kind="MIGRATION_RUN",
            owner_id=run.migration_run_id,
            kind=MigrationOperationKind.MIGRATION_RUN_CREATE,
            request_hash=request_hash,
            expected_revision=expected_project_revision,
            detail={"migration_run": self._run_dict(run)},
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_migration_run(intent.owner_id)
        stored = self._run_from_dict(dict(intent.detail["migration_run"]))
        self._fault(fault, "INTENT_RESERVED")
        self._insert_run_if_needed(stored, intent, actor)
        self._fault(fault, "REGISTRY_COMMITTED")
        self._finish_pending_intent(
            intent.operation_id,
            stage="REGISTRY_COMMITTED",
            result={"migration_run_id": stored.migration_run_id},
        )
        return self.get_migration_run(stored.migration_run_id)

    def get_migration_run(self, migration_run_id: str) -> MigrationRun:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            row = self._exact_row(
                connection,
                table="migration_run",
                id_column="migration_run_id",
                identity=migration_run_id,
                expected_kind="MIGRATION_RUN",
            )
        return self._run_from_row(row)

    def list_migration_runs(self, project_id: str) -> tuple[MigrationRun, ...]:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            self._require_project(connection, project_id)
            rows = self._rows(
                connection,
                "SELECT * FROM migration_run WHERE project_id = ? "
                "ORDER BY run_number",
                [project_id],
            )
        return tuple(self._run_from_row(row) for row in rows)

    def save_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationRun:
        expected_revision = require_revision(expected_revision)
        if run.optimistic_revision != expected_revision:
            raise MigrationConflictError("MigrationRun revision is stale")
        new_revision = expected_revision + 1
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                updated = connection.execute(
                    """
                    UPDATE migration_run
                       SET label = ?, state = ?, target_binding_id = ?,
                           cutover_selection_id = ?, optimistic_revision = ?,
                           updated_at = ?, closed_at = ?
                     WHERE migration_run_id = ? AND optimistic_revision = ?
                     RETURNING migration_run_id
                    """,
                    [
                        run.label,
                        run.state.value,
                        run.target_binding_id,
                        run.cutover_selection_id,
                        new_revision,
                        run.updated_at.isoformat(),
                        self._time(run.closed_at),
                        run.migration_run_id,
                        expected_revision,
                    ],
                ).fetchone()
                if updated is None:
                    raise MigrationConflictError(
                        "MigrationRun changed; reload and retry"
                    )
                self._insert_event(
                    connection,
                    project_id=run.project_id,
                    aggregate_kind="MIGRATION_RUN",
                    aggregate_id=run.migration_run_id,
                    aggregate_revision=new_revision,
                    event_type=event_type,
                    detail={},
                    actor=actor,
                    occurred_at=run.updated_at,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_migration_run(run.migration_run_id)

    def create_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_project_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationWorkspace:
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=workspace.project_id,
            owner_kind="MIGRATION_WORKSPACE",
            owner_id=workspace.workspace_id,
            kind=MigrationOperationKind.MIGRATION_WORKSPACE_CREATE,
            request_hash=request_hash,
            expected_revision=expected_project_revision,
            detail={"migration_workspace": self._workspace_dict(workspace)},
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_migration_workspace(intent.owner_id)
        stored = self._workspace_from_dict(
            dict(intent.detail["migration_workspace"])
        )
        self._fault(fault, "INTENT_RESERVED")
        self._insert_workspace_if_needed(stored, intent, actor)
        self._fault(fault, "REGISTRY_COMMITTED")
        self.database.create_workspace_store(stored)
        self._fault(fault, "STORE_CREATED")
        self._finish_pending_intent(
            intent.operation_id,
            stage="STORE_LINKED",
            result={"workspace_id": stored.workspace_id},
        )
        return self.get_migration_workspace(stored.workspace_id)

    def get_migration_workspace(self, workspace_id: str) -> MigrationWorkspace:
        workspace = self._get_workspace_registry(workspace_id)
        self.database.ensure_workspace_store(workspace)
        return workspace

    def _get_workspace_registry(self, workspace_id: str) -> MigrationWorkspace:
        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            row = self._exact_row(
                connection,
                table="migration_workspace",
                id_column="workspace_id",
                identity=workspace_id,
                expected_kind="MIGRATION_WORKSPACE",
            )
        return self._workspace_from_row(row)

    def list_migration_workspaces(
        self,
        migration_run_id: str,
    ) -> tuple[MigrationWorkspace, ...]:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            if connection.execute(
                "SELECT 1 FROM migration_run WHERE migration_run_id = ?",
                [migration_run_id],
            ).fetchone() is None:
                self._raise_missing_identity(connection, migration_run_id)
            rows = self._rows(
                connection,
                "SELECT * FROM migration_workspace WHERE migration_run_id = ? "
                "ORDER BY created_at, workspace_id",
                [migration_run_id],
            )
        return tuple(self._workspace_from_row(row) for row in rows)

    def save_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationWorkspace:
        expected_revision = require_revision(expected_revision)
        if workspace.optimistic_revision != expected_revision:
            raise MigrationConflictError("MigrationWorkspace revision is stale")
        new_revision = expected_revision + 1
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                updated = connection.execute(
                    """
                    UPDATE migration_workspace
                       SET display_name = ?, state = ?, optimistic_revision = ?,
                           updated_at = ?, closed_at = ?
                     WHERE workspace_id = ? AND optimistic_revision = ?
                     RETURNING workspace_id
                    """,
                    [
                        workspace.display_name,
                        workspace.state.value,
                        new_revision,
                        workspace.updated_at.isoformat(),
                        self._time(workspace.closed_at),
                        workspace.workspace_id,
                        expected_revision,
                    ],
                ).fetchone()
                if updated is None:
                    raise MigrationConflictError(
                        "MigrationWorkspace changed; reload and retry"
                    )
                self._insert_event(
                    connection,
                    project_id=workspace.project_id,
                    aggregate_kind="MIGRATION_WORKSPACE",
                    aggregate_id=workspace.workspace_id,
                    aggregate_revision=new_revision,
                    event_type=event_type,
                    detail={},
                    actor=actor,
                    occurred_at=workspace.updated_at,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_migration_workspace(workspace.workspace_id)

    def create_workspace_source_projection(
        self,
        workspace_id: str,
        *,
        dataset_ids: tuple[str, ...],
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> WorkspaceSourceProjection:
        workspace = self.get_migration_workspace(workspace_id)
        current_projection = self.get_workspace_source_projection(workspace_id)
        if current_projection is not None:
            try:
                self.get_operation_intent(operation_id)
            except MigrationNotFoundError as error:
                raise MigrationConflictError(
                    "MigrationWorkspace already has a source projection"
                ) from error
        package = self.get_source_package(workspace.data_version_id)
        if package is None or package.state is not SourcePackageState.FROZEN:
            raise MigrationConflictError(
                "Accept the DataVersion source package before using it"
            )
        if package.project_id != workspace.project_id:
            raise MigrationConflictError(
                "Workspace and source package belong to different Projects"
            )
        requested = set(dataset_ids)
        available = {item.dataset_id for item in package.datasets}
        if not requested or not requested.issubset(available):
            raise MigrationConflictError(
                "Workspace source selection is outside its DataVersion"
            )
        now = utc_now()
        detail = {
            "created_at": now.isoformat(),
            "created_by": actor.identity.display_name,
            "dataset_ids": list(dataset_ids),
            "package_hash": package.content_hash,
            "projection_id": str(uuid4()),
        }
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=workspace.project_id,
            owner_kind="MIGRATION_WORKSPACE",
            owner_id=workspace_id,
            kind=MigrationOperationKind.WORKSPACE_SOURCE_PROJECT,
            request_hash=request_hash,
            expected_revision=expected_workspace_revision,
            detail=detail,
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            projection = self.get_workspace_source_projection(workspace_id)
            if projection is None:
                raise MigrationConflictError(
                    "Committed workspace source projection is missing"
                )
            return projection
        stored = dict(intent.detail)
        self._fault(fault, "INTENT_RESERVED")
        path = self.database.ensure_workspace_store(workspace)
        with self.database.connect(path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    """
                    SELECT projection_id, package_hash
                      FROM workspace_source_projection
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                expected_projection = (
                    str(stored["projection_id"]),
                    str(stored["package_hash"]),
                )
                if existing is None:
                    connection.execute(
                        "INSERT INTO workspace_source_projection "
                        "VALUES (1, ?, ?, ?, ?)",
                        [
                            stored["projection_id"],
                            stored["package_hash"],
                            stored["created_at"],
                            stored["created_by"],
                        ],
                    )
                    selected = tuple(
                        package.dataset(str(dataset_id))
                        for dataset_id in stored["dataset_ids"]
                    )
                    connection.executemany(
                        "INSERT INTO workspace_source_dataset VALUES (?, ?)",
                        [
                            [dataset.dataset_id, dataset.snapshot_hash]
                            for dataset in selected
                        ],
                    )
                elif existing != expected_projection:
                    raise MigrationConflictError(
                        "MigrationWorkspace already uses another source projection"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fault(fault, "STORE_CREATED")
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                row = connection.execute(
                    "SELECT optimistic_revision FROM migration_workspace "
                    "WHERE workspace_id = ?",
                    [workspace_id],
                ).fetchone()
                expected = expected_workspace_revision
                if row == (expected,):
                    connection.execute(
                        """
                        UPDATE migration_workspace
                           SET optimistic_revision = ?, updated_at = ?
                         WHERE workspace_id = ?
                        """,
                        [expected + 1, stored["created_at"], workspace_id],
                    )
                    self._insert_event(
                        connection,
                        project_id=workspace.project_id,
                        aggregate_kind="MIGRATION_WORKSPACE",
                        aggregate_id=workspace_id,
                        aggregate_revision=expected + 1,
                        event_type="WORKSPACE_SOURCE_PROJECTED",
                        detail={"package_hash": stored["package_hash"]},
                        actor=actor,
                        occurred_at=datetime.fromisoformat(
                            str(stored["created_at"])
                        ),
                    )
                elif row != (expected + 1,):
                    raise MigrationConflictError(
                        "MigrationWorkspace changed before source projection"
                    )
                self._commit_intent(
                    connection,
                    operation_id,
                    stage="WORKSPACE_SOURCE_PROJECTED",
                    result={"projection_id": stored["projection_id"]},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fault(fault, "REGISTRY_COMMITTED")
        projection = self.get_workspace_source_projection(workspace_id)
        if projection is None:
            raise MigrationConflictError(
                "Workspace source projection was not persisted"
            )
        return projection

    def workspace_project_id(self, workspace_id: str) -> str:
        return self._get_workspace_registry(workspace_id).project_id

    def get_workspace_source_projection(
        self,
        workspace_id: str,
    ) -> WorkspaceSourceProjection | None:
        workspace = self.get_migration_workspace(workspace_id)
        path = self.database.ensure_workspace_store(workspace)
        with self.database.connect(path) as connection:
            row = connection.execute(
                """
                SELECT projection_id, package_hash, created_at, created_by
                  FROM workspace_source_projection WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                return None
            dataset_rows = connection.execute(
                "SELECT dataset_id, snapshot_hash "
                "FROM workspace_source_dataset ORDER BY dataset_id"
            ).fetchall()
        package = self.get_source_package(workspace.data_version_id)
        if (
            package is None
            or package.state is not SourcePackageState.FROZEN
            or package.content_hash != str(row[1])
        ):
            raise MigrationConflictError(
                "Workspace source projection no longer matches its DataVersion"
            )
        datasets = tuple(package.dataset(str(item[0])) for item in dataset_rows)
        if any(
            dataset.snapshot_hash != str(stored[1])
            for dataset, stored in zip(datasets, dataset_rows, strict=True)
        ):
            raise MigrationConflictError(
                "Workspace source snapshot reference is inconsistent"
            )
        return WorkspaceSourceProjection(
            projection_id=str(row[0]),
            workspace_id=workspace.workspace_id,
            project_id=workspace.project_id,
            data_version_id=workspace.data_version_id,
            package_hash=str(row[1]),
            datasets=datasets,
            created_at=datetime.fromisoformat(str(row[2])),
            created_by=str(row[3]),
        )

    def get_operation_intent(self, operation_id: str) -> MigrationOperationIntent:
        operation_id = require_uuid(operation_id, "operation_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self._rows(
                connection,
                "SELECT * FROM project_operation_intent WHERE operation_id = ?",
                [operation_id],
            )
        if not rows:
            raise MigrationNotFoundError("Operation intent not found")
        return self._intent_from_row(rows[0])

    def _insert_data_version_if_needed(
        self,
        data_version: DataVersion,
        intent: MigrationOperationIntent,
        actor: Actor,
    ) -> None:
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT data_version_id FROM data_version "
                    "WHERE data_version_id = ?",
                    [data_version.data_version_id],
                ).fetchone()
                if existing is None:
                    self._assert_project_revision(
                        connection,
                        data_version.project_id,
                        intent.expected_revision,
                    )
                    self._assert_identity_available(
                        connection,
                        data_version.data_version_id,
                    )
                    if data_version.parent_data_version_id is not None:
                        parent = connection.execute(
                            "SELECT project_id FROM data_version "
                            "WHERE data_version_id = ?",
                            [data_version.parent_data_version_id],
                        ).fetchone()
                        if parent is None or str(parent[0]) != data_version.project_id:
                            raise MigrationConflictError(
                                "DataVersion parent does not belong to this Project"
                            )
                    connection.execute(
                        "INSERT INTO data_version_identity VALUES (?)",
                        [data_version.data_version_id],
                    )
                    connection.execute(
                        "INSERT INTO data_version VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._data_version_values(data_version),
                    )
                    next_revision = self._advance_project(
                        connection,
                        data_version.project_id,
                        intent.expected_revision,
                        data_version.updated_at,
                    )
                    self._insert_event(
                        connection,
                        project_id=data_version.project_id,
                        aggregate_kind="DATA_VERSION",
                        aggregate_id=data_version.data_version_id,
                        aggregate_revision=data_version.optimistic_revision,
                        event_type="DATA_VERSION_CREATED",
                        detail={"project_revision": next_revision},
                        actor=actor,
                        occurred_at=data_version.created_at,
                    )
                self._set_pending_stage(
                    connection,
                    intent.operation_id,
                    "REGISTRY_COMMITTED",
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _insert_run_if_needed(
        self,
        run: MigrationRun,
        intent: MigrationOperationIntent,
        actor: Actor,
    ) -> None:
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT migration_run_id FROM migration_run "
                    "WHERE migration_run_id = ?",
                    [run.migration_run_id],
                ).fetchone()
                if existing is None:
                    self._assert_project_revision(
                        connection,
                        run.project_id,
                        intent.expected_revision,
                    )
                    self._assert_identity_available(connection, run.migration_run_id)
                    data = connection.execute(
                        "SELECT project_id FROM data_version "
                        "WHERE data_version_id = ?",
                        [run.data_version_id],
                    ).fetchone()
                    if data is None or str(data[0]) != run.project_id:
                        raise MigrationConflictError(
                            "MigrationRun DataVersion does not belong to this Project"
                        )
                    connection.execute(
                        "INSERT INTO migration_run_identity VALUES (?)",
                        [run.migration_run_id],
                    )
                    connection.execute(
                        "INSERT INTO migration_run VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._run_values(run),
                    )
                    next_revision = self._advance_project(
                        connection,
                        run.project_id,
                        intent.expected_revision,
                        run.updated_at,
                    )
                    self._insert_event(
                        connection,
                        project_id=run.project_id,
                        aggregate_kind="MIGRATION_RUN",
                        aggregate_id=run.migration_run_id,
                        aggregate_revision=run.optimistic_revision,
                        event_type="MIGRATION_RUN_CREATED",
                        detail={"project_revision": next_revision},
                        actor=actor,
                        occurred_at=run.created_at,
                    )
                self._set_pending_stage(
                    connection,
                    intent.operation_id,
                    "REGISTRY_COMMITTED",
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _insert_workspace_if_needed(
        self,
        workspace: MigrationWorkspace,
        intent: MigrationOperationIntent,
        actor: Actor,
    ) -> None:
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT workspace_id FROM migration_workspace "
                    "WHERE workspace_id = ?",
                    [workspace.workspace_id],
                ).fetchone()
                if existing is None:
                    self._assert_project_revision(
                        connection,
                        workspace.project_id,
                        intent.expected_revision,
                    )
                    self._assert_identity_available(connection, workspace.workspace_id)
                    run = connection.execute(
                        "SELECT project_id, data_version_id FROM migration_run "
                        "WHERE migration_run_id = ?",
                        [workspace.migration_run_id],
                    ).fetchone()
                    if run != (
                        workspace.project_id,
                        workspace.data_version_id,
                    ):
                        raise MigrationConflictError(
                            "MigrationWorkspace does not match its run context"
                        )
                    if workspace.recipe_application_id is not None:
                        application = connection.execute(
                            "SELECT project_id, migration_run_id, data_version_id "
                            "FROM recipe_application WHERE application_id = ?",
                            [workspace.recipe_application_id],
                        ).fetchone()
                        if application != (
                            workspace.project_id,
                            workspace.migration_run_id,
                            workspace.data_version_id,
                        ):
                            raise MigrationConflictError(
                                "RecipeApplication does not match workspace context"
                            )
                    connection.execute(
                        "INSERT INTO migration_workspace_identity VALUES (?)",
                        [workspace.workspace_id],
                    )
                    connection.execute(
                        "INSERT INTO migration_workspace VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._workspace_values(workspace),
                    )
                    next_revision = self._advance_project(
                        connection,
                        workspace.project_id,
                        intent.expected_revision,
                        workspace.updated_at,
                    )
                    self._insert_event(
                        connection,
                        project_id=workspace.project_id,
                        aggregate_kind="MIGRATION_WORKSPACE",
                        aggregate_id=workspace.workspace_id,
                        aggregate_revision=workspace.optimistic_revision,
                        event_type="MIGRATION_WORKSPACE_CREATED",
                        detail={"project_revision": next_revision},
                        actor=actor,
                        occurred_at=workspace.created_at,
                    )
                self._set_pending_stage(
                    connection,
                    intent.operation_id,
                    "REGISTRY_COMMITTED",
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _reserve_intent(
        self,
        *,
        operation_id: str,
        project_id: str,
        owner_kind: str,
        owner_id: str,
        kind: MigrationOperationKind,
        request_hash: str,
        expected_revision: int | None,
        detail: Mapping[str, object],
        actor: Actor,
    ) -> MigrationOperationIntent:
        operation_id = require_uuid(operation_id, "operation_id")
        project_id = require_uuid(project_id, "project_id")
        owner_id = require_uuid(owner_id, "owner_id")
        request_hash = require_hash(request_hash, "request_hash")
        if expected_revision is not None:
            expected_revision = require_revision(
                expected_revision,
                "expected_revision",
            )
        with self.database.connect(self.registry_path) as connection:
            rows = self._rows(
                connection,
                "SELECT * FROM project_operation_intent WHERE operation_id = ?",
                [operation_id],
            )
            if rows:
                current = self._intent_from_row(rows[0])
                if (
                    (
                        current.project_id != project_id
                        and kind is not MigrationOperationKind.PROJECT_CREATE
                    )
                    or current.owner_kind != owner_kind
                    or current.kind is not kind
                    or current.request_hash != request_hash
                    or current.expected_revision
                    != expected_revision
                    or current.actor.issuer != actor.identity.issuer
                    or current.actor.subject_id != actor.identity.subject_id
                ):
                    raise MigrationOperationReplayError(
                        "Operation identity was already used with different meaning"
                    )
                return current
            now = utc_now()
            connection.execute(
                """
                INSERT INTO project_operation_intent VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'INTENT_RESERVED',
                    ?, '{}', '', ?, ?, ?, ?, ?
                )
                """,
                [
                    operation_id,
                    project_id,
                    owner_kind,
                    owner_id,
                    kind.value,
                    request_hash,
                    expected_revision,
                    canonical_json(detail),
                    actor.identity.issuer,
                    actor.identity.subject_id,
                    actor.identity.display_name,
                    now.isoformat(),
                    now.isoformat(),
                ],
            )
        return self.get_operation_intent(operation_id)

    def _finish_pending_intent(
        self,
        operation_id: str,
        *,
        stage: str,
        result: Mapping[str, object],
    ) -> None:
        with self.database.connect(self.registry_path) as connection:
            updated = connection.execute(
                """
                UPDATE project_operation_intent
                   SET state = 'COMMITTED', stage = ?, result_json = ?,
                       last_error = '', updated_at = ?
                 WHERE operation_id = ? AND state = 'PENDING'
                 RETURNING operation_id
                """,
                [stage, canonical_json(result), utc_now().isoformat(), operation_id],
            ).fetchone()
        if updated is None:
            current = self.get_operation_intent(operation_id)
            if current.state is not MigrationOperationState.COMMITTED:
                raise MigrationConflictError("Operation intent cannot commit")

    @staticmethod
    def _commit_intent(
        connection: duckdb.DuckDBPyConnection,
        operation_id: str,
        *,
        stage: str,
        result: Mapping[str, object],
    ) -> None:
        connection.execute(
            """
            UPDATE project_operation_intent
               SET state = 'COMMITTED', stage = ?, result_json = ?,
                   last_error = '', updated_at = ?
             WHERE operation_id = ? AND state = 'PENDING'
            """,
            [stage, canonical_json(result), utc_now().isoformat(), operation_id],
        )

    @staticmethod
    def _set_pending_stage(
        connection: duckdb.DuckDBPyConnection,
        operation_id: str,
        stage: str,
    ) -> None:
        connection.execute(
            """
            UPDATE project_operation_intent
               SET stage = ?, updated_at = ?
             WHERE operation_id = ? AND state = 'PENDING'
            """,
            [stage, utc_now().isoformat(), operation_id],
        )

    def _next_number(self, project_id: str, table: str, column: str) -> int:
        project_id = require_uuid(project_id, "project_id")
        if (table, column) not in {
            ("data_version", "version_number"),
            ("migration_run", "run_number"),
        }:
            raise ValueError("Unsupported lineage counter")
        with self.database.connect(self.registry_path) as connection:
            self._require_project(connection, project_id)
            row = connection.execute(
                f"SELECT coalesce(max({column}), 0) + 1 FROM {table} "
                "WHERE project_id = ?",
                [project_id],
            ).fetchone()
        return int(row[0])

    def _require_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project_id: str,
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM migration_project WHERE project_id = ?",
            [project_id],
        ).fetchone() is None:
            self._raise_missing_identity(connection, project_id)

    def _assert_project_revision(
        self,
        connection: duckdb.DuckDBPyConnection,
        project_id: str,
        expected_revision: int | None,
    ) -> None:
        if expected_revision is None:
            raise MigrationConflictError("Project revision is required")
        row = connection.execute(
            "SELECT optimistic_revision FROM migration_project WHERE project_id = ?",
            [project_id],
        ).fetchone()
        if row is None:
            self._raise_missing_identity(connection, project_id)
        if int(row[0]) != expected_revision:
            raise MigrationConflictError("Project changed; reload and retry")

    @staticmethod
    def _advance_project(
        connection: duckdb.DuckDBPyConnection,
        project_id: str,
        expected_revision: int | None,
        updated_at: datetime,
    ) -> int:
        if expected_revision is None:
            raise MigrationConflictError("Project revision is required")
        next_revision = expected_revision + 1
        updated = connection.execute(
            """
            UPDATE migration_project
               SET optimistic_revision = ?, updated_at = ?
             WHERE project_id = ? AND optimistic_revision = ?
             RETURNING project_id
            """,
            [next_revision, updated_at.isoformat(), project_id, expected_revision],
        ).fetchone()
        if updated is None:
            raise MigrationConflictError("Project changed; reload and retry")
        return next_revision

    @classmethod
    def _exact_row(
        cls,
        connection: duckdb.DuckDBPyConnection,
        *,
        table: str,
        id_column: str,
        identity: str,
        expected_kind: str,
    ) -> dict[str, object]:
        permitted = {
            ("migration_project", "project_id", "MIGRATION_PROJECT"),
            ("data_version", "data_version_id", "DATA_VERSION"),
            ("migration_run", "migration_run_id", "MIGRATION_RUN"),
            ("migration_workspace", "workspace_id", "MIGRATION_WORKSPACE"),
        }
        if (table, id_column, expected_kind) not in permitted:
            raise ValueError("Unsupported aggregate lookup")
        rows = cls._rows(
            connection,
            f"SELECT * FROM {table} WHERE {id_column} = ?",
            [identity],
        )
        if rows:
            return rows[0]
        cls._raise_missing_identity(connection, identity)
        raise AssertionError("Missing identity lookup did not raise")

    @classmethod
    def _raise_missing_identity(
        cls,
        connection: duckdb.DuckDBPyConnection,
        identity: str,
    ) -> None:
        owner = cls._identity_owner(connection, identity)
        if owner is not None:
            raise MigrationIdentifierConfusionError(
                f"Identifier belongs to {owner}, not the requested aggregate"
            )
        raise MigrationNotFoundError("Migration aggregate not found")

    @classmethod
    def _assert_identity_available(
        cls,
        connection: duckdb.DuckDBPyConnection,
        identity: str,
    ) -> None:
        owner = cls._identity_owner(connection, identity)
        if owner is not None:
            raise MigrationIdentifierConfusionError(
                f"Identifier already belongs to {owner}"
            )

    @staticmethod
    def _identity_owner(
        connection: duckdb.DuckDBPyConnection,
        identity: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT owner_kind FROM (
                SELECT 'MIGRATION_PROJECT' AS owner_kind, project_id AS identity
                  FROM migration_project
                UNION ALL SELECT 'DATA_VERSION', data_version_id FROM data_version
                UNION ALL SELECT 'MIGRATION_RUN', migration_run_id FROM migration_run
                UNION ALL SELECT 'MIGRATION_WORKSPACE', workspace_id
                  FROM migration_workspace
                UNION ALL SELECT 'TARGET_BINDING', target_binding_id
                  FROM target_binding
                UNION ALL SELECT 'RECIPE', recipe_id FROM recipe
                UNION ALL SELECT 'RECIPE_APPLICATION', application_id
                  FROM recipe_application
                UNION ALL SELECT 'RECIPE_QUALIFICATION', qualification_id
                  FROM recipe_qualification
                UNION ALL SELECT 'CUTOVER_PLAN', cutover_plan_id FROM cutover_plan
                UNION ALL SELECT 'PLAN_QUALIFICATION', qualification_id
                  FROM cutover_plan_qualification
                UNION ALL SELECT 'CUTOVER_SELECTION', cutover_selection_id
                  FROM project_cutover_selection
            ) identities
            WHERE identity = ?
            LIMIT 1
            """,
            [identity],
        ).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _rows(
        connection: duckdb.DuckDBPyConnection,
        query: str,
        parameters: list[object],
    ) -> list[dict[str, object]]:
        rows = connection.execute(query, parameters).fetchall()
        columns = [str(item[0]) for item in connection.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    @staticmethod
    def _insert_source_package(
        connection: duckdb.DuckDBPyConnection,
        package: DataVersionSourcePackage,
    ) -> None:
        if package.files:
            connection.executemany(
                "INSERT INTO source_package_file VALUES (?, ?, ?, ?, ?, ?)",
                [
                    [
                        item.file_id,
                        item.display_name,
                        item.storage_key,
                        item.size_bytes,
                        item.sha256,
                        item.received_at.isoformat(),
                    ]
                    for item in package.files
                ],
            )
        if package.catalogs:
            connection.executemany(
                "INSERT INTO source_package_catalog VALUES (?, ?, ?, ?)",
                [
                    [
                        item.file_id,
                        item.source_sha256,
                        item.content_hash,
                        canonical_json(item.payload),
                    ]
                    for item in package.catalogs
                ],
            )
        if package.configurations:
            connection.executemany(
                "INSERT INTO source_package_configuration VALUES (?, ?, ?, ?)",
                [
                    [
                        item.file_id,
                        item.catalog_hash,
                        item.content_hash,
                        canonical_json(item.payload),
                    ]
                    for item in package.configurations
                ],
            )
        if package.datasets:
            connection.executemany(
                "INSERT INTO source_package_dataset "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        item.dataset_id,
                        item.display_name,
                        canonical_json(list(item.source_file_ids)),
                        canonical_json(item.source.to_dict()),
                        item.row_count,
                        canonical_json(
                            [
                                {
                                    "candidate_type": column.candidate_type,
                                    "ordinal": column.ordinal,
                                    "source_name": column.source_name,
                                    "stable_key": column.stable_key,
                                }
                                for column in item.columns
                            ]
                        ),
                        item.schema_hash,
                        item.snapshot_hash,
                        item.snapshot_storage_key,
                        canonical_json(item.manifest),
                    ]
                    for item in package.datasets
                ],
            )

    def _freeze_source_store(
        self,
        data_version: DataVersion,
        *,
        package_hash: str,
        expected_package_revision: int,
        frozen_at: datetime,
        actor: Actor,
    ) -> None:
        path = self.database.ensure_data_version_store(data_version)
        with self.database.connect(path) as connection:
            connection.begin()
            try:
                state = connection.execute(
                    """
                    SELECT revision, state, package_hash
                      FROM source_package_state WHERE singleton_id = 1
                    """
                ).fetchone()
                expected_draft = (
                    expected_package_revision,
                    SourcePackageState.DRAFT.value,
                    package_hash,
                )
                expected_frozen = (
                    expected_package_revision + 1,
                    SourcePackageState.FROZEN.value,
                    package_hash,
                )
                if state == expected_draft:
                    connection.execute(
                        """
                        UPDATE source_package_state
                           SET revision = ?, state = 'FROZEN', frozen_at = ?,
                               updated_at = ?
                         WHERE singleton_id = 1
                        """,
                        [
                            expected_package_revision + 1,
                            frozen_at.isoformat(),
                            frozen_at.isoformat(),
                        ],
                    )
                    connection.execute(
                        """
                        UPDATE data_version_identity
                           SET state = 'FROZEN', source_package_hash = ?
                         WHERE singleton_id = 1
                        """,
                        [package_hash],
                    )
                    self._insert_source_package_event(
                        connection,
                        revision=expected_package_revision + 1,
                        event_type="SOURCE_PACKAGE_FROZEN",
                        detail={"package_hash": package_hash},
                        actor=actor,
                        occurred_at=frozen_at,
                    )
                elif state != expected_frozen:
                    raise MigrationConflictError(
                        "Source package changed before acceptance"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _insert_source_package_event(
        connection: duckdb.DuckDBPyConnection,
        *,
        revision: int,
        event_type: str,
        detail: Mapping[str, object],
        actor: Actor,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO source_package_event VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(uuid4()),
                revision,
                event_type,
                canonical_json(detail),
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
                occurred_at.isoformat(),
            ],
        )

    @staticmethod
    def _json_mapping(value: str) -> Mapping[str, object]:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise MigrationConflictError(
                "Stored source package payload is invalid"
            )
        return payload

    @staticmethod
    def _source_columns(value: str) -> tuple[SourceDatasetColumn, ...]:
        payload = json.loads(value)
        if not isinstance(payload, list):
            raise MigrationConflictError(
                "Stored source dataset columns are invalid"
            )
        if any(not isinstance(item, dict) for item in payload):
            raise MigrationConflictError(
                "Stored source dataset columns are invalid"
            )
        try:
            return tuple(
                SourceDatasetColumn(
                    ordinal=int(item["ordinal"]),
                    source_name=str(item["source_name"]),
                    stable_key=str(item["stable_key"]),
                    candidate_type=str(item["candidate_type"]),
                )
                for item in payload
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MigrationConflictError(
                "Stored source dataset columns are invalid"
            ) from error

    @staticmethod
    def _insert_event(
        connection: duckdb.DuckDBPyConnection,
        *,
        project_id: str,
        aggregate_kind: str,
        aggregate_id: str,
        aggregate_revision: int,
        event_type: str,
        detail: Mapping[str, object],
        actor: Actor,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO migration_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(uuid4()),
                project_id,
                aggregate_kind,
                aggregate_id,
                aggregate_revision,
                event_type,
                canonical_json(detail),
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
                occurred_at.isoformat(),
            ],
        )

    @staticmethod
    def _fault(fault: FaultInjector | None, stage: str) -> None:
        if fault is not None:
            fault(stage)

    @staticmethod
    def _time(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _project_values(project: MigrationProject) -> list[object]:
        return [
            project.project_id,
            project.display_name,
            project.migration_purpose,
            project.source_system_identity,
            project.data_classification.value,
            project.retention_days,
            project.status.value,
            project.optimistic_revision,
            project.created_at.isoformat(),
            project.updated_at.isoformat(),
            MigrationFoundationRepository._time(project.closed_at),
            MigrationFoundationRepository._time(project.archived_at),
        ]

    @staticmethod
    def _project_dict(project: MigrationProject) -> dict[str, object]:
        return {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "migration_purpose": project.migration_purpose,
            "source_system_identity": project.source_system_identity,
            "data_classification": project.data_classification.value,
            "retention_days": project.retention_days,
            "status": project.status.value,
            "optimistic_revision": project.optimistic_revision,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "closed_at": MigrationFoundationRepository._time(project.closed_at),
            "archived_at": MigrationFoundationRepository._time(project.archived_at),
        }

    @staticmethod
    def _project_from_dict(value: Mapping[str, object]) -> MigrationProject:
        return MigrationProject(
            project_id=str(value["project_id"]),
            display_name=str(value["display_name"]),
            migration_purpose=str(value["migration_purpose"]),
            source_system_identity=str(value["source_system_identity"]),
            data_classification=MigrationDataClassification(
                str(value["data_classification"])
            ),
            retention_days=int(value["retention_days"]),
            status=MigrationProjectStatus(str(value["status"])),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            closed_at=MigrationFoundationRepository._optional_time(
                value.get("closed_at")
            ),
            archived_at=MigrationFoundationRepository._optional_time(
                value.get("archived_at")
            ),
        )

    @classmethod
    def _project_from_row(cls, value: Mapping[str, object]) -> MigrationProject:
        return cls._project_from_dict(value)

    @staticmethod
    def _data_version_values(value: DataVersion) -> list[object]:
        return [
            value.data_version_id,
            value.project_id,
            value.version_number,
            value.parent_data_version_id,
            value.purpose.value,
            value.state.value,
            value.label,
            value.export_as_of,
            value.source_package_hash,
            value.optimistic_revision,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
            MigrationFoundationRepository._time(value.frozen_at),
        ]

    @staticmethod
    def _data_version_dict(value: DataVersion) -> dict[str, object]:
        return {
            "data_version_id": value.data_version_id,
            "project_id": value.project_id,
            "version_number": value.version_number,
            "parent_data_version_id": value.parent_data_version_id,
            "purpose": value.purpose.value,
            "state": value.state.value,
            "label": value.label,
            "export_as_of": value.export_as_of,
            "source_package_hash": value.source_package_hash,
            "optimistic_revision": value.optimistic_revision,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "frozen_at": MigrationFoundationRepository._time(value.frozen_at),
        }

    @staticmethod
    def _data_version_from_dict(value: Mapping[str, object]) -> DataVersion:
        return DataVersion(
            data_version_id=str(value["data_version_id"]),
            project_id=str(value["project_id"]),
            version_number=int(value["version_number"]),
            parent_data_version_id=(
                str(value["parent_data_version_id"])
                if value.get("parent_data_version_id")
                else None
            ),
            purpose=DataVersionPurpose(str(value["purpose"])),
            state=DataVersionState(str(value["state"])),
            label=str(value["label"]),
            export_as_of=str(value["export_as_of"]),
            source_package_hash=(
                str(value["source_package_hash"])
                if value.get("source_package_hash")
                else None
            ),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            frozen_at=MigrationFoundationRepository._optional_time(
                value.get("frozen_at")
            ),
        )

    @classmethod
    def _data_version_from_row(cls, value: Mapping[str, object]) -> DataVersion:
        return cls._data_version_from_dict(value)

    @staticmethod
    def _run_values(value: MigrationRun) -> list[object]:
        return [
            value.migration_run_id,
            value.project_id,
            value.data_version_id,
            value.run_number,
            value.purpose.value,
            value.label,
            value.state.value,
            value.target_binding_id,
            value.cutover_selection_id,
            value.optimistic_revision,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
            MigrationFoundationRepository._time(value.closed_at),
        ]

    @staticmethod
    def _run_dict(value: MigrationRun) -> dict[str, object]:
        return {
            "migration_run_id": value.migration_run_id,
            "project_id": value.project_id,
            "data_version_id": value.data_version_id,
            "run_number": value.run_number,
            "purpose": value.purpose.value,
            "label": value.label,
            "state": value.state.value,
            "target_binding_id": value.target_binding_id,
            "cutover_selection_id": value.cutover_selection_id,
            "optimistic_revision": value.optimistic_revision,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "closed_at": MigrationFoundationRepository._time(value.closed_at),
        }

    @staticmethod
    def _run_from_dict(value: Mapping[str, object]) -> MigrationRun:
        return MigrationRun(
            migration_run_id=str(value["migration_run_id"]),
            project_id=str(value["project_id"]),
            data_version_id=str(value["data_version_id"]),
            run_number=int(value["run_number"]),
            purpose=MigrationRunPurpose(str(value["purpose"])),
            label=str(value["label"]),
            state=MigrationRunState(str(value["state"])),
            target_binding_id=(
                str(value["target_binding_id"])
                if value.get("target_binding_id")
                else None
            ),
            cutover_selection_id=(
                str(value["cutover_selection_id"])
                if value.get("cutover_selection_id")
                else None
            ),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            closed_at=MigrationFoundationRepository._optional_time(
                value.get("closed_at")
            ),
        )

    @classmethod
    def _run_from_row(cls, value: Mapping[str, object]) -> MigrationRun:
        return cls._run_from_dict(value)

    @staticmethod
    def _workspace_values(value: MigrationWorkspace) -> list[object]:
        return [
            value.workspace_id,
            value.project_id,
            value.data_version_id,
            value.migration_run_id,
            value.recipe_application_id,
            value.display_name,
            value.state.value,
            value.optimistic_revision,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
            MigrationFoundationRepository._time(value.closed_at),
        ]

    @staticmethod
    def _workspace_dict(value: MigrationWorkspace) -> dict[str, object]:
        return {
            "workspace_id": value.workspace_id,
            "project_id": value.project_id,
            "data_version_id": value.data_version_id,
            "migration_run_id": value.migration_run_id,
            "recipe_application_id": value.recipe_application_id,
            "display_name": value.display_name,
            "state": value.state.value,
            "optimistic_revision": value.optimistic_revision,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "closed_at": MigrationFoundationRepository._time(value.closed_at),
        }

    @staticmethod
    def _workspace_from_dict(value: Mapping[str, object]) -> MigrationWorkspace:
        return MigrationWorkspace(
            workspace_id=str(value["workspace_id"]),
            project_id=str(value["project_id"]),
            data_version_id=str(value["data_version_id"]),
            migration_run_id=str(value["migration_run_id"]),
            recipe_application_id=(
                str(value["recipe_application_id"])
                if value.get("recipe_application_id")
                else None
            ),
            display_name=str(value["display_name"]),
            state=MigrationWorkspaceState(str(value["state"])),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            closed_at=MigrationFoundationRepository._optional_time(
                value.get("closed_at")
            ),
        )

    @classmethod
    def _workspace_from_row(
        cls,
        value: Mapping[str, object],
    ) -> MigrationWorkspace:
        return cls._workspace_from_dict(value)

    @staticmethod
    def _intent_from_row(value: Mapping[str, object]) -> MigrationOperationIntent:
        return MigrationOperationIntent(
            operation_id=str(value["operation_id"]),
            project_id=str(value["project_id"]),
            owner_kind=str(value["owner_kind"]),
            owner_id=str(value["owner_id"]),
            kind=MigrationOperationKind(str(value["kind"])),
            request_hash=str(value["request_hash"]),
            expected_revision=(
                int(value["expected_revision"])
                if value.get("expected_revision") is not None
                else None
            ),
            state=MigrationOperationState(str(value["state"])),
            stage=str(value["stage"]),
            detail=json.loads(str(value["detail_json"])),
            last_error=str(value["last_error"]),
            actor=ActorIdentity(
                issuer=str(value["actor_issuer"]),
                subject_id=str(value["actor_subject"]),
                display_name=str(value["actor_display_name"]),
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )

    @staticmethod
    def _optional_time(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value else None
