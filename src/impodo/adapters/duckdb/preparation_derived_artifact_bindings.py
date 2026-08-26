"""Persist immutable derived-value artifacts and their session bindings."""

from __future__ import annotations

from ...domain.derived_value_artifact import DerivedValueArtifact
from ...domain.staging.preparation_session import PreparationSessionStatus
from ...workspace_errors import WorkspaceError


class PreparationDerivedArtifactBindings:
    """Own derived-artifact reuse and its building-session binding boundary."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def find(
        self,
        workspace_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> DerivedValueArtifact | None:
        """Find one historical exact derived artifact for safe reuse."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT manifest_json
                  FROM derived_value_artifact_manifest
                 WHERE dataset_id = ? AND logical_hash = ?
                 ORDER BY created_at DESC, content_hash
                 LIMIT 1
                """,
                [dataset_id, logical_hash],
            ).fetchone()
        return (
            DerivedValueArtifact.from_json(str(row[0])) if row is not None else None
        )

    def current(self, workspace_id: str) -> tuple[DerivedValueArtifact, ...]:
        """Load derived artifacts advanced only by published preparation."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
                SELECT manifest.manifest_json
                  FROM derived_value_artifact_current AS current
                  JOIN derived_value_artifact_manifest AS manifest
                    ON manifest.content_hash = current.content_hash
                 ORDER BY current.dataset_id
                """
            ).fetchall()
        return tuple(DerivedValueArtifact.from_json(str(row[0])) for row in rows)

    def session(
        self,
        workspace_id: str,
        session_id: str,
    ) -> tuple[DerivedValueArtifact, ...]:
        """Load every exact derived artifact bound to one pending session."""

        canonical_session_id = self._repository._session_id(session_id)
        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
                SELECT binding.dataset_id, binding.content_hash,
                       manifest.manifest_json
                  FROM preparation_session_derived_artifact AS binding
                  LEFT JOIN derived_value_artifact_manifest AS manifest
                    ON manifest.content_hash = binding.content_hash
                   AND manifest.dataset_id = binding.dataset_id
                 WHERE binding.session_id = ?
                 ORDER BY binding.dataset_id
                """,
                [canonical_session_id],
            ).fetchall()
        return self._repository._derived_artifacts_from_bindings(rows)

    def storage_keys(self, workspace_id: str) -> frozenset[str]:
        """Return immutable derived files referenced by any manifest."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
                SELECT parquet_storage_key
                  FROM derived_value_artifact_manifest
                 ORDER BY parquet_storage_key
                """
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def bind(
        self,
        workspace_id: str,
        session_id: str,
        artifact: DerivedValueArtifact,
    ) -> None:
        """Register a manifest and bind it to one building session atomically."""

        if artifact.workspace_id != workspace_id:
            raise WorkspaceError("Derived-value artifact belongs to another workspace")
        canonical_session_id = self._repository._session_id(session_id)
        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._repository._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                bindings = connection.execute(
                    """
                    SELECT physical_selection_hash, source_selection_hash,
                           mapping_hash, schema_hash, derived_plan_hash
                      FROM preparation_session
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if bindings != (
                    artifact.physical_selection_hash,
                    artifact.source_selection_hash,
                    artifact.mapping_hash,
                    artifact.schema_hash,
                    artifact.derived_plan_hash,
                ):
                    raise WorkspaceError(
                        "Derived-value artifact does not match the preparation session"
                    )
                self._repository._require_derived_artifact_inputs(
                    connection,
                    canonical_session_id,
                    artifact,
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO derived_value_artifact_manifest (
                        content_hash, dataset_id, logical_hash, derivation_kind,
                        physical_selection_hash, source_selection_hash,
                        derived_plan_hash, derivation_rule_hash, mapping_hash,
                        schema_hash, transformation_program_hash, lineage_hash,
                        writer_contract_version, row_count, physical_schema_hash,
                        parquet_sha256, parquet_storage_key, created_at,
                        manifest_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?)
                    """,
                    [
                        artifact.content_hash,
                        artifact.dataset_id,
                        artifact.logical_hash,
                        artifact.derivation_kind.value,
                        artifact.physical_selection_hash,
                        artifact.source_selection_hash,
                        artifact.derived_plan_hash,
                        artifact.derivation_rule_hash,
                        artifact.mapping_hash,
                        artifact.schema_hash,
                        artifact.transformation_program_hash,
                        artifact.lineage_hash,
                        artifact.writer_contract_version,
                        artifact.row_count,
                        artifact.physical_schema_hash,
                        artifact.parquet_sha256,
                        artifact.parquet_storage_key,
                        artifact.created_at.isoformat(),
                        artifact.to_json(),
                    ],
                )
                registered = connection.execute(
                    """
                    SELECT dataset_id, logical_hash, derivation_kind,
                           parquet_sha256, parquet_storage_key, manifest_json
                      FROM derived_value_artifact_manifest
                     WHERE content_hash = ?
                    """,
                    [artifact.content_hash],
                ).fetchone()
                if registered is None:
                    raise WorkspaceError(
                        "Stored derived-value artifact manifest is missing"
                    )
                stored = DerivedValueArtifact.from_json(str(registered[5]))
                if (
                    registered[:5]
                    != (
                        artifact.dataset_id,
                        artifact.logical_hash,
                        artifact.derivation_kind.value,
                        artifact.parquet_sha256,
                        artifact.parquet_storage_key,
                    )
                    or stored.content_hash != artifact.content_hash
                ):
                    raise WorkspaceError(
                        "Stored derived-value artifact manifest is inconsistent"
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO preparation_session_derived_artifact
                    VALUES (?, ?, ?)
                    """,
                    [
                        canonical_session_id,
                        artifact.dataset_id,
                        artifact.content_hash,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
