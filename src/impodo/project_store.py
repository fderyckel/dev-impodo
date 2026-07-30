"""Hardened DuckDB persistence for local migration projects."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Iterable, Iterator
from uuid import UUID

import duckdb

from .access import Actor
from .inspection import SourceFileCatalog, SourceInspectionError
from .mapping_semantics import (
    MappingRevision,
    MappingSubmission,
    MappingValidationResult,
    MappingValidationStatus,
    SchemaGovernance,
    mapping_issue_fingerprint,
)
from .projects import (
    ApprovalStatus,
    DataClassification,
    ExportStatus,
    MigrationProject,
    OdooConnectionMode,
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectSummary,
    SourceFile,
    TargetEnvironment,
)
from .workspace import (
    MappingDraft,
    OdooModelCatalog,
    OdooSchemaCatalog,
    SourceConfiguration,
    SourceSelection,
    WorkspaceError,
)


SCHEMA_VERSION = 7


class DuckDbProjectRepository:
    """Store a minimal registry plus one isolated database per project."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "registry.duckdb"
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS project_registry (
                    project_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )

    def create(self, project: MigrationProject, *, actor: Actor) -> None:
        project_dir = self.project_directory(project.project_id)
        project_dir.mkdir(parents=False, exist_ok=False)
        for child in ("inbox", "staging", "snapshots", "reports", "audit"):
            (project_dir / child).mkdir()
        database_path = project_dir / "project.duckdb"
        with self._connect(database_path) as connection:
            self._initialize_project_database(connection)
            self._insert_project(connection, project)
            self._insert_audit(
                connection,
                project,
                event_type="PROJECT_CREATED",
                detail="",
                actor=actor,
            )
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                INSERT INTO project_registry VALUES (?, ?, ?, ?, ?)
                """,
                [
                    project.project_id,
                    project.name,
                    project.status.value,
                    project.revision,
                    project.updated_at.isoformat(),
                ],
            )
    def get(self, project_id: str) -> MigrationProject:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute("SELECT * FROM project").fetchone()
            if row is None:
                raise ProjectNotFoundError("Project not found")
            columns = [item[0] for item in connection.description]
            data = dict(zip(columns, row, strict=True))
            source_rows = connection.execute(
                """
                SELECT file_id, display_name, stored_name, size_bytes, sha256,
                       received_at
                  FROM source_file
                 ORDER BY received_at, file_id
                """
            ).fetchall()
        return _project_from_rows(data, source_rows)

    def list(self) -> tuple[ProjectSummary, ...]:
        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT project_id, name, status, revision, updated_at
                  FROM project_registry
                 ORDER BY updated_at DESC, project_id
                """
            ).fetchall()
        return tuple(
            ProjectSummary(
                project_id=row[0],
                name=row[1],
                status=ProjectStatus(row[2]),
                revision=row[3],
                updated_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        )

    def get_source_catalogs(
        self,
        project_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        """Load source catalogs in the same order as registered source files."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
            self._migrate_project_database(connection)
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
                connection.execute("DELETE FROM mapping_draft")
                connection.execute("DELETE FROM mapping_current")
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
            self._migrate_project_database(connection)
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
                connection.execute("DELETE FROM mapping_draft")
                connection.execute("DELETE FROM mapping_current")
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
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
                connection.execute("DELETE FROM mapping_draft")
                connection.execute("DELETE FROM mapping_current")
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
        value = self._read_singleton_json(
            project_id,
            "SELECT selection_json FROM source_selection WHERE singleton_id = 1",
        )
        return SourceSelection.from_json(value) if value else None

    def save_source_selection(
        self,
        project_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None:
        self._save_singleton(
            project_id,
            table="source_selection",
            value_column="selection_json",
            value=selection.to_json(),
            event_type="SOURCE_SELECTION_FROZEN",
            detail=f"version {selection.version}: {len(selection.datasets)} dataset(s)",
            actor=actor,
            invalidate=("mapping_draft", "mapping_current"),
        )

    def get_odoo_schema_catalog(
        self,
        project_id: str,
    ) -> OdooSchemaCatalog | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT catalog_json
              FROM odoo_schema_catalog
             WHERE singleton_id = 1
            """,
        )
        return OdooSchemaCatalog.from_json(value) if value else None

    def get_odoo_model_catalog(
        self,
        project_id: str,
    ) -> OdooModelCatalog | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT catalog_json
              FROM odoo_model_catalog
             WHERE singleton_id = 1
            """,
        )
        return OdooModelCatalog.from_json(value) if value else None

    def save_odoo_model_catalog(
        self,
        project_id: str,
        catalog: OdooModelCatalog,
        *,
        actor: Actor,
    ) -> None:
        self._save_singleton(
            project_id,
            table="odoo_model_catalog",
            value_column="catalog_json",
            value=catalog.to_json(),
            event_type="ODOO_MODEL_CATALOG_REFRESHED",
            detail=f"{len(catalog.models)} persistent model(s)",
            actor=actor,
        )

    def save_odoo_schema_catalog(
        self,
        project_id: str,
        catalog: OdooSchemaCatalog,
        *,
        actor: Actor,
    ) -> None:
        event_type = (
            "LOCAL_SCHEMA_DRAFT_CREATED"
            if catalog.origin.value == "LOCAL_MANUAL"
            else "ODOO_SCHEMA_CAPTURED"
        )
        source = (
            "unverified local draft"
            if catalog.origin.value == "LOCAL_MANUAL"
            else "authenticated Odoo capture"
        )
        self._save_singleton(
            project_id,
            table="odoo_schema_catalog",
            value_column="catalog_json",
            value=catalog.to_json(),
            event_type=event_type,
            detail=f"{len(catalog.models)} permitted model(s); {source}",
            actor=actor,
            invalidate=(
                "mapping_draft",
                "mapping_current",
                "schema_governance_current",
            ),
        )

    def get_schema_governance(
        self,
        project_id: str,
    ) -> SchemaGovernance | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT revision.governance_json
              FROM schema_governance_current AS current
              JOIN schema_governance_revision AS revision
                ON revision.governance_id = current.governance_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        return SchemaGovernance.from_json(value) if value else None

    def save_schema_governance(
        self,
        project_id: str,
        governance: SchemaGovernance,
        *,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            schema_row = connection.execute(
                """
                SELECT catalog_json
                  FROM odoo_schema_catalog
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if schema_row is None:
                raise WorkspaceError("Capture the Odoo schema first")
            schema = OdooSchemaCatalog.from_json(str(schema_row[0]))
            if (
                governance.project_id != project_id
                or governance.catalog_hash != schema.content_hash
                or tuple(sorted(governance.permitted_models))
                != tuple(sorted(model.name for model in schema.models))
            ):
                raise WorkspaceError(
                    "Schema governance does not match the captured schema"
                )
            current = connection.execute(
                """
                SELECT governance_id, version
                  FROM schema_governance_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            expected_version = int(current[1]) + 1 if current else 1
            expected_id = str(current[0]) if current else governance.governance_id
            if (
                governance.version != expected_version
                or governance.governance_id != expected_id
            ):
                raise WorkspaceError(
                    "Schema governance was modified by another request"
                )
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO schema_governance_revision
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        governance.governance_id,
                        governance.version,
                        governance.catalog_hash,
                        governance.content_hash,
                        governance.to_json(),
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO schema_governance_current
                    VALUES (1, ?, ?)
                    """,
                    [governance.governance_id, governance.version],
                )
                connection.execute("DELETE FROM mapping_current")
                connection.execute("DELETE FROM mapping_draft")
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="SCHEMA_GOVERNANCE_CONFIRMED",
                    detail=(
                        f"version {governance.version}: "
                        f"{len(governance.business_keys)} business key(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_mapping_draft(self, project_id: str) -> MappingDraft | None:
        value = self._read_singleton_json(
            project_id,
            "SELECT draft_json FROM mapping_draft WHERE singleton_id = 1",
        )
        return MappingDraft.from_json(value) if value else None

    def save_mapping_draft(
        self,
        project_id: str,
        draft: MappingDraft,
        *,
        actor: Actor,
    ) -> None:
        self._save_singleton(
            project_id,
            table="mapping_draft",
            value_column="draft_json",
            value=draft.to_json(),
            event_type=f"MAPPING_{draft.status.value}",
            detail=f"version {draft.version}: {len(draft.entries)} mapping(s)",
            actor=actor,
        )

    def get_mapping_revision(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingRevision | None:
        if version is None:
            query = """
                SELECT revision.revision_json
                  FROM mapping_current AS current
                  JOIN mapping_revision AS revision
                    ON revision.mapping_id = current.mapping_id
                   AND revision.version = current.version
                 WHERE current.singleton_id = 1
            """
            value = self._read_singleton_json(project_id, query)
        else:
            values = self._read_json_rows(
                project_id,
                """
                SELECT revision_json
                  FROM mapping_revision
                 WHERE version = ?
                 ORDER BY mapping_id
                """,
                [version],
            )
            value = values[0] if values else None
        return MappingRevision.from_json(value) if value else None

    def list_mapping_revisions(
        self,
        project_id: str,
    ) -> tuple[MappingRevision, ...]:
        return tuple(
            MappingRevision.from_json(value)
            for value in self._read_json_rows(
                project_id,
                """
                SELECT revision_json
                  FROM mapping_revision
                 ORDER BY version, mapping_id
                """,
            )
        )

    def save_mapping_revision(
        self,
        project_id: str,
        revision: MappingRevision,
        *,
        validation: MappingValidationResult,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        if revision.definition.mapping_id != revision.mapping_id:
            raise WorkspaceError(
                "Mapping revision and definition IDs do not match"
            )
        if validation.mapping_content_hash != revision.definition.content_hash:
            raise WorkspaceError(
                "Mapping validation does not match its revision"
            )
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            current = connection.execute(
                """
                SELECT mapping_id, version
                  FROM mapping_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            current_version = int(current[1]) if current else None
            current_id = str(current[0]) if current else revision.mapping_id
            maximum = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM mapping_revision"
            ).fetchone()
            next_version = int(maximum[0]) + 1
            if (
                current_version != expected_parent_version
                or revision.parent_version != expected_parent_version
                or revision.mapping_id != current_id
                or revision.version != next_version
            ):
                raise WorkspaceError(
                    "The mapping was modified by another request"
                )
            revision_number = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO mapping_revision
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        revision.mapping_id,
                        revision.version,
                        revision.parent_version,
                        revision.definition.content_hash,
                        revision.definition.source_selection_hash,
                        revision.definition.schema_hash,
                        revision.created_at.isoformat(),
                        revision.to_json(),
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO mapping_current
                    VALUES (1, ?, ?)
                    """,
                    [revision.mapping_id, revision.version],
                )
                connection.execute(
                    """
                    INSERT INTO mapping_validation
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        revision.mapping_id,
                        revision.version,
                        validation.validator_version,
                        validation.validation_hash,
                        revision.created_at.isoformat(),
                        validation.to_json(),
                    ],
                )
                connection.execute("DELETE FROM mapping_draft")
                self._insert_workspace_audit(
                    connection,
                    revision=revision_number,
                    event_type="MAPPING_REVISION_SAVED",
                    detail=(
                        f"version {revision.version}: "
                        f"{len(revision.definition.datasets)} dataset(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_mapping_validation(
        self,
        project_id: str,
        version: int,
    ) -> MappingValidationResult | None:
        values = self._read_json_rows(
            project_id,
            """
            SELECT validation_json
              FROM mapping_validation
             WHERE version = ?
             ORDER BY created_at DESC, validation_hash
            """,
            [version],
        )
        return (
            MappingValidationResult.from_json(values[0]) if values else None
        )

    def save_mapping_validation(
        self,
        project_id: str,
        version: int,
        validation: MappingValidationResult,
        *,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute(
                """
                SELECT mapping_id, content_hash
                  FROM mapping_revision
                 WHERE version = ?
                 ORDER BY mapping_id
                 LIMIT 1
                """,
                [version],
            ).fetchone()
            if row is None or str(row[1]) != validation.mapping_content_hash:
                raise WorkspaceError(
                    "Mapping validation does not match its revision"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO mapping_validation
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    str(row[0]),
                    version,
                    validation.validator_version,
                    validation.validation_hash,
                    datetime.now(timezone.utc).isoformat(),
                    validation.to_json(),
                ],
            )

    def get_mapping_submission(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingSubmission | None:
        condition = "WHERE version = ?" if version is not None else ""
        parameters: list[object] = [version] if version is not None else []
        values = self._read_json_rows(
            project_id,
            f"""
            SELECT submission_json
              FROM mapping_submission
              {condition}
             ORDER BY submitted_at DESC, submission_id
            """,
            parameters,
        )
        return MappingSubmission.from_json(values[0]) if values else None

    def save_mapping_submission(
        self,
        project_id: str,
        submission: MappingSubmission,
        *,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute(
                """
                SELECT revision.content_hash, validation.validation_hash,
                       validation.validation_json
                  FROM mapping_revision AS revision
                  JOIN mapping_validation AS validation
                    ON validation.mapping_id = revision.mapping_id
                   AND validation.version = revision.version
                 WHERE revision.mapping_id = ?
                   AND revision.version = ?
                   AND validation.validation_hash = ?
                """,
                [
                    submission.mapping_id,
                    submission.version,
                    submission.validation_hash,
                ],
            ).fetchone()
            if (
                row is None
                or str(row[0]) != submission.mapping_content_hash
                or str(row[1]) != submission.validation_hash
            ):
                raise WorkspaceError(
                    "Mapping submission does not match validated evidence"
                )
            validation = MappingValidationResult.from_json(str(row[2]))
            warning_fingerprints = tuple(
                sorted(
                    mapping_issue_fingerprint(item)
                    for item in validation.issues
                    if item.severity == "warning"
                )
            )
            if (
                validation.status is MappingValidationStatus.INVALID
                or submission.warning_acknowledgements
                != warning_fingerprints
            ):
                raise WorkspaceError(
                    "Mapping submission has not passed its validation gate"
                )
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO mapping_submission
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        submission.submission_id,
                        submission.mapping_id,
                        submission.version,
                        submission.mapping_content_hash,
                        submission.validation_hash,
                        submission.submitted_at.isoformat(),
                        submission.to_json(),
                    ],
                )
                connection.execute(
                    "UPDATE project SET mapping_version = ?",
                    [str(submission.version)],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="MAPPING_SUBMITTED",
                    detail=(
                        f"version {submission.version}: "
                        f"{submission.mapping_content_hash}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._update_project(connection, project)
                self._insert_audit(
                    connection,
                    project,
                    event_type=event_type,
                    detail=event_detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def add_source_file(
        self,
        project: MigrationProject,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Insert one immutable source row without rewriting existing evidence."""

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._update_project(connection, project)
                connection.execute(
                    """
                    INSERT INTO source_file VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        source_file.file_id,
                        source_file.display_name,
                        source_file.stored_name,
                        source_file.size_bytes,
                        source_file.sha256,
                        source_file.received_at.isoformat(),
                    ],
                )
                self._insert_audit(
                    connection,
                    project,
                    event_type="SOURCE_FILE_ADDED",
                    detail=source_file.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._update_registry(project)

    def update_schema_scope(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Replace Stage C's model allowlist and invalidate its dependents."""

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._update_project(connection, project)
                connection.execute("DELETE FROM odoo_schema_catalog")
                connection.execute("DELETE FROM schema_governance_current")
                connection.execute("DELETE FROM mapping_draft")
                connection.execute("DELETE FROM mapping_current")
                self._insert_audit(
                    connection,
                    project,
                    event_type="SCHEMA_SCOPE_UPDATED",
                    detail=(
                        f"{len(project.intended_models)} permitted model(s); "
                        "captured schema and active mapping invalidated"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._update_registry(project)
        self._write_registration_manifest(project)

    def project_directory(self, project_id: str) -> Path:
        try:
            canonical = str(UUID(project_id))
        except (ValueError, AttributeError) as error:
            raise ProjectNotFoundError("Invalid project identifier") from error
        target = (self.root / canonical).resolve()
        if target.parent != self.root:
            raise ProjectNotFoundError("Invalid project identifier")
        return target

    def _update_registry(self, project: MigrationProject) -> None:
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                UPDATE project_registry
                   SET name = ?, status = ?, revision = ?, updated_at = ?
                 WHERE project_id = ?
                """,
                [
                    project.name,
                    project.status.value,
                    project.revision,
                    project.updated_at.isoformat(),
                    project.project_id,
                ],
            )

    def _read_json_rows(
        self,
        project_id: str,
        query: str,
        parameters: list[object] | None = None,
    ) -> tuple[str, ...]:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            rows = connection.execute(query, parameters or []).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _read_singleton_json(
        self,
        project_id: str,
        query: str,
    ) -> str | None:
        values = self._read_json_rows(project_id, query)
        return values[0] if values else None

    def _save_singleton(
        self,
        project_id: str,
        *,
        table: str,
        value_column: str,
        value: str,
        event_type: str,
        detail: str,
        actor: Actor,
        invalidate: tuple[str, ...] = (),
    ) -> None:
        permitted = {
            ("source_selection", "selection_json"),
            ("odoo_model_catalog", "catalog_json"),
            ("odoo_schema_catalog", "catalog_json"),
            ("mapping_draft", "draft_json"),
        }
        if (table, value_column) not in permitted:
            raise ValueError("Unsupported workspace table")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO {table} (singleton_id, {value_column})
                    VALUES (1, ?)
                    """,
                    [value],
                )
                for target in invalidate:
                    if target not in {
                        "mapping_draft",
                        "mapping_current",
                        "schema_governance_current",
                    }:
                        raise ValueError("Unsupported invalidation table")
                    connection.execute(f"DELETE FROM {target}")
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type=event_type,
                    detail=detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _project_revision(connection: duckdb.DuckDBPyConnection) -> int:
        row = connection.execute("SELECT revision FROM project").fetchone()
        if row is None:
            raise ProjectNotFoundError("Project not found")
        return int(row[0])

    @staticmethod
    def _insert_workspace_audit(
        connection: duckdb.DuckDBPyConnection,
        *,
        revision: int,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_event (
                event_id, event_type, project_revision, occurred_at, detail,
                actor_issuer, actor_subject, actor_display_name
            )
            VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_type,
                revision,
                datetime.now(timezone.utc).isoformat(),
                detail,
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
            ],
        )

    @contextmanager
    def _connect(self, path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = duckdb.connect(
            str(path),
            config={
                "allow_community_extensions": "false",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "enable_external_access": "false",
                "lock_configuration": "true",
                "memory_limit": "256MB",
                "threads": "2",
            },
        )
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES ({SCHEMA_VERSION});

            CREATE TABLE project (
                project_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                source_system VARCHAR NOT NULL,
                export_status VARCHAR NOT NULL,
                export_date VARCHAR,
                description VARCHAR NOT NULL,
                data_manager VARCHAR NOT NULL,
                functional_owner VARCHAR NOT NULL,
                business_unit VARCHAR NOT NULL,
                data_classification VARCHAR NOT NULL,
                retention_days INTEGER NOT NULL,
                support_access BOOLEAN NOT NULL,
                odoo_connection_mode VARCHAR,
                target_environment VARCHAR,
                odoo_base_url VARCHAR NOT NULL,
                odoo_database VARCHAR NOT NULL,
                intended_applications VARCHAR NOT NULL,
                intended_models VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                registered_at VARCHAR,
                mapping_version VARCHAR,
                current_run_id VARCHAR,
                approval_status VARCHAR NOT NULL
            );

            CREATE TABLE source_file (
                file_id VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                stored_name VARCHAR NOT NULL,
                size_bytes BIGINT NOT NULL,
                sha256 VARCHAR NOT NULL,
                received_at VARCHAR NOT NULL
            );

            CREATE TABLE source_catalog (
                file_id VARCHAR PRIMARY KEY,
                source_sha256 VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                inspected_at VARCHAR NOT NULL,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE source_configuration (
                file_id VARCHAR PRIMARY KEY,
                source_sha256 VARCHAR NOT NULL,
                catalog_hash VARCHAR NOT NULL,
                configuration_json VARCHAR NOT NULL
            );

            CREATE TABLE source_selection (
                singleton_id INTEGER PRIMARY KEY,
                selection_json VARCHAR NOT NULL
            );

            CREATE TABLE odoo_schema_catalog (
                singleton_id INTEGER PRIMARY KEY,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE odoo_model_catalog (
                singleton_id INTEGER PRIMARY KEY,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE mapping_draft (
                singleton_id INTEGER PRIMARY KEY,
                draft_json VARCHAR NOT NULL
            );

            CREATE TABLE schema_governance_revision (
                governance_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                catalog_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                governance_json VARCHAR NOT NULL,
                PRIMARY KEY (governance_id, version)
            );

            CREATE TABLE schema_governance_current (
                singleton_id INTEGER PRIMARY KEY,
                governance_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE mapping_revision (
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                parent_version INTEGER,
                content_hash VARCHAR NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                revision_json VARCHAR NOT NULL,
                PRIMARY KEY (mapping_id, version)
            );

            CREATE TABLE mapping_current (
                singleton_id INTEGER PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE mapping_validation (
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                validator_version VARCHAR NOT NULL,
                validation_hash VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL,
                validation_json VARCHAR NOT NULL,
                PRIMARY KEY (mapping_id, version, validation_hash)
            );

            CREATE TABLE mapping_submission (
                submission_id VARCHAR PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                content_hash VARCHAR NOT NULL,
                validation_hash VARCHAR NOT NULL,
                submitted_at VARCHAR NOT NULL,
                submission_json VARCHAR NOT NULL
            );

            CREATE TABLE audit_event (
                event_id BIGINT PRIMARY KEY,
                event_type VARCHAR NOT NULL,
                project_revision INTEGER NOT NULL,
                occurred_at VARCHAR NOT NULL,
                detail VARCHAR NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL
            );

            CREATE SEQUENCE audit_event_sequence START 1;
            """
        )

    def _insert_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
    ) -> None:
        connection.execute(
            f"INSERT INTO project VALUES ({', '.join('?' for _ in range(26))})",
            _project_values(project),
        )

    def _update_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
    ) -> None:
        connection.execute(
            """
            UPDATE project SET
                name = ?,
                source_system = ?,
                export_status = ?,
                export_date = ?,
                description = ?,
                data_manager = ?,
                functional_owner = ?,
                business_unit = ?,
                data_classification = ?,
                retention_days = ?,
                support_access = ?,
                odoo_connection_mode = ?,
                target_environment = ?,
                odoo_base_url = ?,
                odoo_database = ?,
                intended_applications = ?,
                intended_models = ?,
                status = ?,
                revision = ?,
                created_at = ?,
                updated_at = ?,
                registered_at = ?,
                mapping_version = ?,
                current_run_id = ?,
                approval_status = ?
            WHERE project_id = ?
            """,
            _project_values(project)[1:] + [project.project_id],
        )

    def _migrate_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        row = connection.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            raise RuntimeError("Project database has no schema version")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "Project database was created by a newer Impodo version"
            )
        if version < SCHEMA_VERSION:
            connection.begin()
            try:
                if version == 1:
                    connection.execute(
                        """
                        ALTER TABLE project
                        ADD COLUMN odoo_connection_mode VARCHAR DEFAULT 'REMOTE'
                        """
                    )
                    version = 2
                if version == 2:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_catalog (
                            file_id VARCHAR PRIMARY KEY,
                            source_sha256 VARCHAR NOT NULL,
                            contract_version INTEGER NOT NULL,
                            inspected_at VARCHAR NOT NULL,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 3
                if version == 3:
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_issuer
                        VARCHAR DEFAULT 'urn:impodo:legacy'
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_subject
                        VARCHAR DEFAULT 'unknown'
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_display_name
                        VARCHAR DEFAULT 'Legacy operator'
                        """
                    )
                    version = 4
                if version == 4:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_configuration (
                            file_id VARCHAR PRIMARY KEY,
                            source_sha256 VARCHAR NOT NULL,
                            catalog_hash VARCHAR NOT NULL,
                            configuration_json VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_selection (
                            singleton_id INTEGER PRIMARY KEY,
                            selection_json VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS odoo_schema_catalog (
                            singleton_id INTEGER PRIMARY KEY,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_draft (
                            singleton_id INTEGER PRIMARY KEY,
                            draft_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 5
                if version == 5:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_governance_revision (
                            governance_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            catalog_hash VARCHAR NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            governance_json VARCHAR NOT NULL,
                            PRIMARY KEY (governance_id, version)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS schema_governance_current (
                            singleton_id INTEGER PRIMARY KEY,
                            governance_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_revision (
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            parent_version INTEGER,
                            content_hash VARCHAR NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            schema_hash VARCHAR NOT NULL,
                            created_at VARCHAR NOT NULL,
                            revision_json VARCHAR NOT NULL,
                            PRIMARY KEY (mapping_id, version)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_current (
                            singleton_id INTEGER PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_validation (
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            validator_version VARCHAR NOT NULL,
                            validation_hash VARCHAR NOT NULL,
                            created_at VARCHAR NOT NULL,
                            validation_json VARCHAR NOT NULL,
                            PRIMARY KEY (mapping_id, version, validation_hash)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_submission (
                            submission_id VARCHAR PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            validation_hash VARCHAR NOT NULL,
                            submitted_at VARCHAR NOT NULL,
                            submission_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 6
                if version == 6:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS odoo_model_catalog (
                            singleton_id INTEGER PRIMARY KEY,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 7
                connection.execute(
                    "UPDATE schema_version SET version = ?",
                    [version],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _insert_audit(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
        *,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_event (
                event_id, event_type, project_revision, occurred_at, detail,
                actor_issuer, actor_subject, actor_display_name
            )
            VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_type,
                project.revision,
                project.updated_at.isoformat(),
                detail,
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
            ],
        )

    def _write_registration_manifest(self, project: MigrationProject) -> Path:
        payload = {
            "contract_version": 2,
            "project": {
                "project_id": project.project_id,
                "name": project.name,
                "source_system": project.source_system,
                "export_status": project.export_status.value,
                "export_date": (
                    project.export_date.isoformat() if project.export_date else None
                ),
                "data_manager": project.data_manager,
                "functional_owner": project.functional_owner,
                "business_unit": project.business_unit,
                "data_classification": project.data_classification.value,
                "retention_days": project.retention_days,
                "support_access": project.support_access,
                "odoo_connection_mode": (
                    project.odoo_connection_mode.value
                    if project.odoo_connection_mode
                    else None
                ),
                "target_environment": (
                    project.target_environment.value
                    if project.target_environment
                    else None
                ),
                "odoo_base_url": project.odoo_base_url,
                "odoo_database": project.odoo_database,
                "intended_applications": list(project.intended_applications),
                "intended_models": list(project.intended_models),
                "status": project.status.value,
                "revision": project.revision,
                "created_at": project.created_at.isoformat(),
                "registered_at": (
                    project.registered_at.isoformat()
                    if project.registered_at
                    else None
                ),
                "mapping_version": project.mapping_version,
                "current_run_id": project.current_run_id,
                "approval_status": project.approval_status.value,
            },
            "source_files": [
                {
                    "file_id": source_file.file_id,
                    "display_name": source_file.display_name,
                    "stored_name": source_file.stored_name,
                    "size_bytes": source_file.size_bytes,
                    "sha256": source_file.sha256,
                    "received_at": source_file.received_at.isoformat(),
                }
                for source_file in project.source_files
            ],
        }
        audit_dir = self.project_directory(project.project_id) / "audit"
        target = audit_dir / (
            f"project-registration-r{project.revision}.json"
        )
        partial = target.with_suffix(".json.partial")
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        partial.write_bytes(encoded)
        partial.replace(target)
        return target


def _project_values(project: MigrationProject) -> list[object]:
    return [
        project.project_id,
        project.name,
        project.source_system,
        project.export_status.value,
        project.export_date.isoformat() if project.export_date else None,
        project.description,
        project.data_manager,
        project.functional_owner,
        project.business_unit,
        project.data_classification.value,
        project.retention_days,
        project.support_access,
        (
            project.odoo_connection_mode.value
            if project.odoo_connection_mode
            else None
        ),
        project.target_environment.value if project.target_environment else None,
        project.odoo_base_url,
        project.odoo_database,
        json.dumps(project.intended_applications),
        json.dumps(project.intended_models),
        project.status.value,
        project.revision,
        project.created_at.isoformat(),
        project.updated_at.isoformat(),
        project.registered_at.isoformat() if project.registered_at else None,
        project.mapping_version,
        project.current_run_id,
        project.approval_status.value,
    ]


def _project_from_rows(
    data: dict[str, object],
    source_rows: list[tuple[object, ...]],
) -> MigrationProject:
    export_date = str(data["export_date"]) if data["export_date"] else None
    registered_at = (
        str(data["registered_at"]) if data["registered_at"] else None
    )
    target_environment = (
        TargetEnvironment(str(data["target_environment"]))
        if data["target_environment"]
        else None
    )
    connection_mode = (
        OdooConnectionMode(str(data["odoo_connection_mode"]))
        if data.get("odoo_connection_mode")
        else None
    )
    return MigrationProject(
        project_id=str(data["project_id"]),
        name=str(data["name"]),
        source_system=str(data["source_system"]),
        export_status=ExportStatus(str(data["export_status"])),
        export_date=date.fromisoformat(export_date) if export_date else None,
        description=str(data["description"]),
        data_manager=str(data["data_manager"]),
        functional_owner=str(data["functional_owner"]),
        business_unit=str(data["business_unit"]),
        data_classification=DataClassification(
            str(data["data_classification"])
        ),
        retention_days=int(data["retention_days"]),
        support_access=bool(data["support_access"]),
        odoo_connection_mode=connection_mode,
        target_environment=target_environment,
        odoo_base_url=str(data["odoo_base_url"]),
        odoo_database=str(data["odoo_database"]),
        intended_applications=tuple(json.loads(str(data["intended_applications"]))),
        intended_models=tuple(json.loads(str(data["intended_models"]))),
        source_files=tuple(
            SourceFile(
                file_id=str(row[0]),
                display_name=str(row[1]),
                stored_name=str(row[2]),
                size_bytes=int(row[3]),
                sha256=str(row[4]),
                received_at=datetime.fromisoformat(str(row[5])),
            )
            for row in source_rows
        ),
        status=ProjectStatus(str(data["status"])),
        revision=int(data["revision"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        registered_at=(
            datetime.fromisoformat(registered_at) if registered_at else None
        ),
        mapping_version=(
            str(data["mapping_version"]) if data["mapping_version"] else None
        ),
        current_run_id=(
            str(data["current_run_id"]) if data["current_run_id"] else None
        ),
        approval_status=ApprovalStatus(str(data["approval_status"])),
    )
