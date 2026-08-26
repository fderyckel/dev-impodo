"""Shared registry identity, root insertion, and revision helpers."""

from __future__ import annotations

from datetime import datetime

import duckdb

from ...access import Actor
from ...domain.data_version.models import (
    DataVersion,
)
from ...domain.run.models import (
    MigrationRun,
)
from ...domain.workspace.models import (
    MigrationWorkspace,
)
from ...migration_foundation import (
    MigrationConflictError,
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
    MigrationOperationIntent,
    require_uuid,
)


class FoundationRegistrySupport:
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
                    self._assert_workspace_revision(
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
                    self._assert_workspace_revision(
                        connection,
                        run.project_id,
                        intent.expected_revision,
                    )
                    self._assert_identity_available(connection, run.migration_run_id)
                    data = connection.execute(
                        "SELECT project_id FROM data_version WHERE data_version_id = ?",
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
                    self._assert_workspace_revision(
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
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        if (
            connection.execute(
                "SELECT 1 FROM migration_project WHERE project_id = ?",
                [project_id],
            ).fetchone()
            is None
        ):
            self._raise_missing_identity(connection, project_id)

    def _assert_workspace_revision(
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
                UNION ALL SELECT 'PRODUCTION_RUN_BINDING',
                  production_run_binding_id FROM production_run_binding
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
