"""Read scalar workflow-navigation evidence in one workspace transaction."""

from __future__ import annotations

import json

import duckdb

from impodo.application.workspace.execution.navigation import (
    ExecutionNavigationState,
    ExecutionPreviewSummary,
)
from impodo.application.workspace.navigation import (
    WorkspaceNavigationFacts,
    WorkspaceNavigationSnapshot,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceStateNotFoundError,
)

from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository
from .serialization import _workspace_from_rows


def read_execution_navigation_state(
    connection: duckdb.DuckDBPyConnection,
) -> ExecutionNavigationState | None:
    """Read current compact execution, run, and reconciliation facts."""

    row = connection.execute(
        """
        SELECT projection.run_id, projection.snapshot_hash,
               projection.snapshot_root_hash, projection.comparison_status,
               projection.create_count, projection.update_count,
               projection.unchanged_count, projection.blocked_count,
               projection.ambiguous_count,
               projection.relationship_blocker_count,
               projection.target_hash, projection.target_odoo_version,
               projection.read_credential_binding_hash,
               projection.read_principal_hash,
               projection.read_permission_hash,
               projection.read_context_hash,
               projection.execution_shape_ready, projection.contract_version,
               execution.run_id, execution.status, reconciliation.status
          FROM preflight_current AS current
          JOIN preflight_execution_projection AS projection
            ON projection.run_id = current.run_id
          LEFT JOIN execution_current AS execution_pointer
            ON execution_pointer.singleton_id = 1
          LEFT JOIN execution_run AS execution
            ON execution.run_id = execution_pointer.run_id
           AND execution.snapshot_hash = projection.snapshot_hash
          LEFT JOIN reconciliation_current AS reconciliation_pointer
            ON reconciliation_pointer.singleton_id = 1
          LEFT JOIN reconciliation_run AS reconciliation
            ON reconciliation.reconciliation_id =
               reconciliation_pointer.reconciliation_id
           AND reconciliation.execution_run_id = execution.run_id
         WHERE current.singleton_id = 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        return ExecutionNavigationState(
            summary=ExecutionPreviewSummary(
                preflight_run_id=str(row[0]),
                snapshot_hash=str(row[1]),
                snapshot_root_hash=str(row[2]),
                comparison_status=str(row[3]),
                create_count=int(row[4]),
                update_count=int(row[5]),
                unchanged_count=int(row[6]),
                blocked_count=int(row[7]),
                ambiguous_count=int(row[8]),
                relationship_blocker_count=int(row[9]),
                target_hash=str(row[10]),
                target_odoo_version=str(row[11]),
                read_credential_binding_hash=str(row[12] or ""),
                read_principal_hash=str(row[13] or ""),
                read_permission_hash=str(row[14] or ""),
                read_context_hash=str(row[15] or ""),
                execution_shape_ready=bool(row[16]),
                contract_version=int(row[17]),
            ),
            execution_run_id=str(row[18] or ""),
            execution_status=str(row[19] or ""),
            reconciliation_status=str(row[20] or ""),
        )
    except (TypeError, ValueError):
        return None


class WorkspaceNavigationRepository(DuckDbRepository):
    """Own the bounded, display-only workspace navigation read model."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        super().__init__(database)

    def get(self, workspace_id: str) -> WorkspaceNavigationFacts:
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        try:
            with self._connect(database_path) as connection:
                self._ensure_workspace_database_schema(connection)
                connection.begin()
                facts = self._read_facts(connection, workspace_id)
                connection.rollback()
                return facts
        except (duckdb.Error, TypeError, ValueError) as error:
            raise WorkspaceError(
                "Stored workflow navigation evidence is invalid"
            ) from error

    def get_snapshot(self, workspace_id: str) -> WorkspaceNavigationSnapshot:
        """Read workspace state and navigation facts in one transaction."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        try:
            with self._connect(database_path) as connection:
                self._ensure_workspace_database_schema(connection)
                connection.begin()
                workspace_state = self._read_workspace_state(
                    connection,
                    workspace_id,
                )
                facts = self._read_facts(connection, workspace_id)
                connection.rollback()
                return WorkspaceNavigationSnapshot(workspace_state, facts)
        except (duckdb.Error, TypeError, ValueError) as error:
            raise WorkspaceError(
                "Stored workflow navigation evidence is invalid"
            ) from error

    @staticmethod
    def _read_workspace_state(
        connection: duckdb.DuckDBPyConnection,
        workspace_id: str,
    ) -> WorkspaceState:
        row = connection.execute(
            """
            SELECT singleton_id, name, source_system, source_mode,
                   data_classification, retention_days, odoo_connection_mode,
                   odoo_base_url, odoo_database, intended_applications,
                   intended_models, status, revision, created_at, updated_at,
                   registered_at, mapping_version, current_run_id,
                   approval_status, destination_odoo_connection_mode,
                   destination_odoo_base_url, destination_odoo_database,
                   destination_verified_target_hash,
                   destination_verified_credential_binding_hash,
                   destination_verified_read_principal_hash,
                   destination_verified_odoo_version,
                   destination_verified_at, destination_match_plan_json,
                   transfer_order_plan_json, transfer_review_package_json,
                   transfer_review_approval_json,
                   transfer_preflight_report_json
              FROM workspace_projection_cache
             WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        columns = [item[0] for item in connection.description]
        source_rows = connection.execute(
            """
            SELECT file_id, display_name, stored_name, size_bytes, sha256,
                   received_at
              FROM source_file
             ORDER BY received_at, file_id
            """
        ).fetchall()
        return _workspace_from_rows(
            dict(zip(columns, row, strict=True)),
            source_rows,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _read_facts(
        connection: duckdb.DuckDBPyConnection,
        workspace_id: str,
    ) -> WorkspaceNavigationFacts:
        source = connection.execute(
            """
            SELECT
                coalesce((SELECT json_extract_string(
                                    try_cast(selection_json AS JSON),
                                    '$.content_hash'
                                )
                            FROM source_selection WHERE singleton_id = 1), ''),
                (SELECT count(*) FROM source_configuration),
                (SELECT count(*) FROM source_configuration
                  WHERE json_array_length(
                      try_cast(configuration_json AS JSON),
                      '$.selected_table_keys'
                  ) > 0),
                EXISTS (
                    SELECT 1
                      FROM derived_entity_plan_current AS current
                      JOIN derived_entity_plan_revision AS revision
                        ON revision.plan_id = current.plan_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                       AND json_array_length(
                           try_cast(revision.plan_json AS JSON), '$.rules'
                       ) > 0
                )
            """
        ).fetchone()
        schema = connection.execute(
            """
            SELECT EXISTS (
                       SELECT 1 FROM odoo_model_catalog
                        WHERE singleton_id = 1
                          AND try_cast(catalog_json AS JSON) IS NOT NULL
                          AND json_extract_string(
                              try_cast(catalog_json AS JSON),
                              '$.contract_version'
                          ) = '2'
                          AND json_type(
                              try_cast(catalog_json AS JSON), '$.models'
                          ) = 'ARRAY'
                   ),
                   EXISTS (
                       SELECT 1 FROM odoo_schema_catalog
                        WHERE singleton_id = 1
                          AND try_cast(catalog_json AS JSON) IS NOT NULL
                          AND json_extract_string(
                              try_cast(catalog_json AS JSON),
                              '$.contract_version'
                          ) = '2'
                          AND json_type(
                              try_cast(catalog_json AS JSON), '$.models'
                          ) = 'ARRAY'
                          AND regexp_full_match(
                              coalesce(json_extract_string(
                                  try_cast(catalog_json AS JSON),
                                  '$.content_hash'
                              ), ''),
                              'sha256:[0-9a-f]{64}'
                          )
                   ),
                   coalesce((
                       SELECT cast(json_extract(
                           try_cast(catalog_json AS JSON), '$.models[*].name'
                       ) AS VARCHAR)
                         FROM odoo_schema_catalog WHERE singleton_id = 1
                   ), '[]'),
                   EXISTS (
                       SELECT 1 FROM odoo_schema_catalog
                        WHERE singleton_id = 1
                          AND NOT (
                              try_cast(catalog_json AS JSON) IS NOT NULL
                              AND json_extract_string(
                                  try_cast(catalog_json AS JSON),
                                  '$.contract_version'
                              ) = '2'
                              AND json_type(
                                  try_cast(catalog_json AS JSON), '$.models'
                              ) = 'ARRAY'
                              AND regexp_full_match(
                                  coalesce(json_extract_string(
                                      try_cast(catalog_json AS JSON),
                                      '$.content_hash'
                                  ), ''),
                                  'sha256:[0-9a-f]{64}'
                              )
                              AND coalesce(cast(json_extract(
                                      try_cast(catalog_json AS JSON),
                                      '$.pending_refresh'
                                  ) AS VARCHAR), 'null') = 'null'
                          )
                   ),
                   coalesce((
                       SELECT json_extract_string(
                           try_cast(catalog_json AS JSON), '$.content_hash'
                       )
                         FROM odoo_schema_catalog WHERE singleton_id = 1
                   ), ''),
                   EXISTS (
                       SELECT 1
                         FROM schema_governance_current AS current
                         JOIN schema_governance_revision AS revision
                           ON revision.governance_id = current.governance_id
                          AND revision.version = current.version
                         JOIN odoo_schema_catalog AS catalog
                           ON catalog.singleton_id = 1
                        WHERE current.singleton_id = 1
                          AND try_cast(revision.governance_json AS JSON)
                              IS NOT NULL
                          AND revision.catalog_hash = json_extract_string(
                              try_cast(catalog.catalog_json AS JSON),
                              '$.content_hash'
                          )
                   )
            """
        ).fetchone()
        capture_models = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT model FROM odoo_capture_selection_current ORDER BY model"
            ).fetchall()
        )
        mapping_complete = bool(
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM mapping_current AS current
                      JOIN mapping_revision AS revision
                        ON revision.mapping_id = current.mapping_id
                       AND revision.version = current.version
                      JOIN mapping_submission AS submission
                        ON submission.mapping_id = revision.mapping_id
                       AND submission.version = revision.version
                       AND submission.content_hash = revision.content_hash
                     WHERE current.singleton_id = 1
                       AND try_cast(revision.revision_json AS JSON) IS NOT NULL
                )
                """
            ).fetchone()[0]
        )
        preparation = connection.execute(
            """
            SELECT coalesce(staging.run_id, ''),
                   coalesce(resolution.status, ''),
                   coalesce(resolution.staging_run_id, ''),
                   coalesce(quality.run_id, ''),
                   coalesce(quality.staging_run_id, ''),
                   coalesce(normalization.run_id, ''),
                   coalesce(normalization.staging_run_id, ''),
                   coalesce(normalization.quality_run_id, ''),
                   coalesce(normalization.status, ''),
                   coalesce(normalization.decision_group_count, 0),
                   coalesce(normalization.reviewed_group_count, 0)
              FROM (SELECT 1 AS singleton_id) AS one
         LEFT JOIN canonical_staging_current AS staging_pointer
                ON staging_pointer.singleton_id = one.singleton_id
         LEFT JOIN canonical_staging_run AS staging
                ON staging.run_id = staging_pointer.run_id
               AND staging.status = 'PUBLISHED'
         LEFT JOIN resolution_current AS resolution_pointer
                ON resolution_pointer.singleton_id = one.singleton_id
         LEFT JOIN resolution_run AS resolution
                ON resolution.run_id = resolution_pointer.run_id
         LEFT JOIN quality_current AS quality_pointer
                ON quality_pointer.singleton_id = one.singleton_id
         LEFT JOIN quality_run AS quality ON quality.run_id = quality_pointer.run_id
         LEFT JOIN normalization_current AS normalization_pointer
                ON normalization_pointer.singleton_id = one.singleton_id
         LEFT JOIN normalization_run AS normalization
                ON normalization.run_id = normalization_pointer.run_id
            """
        ).fetchone()
        execution_state = read_execution_navigation_state(connection)
        preflight_status = (
            execution_state.summary.comparison_status
            if execution_state is not None
            else str(
                connection.execute(
                    """
                    SELECT CASE
                             WHEN count(dataset.run_id) = 0 THEN ''
                             WHEN count(try_cast(
                                      dataset.summary_json AS JSON
                                  )) != count(dataset.run_id) THEN 'BLOCKED'
                             WHEN count(try_cast(json_extract_string(
                                      try_cast(dataset.summary_json AS JSON),
                                      '$.blocked'
                                  ) AS BIGINT)) != count(dataset.run_id)
                                  THEN 'BLOCKED'
                             WHEN count(try_cast(json_extract_string(
                                      try_cast(dataset.summary_json AS JSON),
                                      '$.needs_review'
                                  ) AS BIGINT)) != count(dataset.run_id)
                                  THEN 'BLOCKED'
                             WHEN coalesce(sum(try_cast(json_extract_string(
                                      try_cast(dataset.summary_json AS JSON),
                                      '$.blocked'
                                  ) AS BIGINT)), 0) > 0 THEN 'BLOCKED'
                             WHEN coalesce(sum(try_cast(json_extract_string(
                                      try_cast(dataset.summary_json AS JSON),
                                      '$.needs_review'
                                  ) AS BIGINT)), 0) > 0 THEN 'NEEDS_REVIEW'
                             ELSE 'READY'
                           END
                      FROM preflight_current AS current
                      JOIN preflight_dataset AS dataset
                        ON dataset.run_id = current.run_id
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()[0]
                or ""
            )
        )
        transfer = connection.execute(
            """
            SELECT coalesce(execution.run_id, ''),
                   coalesce(reconciliation.status, '')
              FROM (SELECT 1 AS singleton_id) AS one
         LEFT JOIN execution_current AS execution_pointer
                ON execution_pointer.singleton_id = one.singleton_id
         LEFT JOIN execution_run AS execution
                ON execution.run_id = execution_pointer.run_id
         LEFT JOIN reconciliation_current AS reconciliation_pointer
                ON reconciliation_pointer.singleton_id = one.singleton_id
         LEFT JOIN reconciliation_run AS reconciliation
                ON reconciliation.reconciliation_id =
                   reconciliation_pointer.reconciliation_id
               AND reconciliation.execution_run_id = execution.run_id
            """
        ).fetchone()
        schema_models = tuple(str(item) for item in json.loads(str(schema[2])))
        return WorkspaceNavigationFacts(
            workspace_id=workspace_id,
            source_selection_hash=str(source[0]),
            source_configuration_count=int(source[1]),
            selected_source_configuration_count=int(source[2]),
            derived_rules_present=bool(source[3]),
            odoo_model_catalog_present=bool(schema[0]),
            schema_present=bool(schema[1]),
            schema_attention=bool(schema[3]),
            schema_models=schema_models,
            capture_models=capture_models,
            schema_content_hash=str(schema[4]),
            governance_present=bool(schema[5]),
            mapping_complete=mapping_complete,
            staging_run_id=str(preparation[0]),
            resolution_status=str(preparation[1]),
            resolution_staging_run_id=str(preparation[2]),
            quality_run_id=str(preparation[3]),
            quality_staging_run_id=str(preparation[4]),
            normalization_run_id=str(preparation[5]),
            normalization_staging_run_id=str(preparation[6]),
            normalization_quality_run_id=str(preparation[7]),
            normalization_status=str(preparation[8]),
            normalization_decision_count=int(preparation[9]),
            normalization_reviewed_count=int(preparation[10]),
            preflight_status=preflight_status,
            execution_state=execution_state,
            transfer_execution_run_id=str(transfer[0]),
            transfer_reconciliation_status=str(transfer[1]),
        )
