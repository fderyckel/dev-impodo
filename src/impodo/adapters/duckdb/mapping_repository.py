"""DuckDB mapping repository implementation."""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)


from ...access import Actor
from ...derived_entities import DerivedEntityPlan, mapping_source_selection
from ...inspection import SourceFileCatalog
from ...domain.schema.governance import SchemaGovernance
from ...domain.mapping.artifacts import (
    MappingRevision,
    MappingSubmission,
)
from ...domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
    mapping_issue_fingerprint,
)
from ...projects import ProjectNotFoundError
from ...workspace_contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SourceSelection,
)
from ...workspace_errors import WorkspaceError







class MappingRepositoryMixin:
    """Persistence operations for mapping repository."""

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
