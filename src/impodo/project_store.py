"""Hardened DuckDB persistence for local migration projects."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Iterable, Iterator, Sequence
from uuid import UUID, uuid4

import duckdb

from .access import Actor
from .derived_entities import DerivedEntityPlan, mapping_source_selection
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
    ProjectError,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectSummary,
    SourceFile,
)
from .readiness import ReadinessReport
from .staging import StagingRunStatus, StagingRunSummary
from .staging_contracts import CanonicalRow, CanonicalStagingRun
from .workspace import (
    MappingDraft,
    MappingWorkingDraft,
    OdooModelCatalog,
    OdooSchemaCatalog,
    SourceConfiguration,
    SourceSelection,
    WorkspaceError,
)


SCHEMA_VERSION = 12
STAGING_ROW_BATCH_SIZE = 1_000


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

    def delete(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """Permanently remove one contained project and its registry row."""

        project_dir = self.project_directory(project_id)
        canonical_project_id = project_dir.name
        database_path = project_dir / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")

        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            current = connection.execute(
                "SELECT revision FROM project"
            ).fetchone()
        if current is None:
            raise ProjectNotFoundError("Project not found")
        if int(current[0]) != expected_revision:
            raise ProjectConflictError(
                "The project was modified by another request"
            )

        staged = self.root / f".{canonical_project_id}.deleting-{uuid4()}"
        if staged.parent != self.root or staged.exists():
            raise ProjectError("Could not prepare the project for deletion")

        project_dir.rename(staged)
        registry_deleted = False
        try:
            with self._connect(self.registry_path) as connection:
                registered = connection.execute(
                    """
                    SELECT revision
                      FROM project_registry
                     WHERE project_id = ?
                    """,
                    [canonical_project_id],
                ).fetchone()
                if registered is None:
                    raise ProjectNotFoundError("Project not found")
                if int(registered[0]) != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                connection.execute(
                    "DELETE FROM project_registry WHERE project_id = ?",
                    [canonical_project_id],
                )
                registry_deleted = True
            shutil.rmtree(staged)
        except Exception:
            if not registry_deleted and staged.exists() and not project_dir.exists():
                staged.rename(project_dir)
            raise

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
                connection.execute("DELETE FROM derived_entity_plan_current")
                connection.execute("DELETE FROM mapping_draft")
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
            self.get_derived_entity_plan(project_id),
            self.get_source_catalogs(project_id),
        )

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
            invalidate=(
                "derived_entity_plan_current",
                "mapping_draft",
                "mapping_current",
            ),
        )

    def get_derived_entity_plan(
        self,
        project_id: str,
    ) -> DerivedEntityPlan | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT revision.plan_json
              FROM derived_entity_plan_current AS current
              JOIN derived_entity_plan_revision AS revision
                ON revision.plan_id = current.plan_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        return DerivedEntityPlan.from_json(value) if value else None

    def save_derived_entity_plan(
        self,
        project_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            selection_row = connection.execute(
                "SELECT selection_json FROM source_selection WHERE singleton_id = 1"
            ).fetchone()
            if selection_row is None:
                raise WorkspaceError(
                    "Freeze source datasets before deriving entities"
                )
            selection = SourceSelection.from_json(str(selection_row[0]))
            if (
                plan.project_id != project_id
                or plan.source_selection_hash != selection.content_hash
            ):
                raise WorkspaceError(
                    "Derived-entity plan does not match the frozen source selection"
                )
            current = connection.execute(
                """
                SELECT plan_id, version
                  FROM derived_entity_plan_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            actual_parent = int(current[1]) if current else None
            expected_plan_id = str(current[0]) if current else plan.plan_id
            if (
                actual_parent != expected_parent_version
                or plan.version != (actual_parent or 0) + 1
                or plan.plan_id != expected_plan_id
            ):
                raise WorkspaceError(
                    "The derived-entity plan was modified by another request"
                )
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO derived_entity_plan_revision
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        plan.plan_id,
                        plan.version,
                        plan.source_selection_hash,
                        plan.content_hash,
                        plan.updated_at.isoformat(),
                        plan.updated_by,
                        plan.to_json(),
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO derived_entity_plan_current
                    VALUES (1, ?, ?)
                    """,
                    [plan.plan_id, plan.version],
                )
                connection.execute("DELETE FROM mapping_current")
                connection.execute("DELETE FROM mapping_draft")
                self._invalidate_canonical_staging(
                    connection,
                    reason="DERIVED_ENTITY_PLAN_CHANGED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="DERIVED_ENTITY_PLAN_SAVED",
                    detail=f"version {plan.version}: {len(plan.rules)} rule(s)",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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
                self._invalidate_canonical_staging(
                    connection,
                    reason="SCHEMA_GOVERNANCE_CHANGED",
                )
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

    def get_mapping_working_draft(
        self,
        project_id: str,
    ) -> MappingWorkingDraft | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT draft_json
              FROM mapping_working_draft
             WHERE singleton_id = 1
            """,
        )
        return MappingWorkingDraft.from_json(value) if value else None

    def save_mapping_working_draft(
        self,
        project_id: str,
        draft: MappingWorkingDraft,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        if draft.project_id != project_id:
            raise WorkspaceError("Working draft belongs to another project")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            selection_row = connection.execute(
                """
                SELECT selection_json
                  FROM source_selection
                 WHERE singleton_id = 1
                """
            ).fetchone()
            schema_row = connection.execute(
                """
                SELECT catalog_json
                  FROM odoo_schema_catalog
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if selection_row is None or schema_row is None:
                raise WorkspaceError(
                    "Freeze datasets and capture Odoo schema first"
                )
            selection = SourceSelection.from_json(str(selection_row[0]))
            plan_row = connection.execute(
                """
                SELECT revision.plan_json
                  FROM derived_entity_plan_current AS current
                  JOIN derived_entity_plan_revision AS revision
                    ON revision.plan_id = current.plan_id
                   AND revision.version = current.version
                 WHERE current.singleton_id = 1
                """
            ).fetchone()
            plan = (
                DerivedEntityPlan.from_json(str(plan_row[0]))
                if plan_row is not None
                else None
            )
            catalog_rows = connection.execute(
                "SELECT catalog_json FROM source_catalog ORDER BY file_id"
            ).fetchall()
            mapping_selection = mapping_source_selection(
                selection,
                plan,
                tuple(
                    SourceFileCatalog.from_json(str(row[0]))
                    for row in catalog_rows
                ),
            )
            schema = OdooSchemaCatalog.from_json(str(schema_row[0]))
            governance_row = connection.execute(
                """
                SELECT revision.governance_json
                  FROM schema_governance_current AS current
                  JOIN schema_governance_revision AS revision
                    ON revision.governance_id = current.governance_id
                   AND revision.version = current.version
                 WHERE current.singleton_id = 1
                """
            ).fetchone()
            governance = (
                SchemaGovernance.from_json(str(governance_row[0]))
                if governance_row is not None
                else None
            )
            expected_schema_hash = (
                governance.content_hash
                if governance is not None
                else schema.content_hash
            )
            if (
                draft.definition.source_selection_hash
                != mapping_selection.content_hash
                or draft.definition.schema_hash != expected_schema_hash
            ):
                raise WorkspaceError(
                    "Working draft does not match the current mapping evidence"
                )
            existing = connection.execute(
                """
                SELECT mapping_id, version
                  FROM mapping_working_draft
                 WHERE singleton_id = 1
                """
            ).fetchone()
            actual_version = int(existing[1]) if existing else None
            expected_mapping_id = (
                str(existing[0]) if existing else draft.mapping_id
            )
            current_mapping = connection.execute(
                """
                SELECT mapping_id, version
                  FROM mapping_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            actual_base_version = (
                int(current_mapping[1]) if current_mapping else None
            )
            if current_mapping is not None:
                expected_mapping_id = str(current_mapping[0])
            if (
                actual_version != expected_version
                or draft.version != (actual_version or 0) + 1
                or draft.mapping_id != expected_mapping_id
                or draft.base_mapping_version != actual_base_version
            ):
                raise WorkspaceError(
                    "The working draft was modified by another request"
                )
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO mapping_working_draft
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        1,
                        draft.mapping_id,
                        draft.version,
                        draft.definition.source_selection_hash,
                        draft.definition.schema_hash,
                        draft.content_hash,
                        draft.updated_at.isoformat(),
                        draft.to_json(),
                    ],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="MAPPING_WORKING_DRAFT_SAVED",
                    detail=(
                        f"version {draft.version}: "
                        f"{len(draft.definition.datasets)} dataset(s); "
                        "semantic validation not run"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

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
                self._invalidate_canonical_staging(
                    connection,
                    reason="MAPPING_REVISION_CHANGED",
                )
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
                    """
                    SELECT revision, odoo_connection_mode, odoo_base_url,
                           odoo_database, intended_applications,
                           intended_models
                      FROM project
                    """
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                target_changed = event_type == "PROJECT_TARGET_UPDATED" and (
                    str(current[1] or "")
                    != (
                        project.odoo_connection_mode.value
                        if project.odoo_connection_mode
                        else ""
                    )
                    or str(current[2]) != project.odoo_base_url
                    or str(current[3]) != project.odoo_database
                    or tuple(json.loads(str(current[4])))
                    != project.intended_applications
                    or tuple(json.loads(str(current[5])))
                    != project.intended_models
                )
                self._update_project(connection, project)
                if target_changed:
                    connection.execute("DELETE FROM odoo_schema_catalog")
                    connection.execute("DELETE FROM schema_governance_current")
                    connection.execute("DELETE FROM mapping_draft")
                    connection.execute("DELETE FROM mapping_current")
                    self._invalidate_canonical_staging(
                        connection,
                        reason="PROJECT_TARGET_CHANGED",
                    )
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
                self._invalidate_canonical_staging(
                    connection,
                    reason="SOURCE_FILE_ADDED",
                )
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
                self._invalidate_canonical_staging(
                    connection,
                    reason="SCHEMA_SCOPE_CHANGED",
                )
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

    def get_readiness_report(
        self,
        project_id: str,
        mapping_id: str,
        mapping_version: int,
        mapping_content_hash: str,
        staging_run_id: str,
        staging_content_hash: str,
    ) -> ReadinessReport | None:
        values = self._read_json_rows(
            project_id,
            """
            SELECT report_json
              FROM readiness_run
             WHERE mapping_id = ?
               AND mapping_version = ?
               AND mapping_content_hash = ?
               AND staging_run_id = ?
               AND staging_content_hash = ?
             ORDER BY checked_at DESC, run_id
            """,
            [
                mapping_id,
                mapping_version,
                mapping_content_hash,
                staging_run_id,
                staging_content_hash,
            ],
        )
        return ReadinessReport.from_json(values[0]) if values else None

    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary:
        """Atomically publish immutable canonical rows for one submitted mapping."""

        if run.project_id != project_id:
            raise WorkspaceError("Prepared data belongs to another project")
        try:
            CanonicalStagingRun.from_json(run.to_json())
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Prepared data evidence is invalid") from error
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        published_at = datetime.now(timezone.utc)
        run_id = str(uuid4())
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                mapping = connection.execute(
                    """
                    SELECT revision.mapping_id, revision.version,
                           revision.content_hash,
                           revision.source_selection_hash,
                           revision.schema_hash
                      FROM mapping_current AS current
                      JOIN mapping_revision AS revision
                        ON revision.mapping_id = current.mapping_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                       AND EXISTS (
                           SELECT 1
                             FROM mapping_submission AS submission
                            WHERE submission.mapping_id = revision.mapping_id
                              AND submission.version = revision.version
                              AND submission.content_hash = revision.content_hash
                       )
                    """
                ).fetchone()
                if mapping is None:
                    raise WorkspaceError(
                        "Submit the current field matches before saving prepared data"
                    )
                if (
                    str(mapping[0]) != run.mapping_id
                    or int(mapping[1]) != mapping_version
                    or str(mapping[2]) != run.mapping_hash
                    or str(mapping[3]) != run.source_selection_hash
                    or str(mapping[4]) != run.schema_hash
                ):
                    raise WorkspaceError(
                        "Prepared data no longer matches the submitted field matches"
                    )
                selection = connection.execute(
                    """
                    SELECT selection_json
                      FROM source_selection
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                if selection is None:
                    raise WorkspaceError(
                        "Freeze the source datasets before saving prepared data"
                    )
                physical = SourceSelection.from_json(str(selection[0]))
                if physical.content_hash != run.physical_selection_hash:
                    raise WorkspaceError(
                        "Prepared data no longer matches the frozen source datasets"
                    )
                plan = connection.execute(
                    """
                    SELECT revision.content_hash
                      FROM derived_entity_plan_current AS current
                      JOIN derived_entity_plan_revision AS revision
                        ON revision.plan_id = current.plan_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                current_plan_hash = str(plan[0]) if plan else None
                if current_plan_hash != run.derived_plan_hash:
                    raise WorkspaceError(
                        "Prepared data no longer matches its related-record plan"
                    )

                current = connection.execute(
                    """
                    SELECT run.run_id, run.content_hash, run.mapping_id,
                           run.mapping_version, run.contract_version,
                           run.evaluator_version, run.status, run.published_at,
                           run.published_by, run.reconciliation_json,
                           run.dataset_reconciliation_json
                      FROM canonical_staging_current AS active
                      JOIN canonical_staging_run AS run
                        ON run.run_id = active.run_id
                     WHERE active.singleton_id = 1
                    """
                ).fetchone()
                if (
                    current is not None
                    and str(current[1]) == run.content_hash
                    and str(current[2]) == run.mapping_id
                    and int(current[3]) == mapping_version
                ):
                    connection.rollback()
                    return self._staging_summary(project_id, current)

                connection.execute(
                    """
                    INSERT INTO canonical_staging_run (
                        run_id, content_hash, mapping_id, mapping_version,
                        physical_selection_hash, source_selection_hash,
                        mapping_hash, schema_hash, derived_plan_hash,
                        contract_version, evaluator_version, status,
                        published_at, published_by, row_count,
                        run_issues_json, reconciliation_json,
                        dataset_reconciliation_json, retired_at,
                        retired_reason, successor_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              NULL, NULL, NULL)
                    """,
                    [
                        run_id,
                        run.content_hash,
                        run.mapping_id,
                        mapping_version,
                        run.physical_selection_hash,
                        run.source_selection_hash,
                        run.mapping_hash,
                        run.schema_hash,
                        run.derived_plan_hash,
                        run.contract_version,
                        run.evaluator_version,
                        StagingRunStatus.PUBLISHED.value,
                        published_at.isoformat(),
                        actor.identity.display_name,
                        len(run.rows),
                        _canonical_json(
                            [item.to_portable_dict() for item in run.issues]
                        ),
                        _canonical_json(run.reconciliation.to_portable_dict()),
                        _canonical_json(
                            [item.to_portable_dict() for item in run.datasets]
                        ),
                    ],
                )
                self._insert_canonical_rows(connection, run_id, run.rows)
                stored_count = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM canonical_staging_row
                     WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchone()
                if stored_count is None or int(stored_count[0]) != len(run.rows):
                    raise WorkspaceError("Prepared rows were not stored completely")
                if current is not None:
                    connection.execute(
                        """
                        UPDATE canonical_staging_run
                           SET status = ?, retired_at = ?, retired_reason = ?,
                               successor_run_id = ?
                         WHERE run_id = ?
                        """,
                        [
                            StagingRunStatus.SUPERSEDED.value,
                            published_at.isoformat(),
                            "NEW_CANONICAL_RUN",
                            run_id,
                            str(current[0]),
                        ],
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO canonical_staging_current
                    VALUES (1, ?)
                    """,
                    [run_id],
                )
                connection.execute(
                    """
                    UPDATE project
                       SET current_run_id = NULL,
                           approval_status = 'INVALIDATED'
                    """
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type="CANONICAL_STAGING_PUBLISHED",
                    detail=(
                        f"run {run_id}: {len(run.rows)} prepared row(s); "
                        f"{run.content_hash}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return StagingRunSummary(
            run_id=run_id,
            project_id=project_id,
            content_hash=run.content_hash,
            mapping_id=run.mapping_id,
            mapping_version=mapping_version,
            contract_version=run.contract_version,
            evaluator_version=run.evaluator_version,
            status=StagingRunStatus.PUBLISHED,
            published_at=published_at,
            published_by=actor.identity.display_name,
            reconciliation=run.reconciliation,
            datasets=run.datasets,
        )

    def get_current_staging_summary(
        self,
        project_id: str,
    ) -> StagingRunSummary | None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute(
                """
                SELECT run.run_id, run.content_hash, run.mapping_id,
                       run.mapping_version, run.contract_version,
                       run.evaluator_version, run.status, run.published_at,
                       run.published_by, run.reconciliation_json,
                       run.dataset_reconciliation_json
                  FROM canonical_staging_current AS active
                  JOIN canonical_staging_run AS run
                    ON run.run_id = active.run_id
                 WHERE active.singleton_id = 1
                   AND run.status = 'PUBLISHED'
                """
            ).fetchone()
        return self._staging_summary(project_id, row) if row else None

    def get_canonical_staging_run(
        self,
        project_id: str,
        run_id: str,
    ) -> CanonicalStagingRun | None:
        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Prepared-data run identifier is invalid") from error
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            header = connection.execute(
                """
                SELECT content_hash, mapping_id, physical_selection_hash,
                       source_selection_hash, mapping_hash, schema_hash,
                       derived_plan_hash, contract_version, evaluator_version,
                       run_issues_json, reconciliation_json,
                       dataset_reconciliation_json
                  FROM canonical_staging_run
                 WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            if header is None:
                return None
            rows = connection.execute(
                """
                SELECT row_json
                  FROM canonical_staging_row
                 WHERE run_id = ?
                 ORDER BY ordinal
                """,
                [canonical_run_id],
            ).fetchall()
        payload = {
            "content_hash": str(header[0]),
            "mapping_id": str(header[1]),
            "project_id": project_id,
            "physical_selection_hash": str(header[2]),
            "source_selection_hash": str(header[3]),
            "mapping_hash": str(header[4]),
            "schema_hash": str(header[5]),
            "derived_plan_hash": str(header[6]) if header[6] else None,
            "contract_version": int(header[7]),
            "evaluator_version": int(header[8]),
            "issues": json.loads(str(header[9])),
            "reconciliation": json.loads(str(header[10])),
            "datasets": json.loads(str(header[11])),
            "rows": [json.loads(str(item[0])) for item in rows],
        }
        try:
            return CanonicalStagingRun.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared-data evidence is invalid") from error

    @staticmethod
    def _insert_canonical_rows(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        rows: Sequence[CanonicalRow],
    ) -> None:
        for start in range(0, len(rows), STAGING_ROW_BATCH_SIZE):
            batch = rows[start : start + STAGING_ROW_BATCH_SIZE]
            connection.executemany(
                """
                INSERT INTO canonical_staging_row (
                    run_id, ordinal, row_id, dataset, source_row,
                    target_model, disposition, row_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    [
                        run_id,
                        start + offset,
                        row.row_id,
                        row.dataset,
                        row.source_row,
                        row.target_model,
                        row.disposition.value,
                        _canonical_json(row.to_portable_dict()),
                    ]
                    for offset, row in enumerate(batch)
                ],
            )

    @staticmethod
    def _staging_summary(
        project_id: str,
        row: Sequence[object],
    ) -> StagingRunSummary:
        from .staging_contracts import (
            StagingDatasetReconciliation,
            StagingReconciliation,
        )

        return StagingRunSummary(
            run_id=str(row[0]),
            project_id=project_id,
            content_hash=str(row[1]),
            mapping_id=str(row[2]),
            mapping_version=int(row[3]),
            contract_version=int(row[4]),
            evaluator_version=int(row[5]),
            status=StagingRunStatus(str(row[6])),
            published_at=datetime.fromisoformat(str(row[7])),
            published_by=str(row[8]),
            reconciliation=StagingReconciliation.from_dict(
                json.loads(str(row[9]))
            ),
            datasets=tuple(
                StagingDatasetReconciliation.from_dict(item)
                for item in json.loads(str(row[10]))
            ),
        )

    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        actor: Actor,
    ) -> None:
        try:
            canonical_run_id = str(UUID(report.run_id))
            canonical_staging_run_id = str(UUID(report.staging_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Readiness run identifier is invalid") from error
        if report.project_id != project_id:
            raise WorkspaceError("Readiness report belongs to another project")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                submission = connection.execute(
                    """
                    SELECT submission_id
                      FROM mapping_submission
                     WHERE mapping_id = ?
                       AND version = ?
                       AND content_hash = ?
                     ORDER BY submitted_at DESC
                     LIMIT 1
                    """,
                    [
                        report.mapping_id,
                        report.mapping_version,
                        report.mapping_content_hash,
                    ],
                ).fetchone()
                if submission is None:
                    raise WorkspaceError(
                        "Readiness report does not match a submitted mapping"
                    )
                staging = connection.execute(
                    """
                    SELECT run.content_hash, run.mapping_id,
                           run.mapping_version, run.mapping_hash
                      FROM canonical_staging_current AS current
                      JOIN canonical_staging_run AS run
                        ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.run_id = ?
                       AND run.status = 'PUBLISHED'
                    """,
                    [canonical_staging_run_id],
                ).fetchone()
                if (
                    staging is None
                    or str(staging[0]) != report.staging_content_hash
                    or str(staging[1]) != report.mapping_id
                    or int(staging[2]) != report.mapping_version
                    or str(staging[3]) != report.mapping_content_hash
                ):
                    raise WorkspaceError(
                        "Readiness report does not match the current prepared data"
                    )
                revision = self._project_revision(connection)
                connection.execute(
                    """
                    INSERT INTO readiness_run (
                        run_id, mapping_id, mapping_version,
                        mapping_content_hash, target_hash, staging_run_id,
                        staging_content_hash, checked_at, checked_by,
                        report_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_run_id,
                        report.mapping_id,
                        report.mapping_version,
                        report.mapping_content_hash,
                        report.target_hash,
                        canonical_staging_run_id,
                        report.staging_content_hash,
                        report.checked_at.isoformat(),
                        report.checked_by,
                        report.to_json(),
                    ],
                )
                connection.execute(
                    """
                    UPDATE project
                       SET current_run_id = ?,
                           approval_status = 'REVIEW_REQUIRED'
                    """,
                    [canonical_run_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="READINESS_CHECK_COMPLETED",
                    detail=(
                        f"run {canonical_run_id}: {report.status}; "
                        f"{report.total_count} row(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        updated_project = self.get(project_id)
        self._update_registry(updated_project)
        self._write_registration_manifest(updated_project)

    def project_directory(self, project_id: str) -> Path:
        try:
            canonical = str(UUID(project_id))
        except (ValueError, AttributeError) as error:
            raise ProjectNotFoundError("Invalid project identifier") from error
        candidate = self.root / canonical
        target = candidate.resolve()
        if target != candidate or target.parent != self.root:
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
                        "derived_entity_plan_current",
                        "mapping_draft",
                        "mapping_current",
                        "schema_governance_current",
                    }:
                        raise ValueError("Unsupported invalidation table")
                    connection.execute(f"DELETE FROM {target}")
                if table in {"source_selection", "odoo_schema_catalog"}:
                    self._invalidate_canonical_staging(
                        connection,
                        reason=(
                            "SOURCE_SELECTION_CHANGED"
                            if table == "source_selection"
                            else "ODOO_SCHEMA_CHANGED"
                        ),
                    )
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
    def _invalidate_canonical_staging(
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current staging pointer without deleting audit evidence."""

        current = connection.execute(
            """
            SELECT run_id
              FROM canonical_staging_current
             WHERE singleton_id = 1
            """
        ).fetchone()
        if current is None:
            return
        connection.execute(
            """
            UPDATE canonical_staging_run
               SET status = ?, retired_at = ?, retired_reason = ?
             WHERE run_id = ?
            """,
            [
                StagingRunStatus.INVALIDATED.value,
                datetime.now(timezone.utc).isoformat(),
                reason,
                str(current[0]),
            ],
        )
        connection.execute("DELETE FROM canonical_staging_current")
        connection.execute(
            """
            UPDATE project
               SET current_run_id = NULL,
                   approval_status = 'INVALIDATED'
            """
        )

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

            CREATE TABLE derived_entity_plan_revision (
                plan_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                updated_by VARCHAR NOT NULL,
                plan_json VARCHAR NOT NULL,
                PRIMARY KEY (plan_id, version)
            );

            CREATE TABLE derived_entity_plan_current (
                singleton_id INTEGER PRIMARY KEY,
                plan_id VARCHAR NOT NULL,
                version INTEGER NOT NULL
            );

            CREATE TABLE odoo_model_catalog (
                singleton_id INTEGER PRIMARY KEY,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE mapping_draft (
                singleton_id INTEGER PRIMARY KEY,
                draft_json VARCHAR NOT NULL
            );

            CREATE TABLE mapping_working_draft (
                singleton_id INTEGER PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                version INTEGER NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                content_hash VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
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

            CREATE TABLE canonical_staging_run (
                run_id VARCHAR PRIMARY KEY,
                content_hash VARCHAR NOT NULL,
                mapping_id VARCHAR NOT NULL,
                mapping_version INTEGER NOT NULL,
                physical_selection_hash VARCHAR NOT NULL,
                source_selection_hash VARCHAR NOT NULL,
                mapping_hash VARCHAR NOT NULL,
                schema_hash VARCHAR NOT NULL,
                derived_plan_hash VARCHAR,
                contract_version INTEGER NOT NULL,
                evaluator_version INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                published_at VARCHAR NOT NULL,
                published_by VARCHAR NOT NULL,
                row_count BIGINT NOT NULL,
                run_issues_json VARCHAR NOT NULL,
                reconciliation_json VARCHAR NOT NULL,
                dataset_reconciliation_json VARCHAR NOT NULL,
                retired_at VARCHAR,
                retired_reason VARCHAR,
                successor_run_id VARCHAR
            );

            CREATE TABLE canonical_staging_row (
                run_id VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL,
                row_id VARCHAR NOT NULL,
                dataset VARCHAR NOT NULL,
                source_row BIGINT NOT NULL,
                target_model VARCHAR NOT NULL,
                disposition VARCHAR NOT NULL,
                row_json VARCHAR NOT NULL,
                PRIMARY KEY (run_id, ordinal),
                UNIQUE (run_id, row_id)
            );

            CREATE INDEX canonical_staging_row_lookup
                ON canonical_staging_row (run_id, dataset, disposition);

            CREATE TABLE canonical_staging_current (
                singleton_id INTEGER PRIMARY KEY,
                run_id VARCHAR NOT NULL
            );

            CREATE TABLE readiness_run (
                run_id VARCHAR PRIMARY KEY,
                mapping_id VARCHAR NOT NULL,
                mapping_version INTEGER NOT NULL,
                mapping_content_hash VARCHAR NOT NULL,
                target_hash VARCHAR NOT NULL,
                staging_run_id VARCHAR NOT NULL,
                staging_content_hash VARCHAR NOT NULL,
                checked_at VARCHAR NOT NULL,
                checked_by VARCHAR NOT NULL,
                report_json VARCHAR NOT NULL
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
            f"INSERT INTO project VALUES ({', '.join('?' for _ in range(25))})",
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
                if version == 7:
                    legacy_target_column = "_".join(("target", "environment"))
                    drop_legacy_column = (
                        "ALTER TABLE project DROP COLUMN IF EXISTS "
                        f'"{legacy_target_column}"'
                    )
                    connection.execute(drop_legacy_column)
                    for table in (
                        "odoo_model_catalog",
                        "odoo_schema_catalog",
                        "schema_governance_current",
                        "schema_governance_revision",
                        "mapping_draft",
                        "mapping_current",
                        "mapping_revision",
                        "mapping_validation",
                        "mapping_submission",
                    ):
                        connection.execute(f"DELETE FROM {table}")
                    connection.execute(
                        """
                        UPDATE project
                           SET mapping_version = NULL,
                               current_run_id = NULL,
                               approval_status = 'INVALIDATED'
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO audit_event (
                            event_id, event_type, project_revision, occurred_at,
                            detail, actor_issuer, actor_subject,
                            actor_display_name
                        )
                        SELECT nextval('audit_event_sequence'),
                               'TARGET_CONTRACT_MIGRATED', revision, updated_at,
                               'Target-derived evidence invalidated after '
                               || 'the target contract changed',
                               'urn:impodo:migration', 'schema-v8',
                               'Impodo schema migration'
                          FROM project
                        """
                    )
                    version = 8
                if version == 8:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS derived_entity_plan_revision (
                            plan_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            updated_at VARCHAR NOT NULL,
                            updated_by VARCHAR NOT NULL,
                            plan_json VARCHAR NOT NULL,
                            PRIMARY KEY (plan_id, version)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS derived_entity_plan_current (
                            singleton_id INTEGER PRIMARY KEY,
                            plan_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL
                        )
                        """
                    )
                    version = 9
                if version == 9:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS readiness_run (
                            run_id VARCHAR PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            mapping_version INTEGER NOT NULL,
                            mapping_content_hash VARCHAR NOT NULL,
                            target_hash VARCHAR NOT NULL,
                            checked_at VARCHAR NOT NULL,
                            checked_by VARCHAR NOT NULL,
                            report_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 10
                if version == 10:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mapping_working_draft (
                            singleton_id INTEGER PRIMARY KEY,
                            mapping_id VARCHAR NOT NULL,
                            version INTEGER NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            schema_hash VARCHAR NOT NULL,
                            content_hash VARCHAR NOT NULL,
                            updated_at VARCHAR NOT NULL,
                            draft_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 11
                if version == 11:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS canonical_staging_run (
                            run_id VARCHAR PRIMARY KEY,
                            content_hash VARCHAR NOT NULL,
                            mapping_id VARCHAR NOT NULL,
                            mapping_version INTEGER NOT NULL,
                            physical_selection_hash VARCHAR NOT NULL,
                            source_selection_hash VARCHAR NOT NULL,
                            mapping_hash VARCHAR NOT NULL,
                            schema_hash VARCHAR NOT NULL,
                            derived_plan_hash VARCHAR,
                            contract_version INTEGER NOT NULL,
                            evaluator_version INTEGER NOT NULL,
                            status VARCHAR NOT NULL,
                            published_at VARCHAR NOT NULL,
                            published_by VARCHAR NOT NULL,
                            row_count BIGINT NOT NULL,
                            run_issues_json VARCHAR NOT NULL,
                            reconciliation_json VARCHAR NOT NULL,
                            dataset_reconciliation_json VARCHAR NOT NULL,
                            retired_at VARCHAR,
                            retired_reason VARCHAR,
                            successor_run_id VARCHAR
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS canonical_staging_row (
                            run_id VARCHAR NOT NULL,
                            ordinal BIGINT NOT NULL,
                            row_id VARCHAR NOT NULL,
                            dataset VARCHAR NOT NULL,
                            source_row BIGINT NOT NULL,
                            target_model VARCHAR NOT NULL,
                            disposition VARCHAR NOT NULL,
                            row_json VARCHAR NOT NULL,
                            PRIMARY KEY (run_id, ordinal),
                            UNIQUE (run_id, row_id)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS canonical_staging_row_lookup
                            ON canonical_staging_row (
                                run_id, dataset, disposition
                            )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS canonical_staging_current (
                            singleton_id INTEGER PRIMARY KEY,
                            run_id VARCHAR NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE readiness_run
                        ADD COLUMN IF NOT EXISTS staging_run_id
                        VARCHAR DEFAULT ''
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE readiness_run
                        ADD COLUMN IF NOT EXISTS staging_content_hash
                        VARCHAR DEFAULT ''
                        """
                    )
                    version = 12
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
            "contract_version": 3,
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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
