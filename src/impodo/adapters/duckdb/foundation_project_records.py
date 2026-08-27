"""Persist MigrationProject roots through the shared registry coordinator."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from impodo.domain.project.foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    require_uuid,
)
from impodo.domain.shared.access import Actor

from ...domain.project.models import (
    MigrationProject,
    MigrationProjectStatus,
    MigrationProjectSummary,
)


class FoundationProjectRecords:
    """Own Project identity, summary, and optimistic-revision persistence."""

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
        with self._registry_transactions.transaction() as connection:
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
        with self._registry_transactions.transaction() as connection:
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
        return self.get_project(project.project_id)

    def delete_project(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> MigrationProject:
        """Delete one Project root after staging every contained local directory."""

        project_id = require_uuid(project_id, "project_id")
        expected_revision = require_revision(expected_revision)
        with self.database.connect(self.registry_path) as connection:
            project = self.get_project(project_id)
            if project.optimistic_revision != expected_revision:
                raise MigrationConflictError(
                    "The project changed in another request; reload before deleting"
                )
            data_version_ids = self._project_owner_ids(
                connection,
                "data_version",
                "data_version_id",
                project_id,
            )
            workspace_ids = self._project_owner_ids(
                connection,
                "migration_workspace",
                "workspace_id",
                project_id,
            )
            recipe_ids = self._project_owner_ids(
                connection,
                "recipe",
                "recipe_id",
                project_id,
            )

        staged = self._stage_project_directories(
            project_id,
            data_version_ids=data_version_ids,
            workspace_ids=workspace_ids,
            recipe_ids=recipe_ids,
        )
        registry_deleted = False
        try:
            with self._registry_transactions.transaction() as connection:
                current = connection.execute(
                    """
                    SELECT optimistic_revision
                      FROM migration_project
                     WHERE project_id = ?
                    """,
                    [project_id],
                ).fetchone()
                if current is None:
                    raise MigrationConflictError("Project no longer exists")
                if int(current[0]) != expected_revision:
                    raise MigrationConflictError(
                        "The project changed in another request; reload before deleting"
                    )
                self._delete_project_registry_rows(connection, project_id)
            registry_deleted = True
        except Exception:
            self._restore_staged_directories(staged)
            raise

        if registry_deleted:
            for _original, temporary in staged:
                if temporary.is_dir():
                    shutil.rmtree(temporary, ignore_errors=True)
                elif temporary.exists():
                    temporary.unlink()
        return project

    @staticmethod
    def _project_owner_ids(
        connection,
        table: str,
        id_column: str,
        project_id: str,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            f"SELECT {id_column} FROM {table} WHERE project_id = ?",
            [project_id],
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _stage_project_directories(
        self,
        project_id: str,
        *,
        data_version_ids: tuple[str, ...],
        workspace_ids: tuple[str, ...],
        recipe_ids: tuple[str, ...],
    ) -> tuple[tuple[Path, Path], ...]:
        root = self.database.root
        candidates = [self.database.project_directory(project_id)]
        candidates.extend(root / "artifacts" / "dv" / item for item in data_version_ids)
        candidates.extend(root / "artifacts" / "ws" / item for item in workspace_ids)
        candidates.extend(root / ".recipes-protected" / item for item in recipe_ids)
        candidates.append(root / ".project-evidence-protected" / project_id)
        staged: list[tuple[Path, Path]] = []
        try:
            for candidate in candidates:
                original = candidate.resolve()
                parent = candidate.parent.resolve()
                if original.parent != parent:
                    raise MigrationConflictError(
                        "Project storage contains an unsafe deletion path"
                    )
                if candidate.is_symlink():
                    raise MigrationConflictError(
                        "Project storage contains an unsafe deletion path"
                    )
                if not candidate.exists():
                    continue
                if not candidate.is_dir():
                    raise MigrationConflictError(
                        "Project storage contains an unsafe deletion path"
                    )
                temporary = parent / f".{candidate.name}.deleting-{uuid4()}"
                candidate.rename(temporary)
                staged.append((candidate, temporary))
        except Exception:
            self._restore_staged_directories(tuple(staged))
            raise
        return tuple(staged)

    @staticmethod
    def _restore_staged_directories(
        staged: tuple[tuple[Path, Path], ...],
    ) -> None:
        for original, temporary in reversed(staged):
            if temporary.exists() and not original.exists():
                temporary.rename(original)

    @staticmethod
    def _delete_project_registry_rows(connection, project_id: str) -> None:
        """Delete registry dependants in foreign-key order."""

        project_tables = (
            "production_run_binding",
            "test_run_parameter_values",
            "test_run_setup_binding",
            "project_cutover_selection",
            "cutover_plan_qualification",
            "recipe_qualification",
        )
        for table in project_tables:
            connection.execute(
                f"DELETE FROM {table} WHERE project_id = ?",
                [project_id],
            )

        application_ids = (
            "SELECT application_id FROM recipe_application WHERE project_id = ?"
        )
        for table in (
            "recipe_application_reference_requirement",
            "recipe_application_requirement",
            "recipe_application_issue",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE application_id IN ({application_ids})",
                [project_id],
            )
        connection.execute(
            "DELETE FROM recipe_application WHERE project_id = ?",
            [project_id],
        )

        run_ids = "SELECT migration_run_id FROM migration_run WHERE project_id = ?"
        for table in (
            "migration_run_reference_bundle",
            "migration_run_target_schema",
            "migration_run_requirement_plan",
            "migration_run_cutover_plan",
            "migration_run_target_setup",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE migration_run_id IN ({run_ids})",
                [project_id],
            )
        connection.execute(
            "DELETE FROM target_binding WHERE project_id = ?",
            [project_id],
        )

        plan_ids = "SELECT cutover_plan_id FROM cutover_plan WHERE project_id = ?"
        for table in (
            "cutover_write_ownership",
            "cutover_dependency",
            "cutover_plan_recipe",
            "cutover_plan_revision",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE cutover_plan_id IN ({plan_ids})",
                [project_id],
            )
        connection.execute(
            "DELETE FROM cutover_plan WHERE project_id = ?",
            [project_id],
        )

        recipe_ids = "SELECT recipe_id FROM recipe WHERE project_id = ?"
        connection.execute(
            f"DELETE FROM recipe_revision WHERE recipe_id IN ({recipe_ids})",
            [project_id],
        )
        connection.execute("DELETE FROM recipe WHERE project_id = ?", [project_id])

        connection.execute(
            "DELETE FROM migration_workspace WHERE project_id = ?",
            [project_id],
        )
        connection.execute("DELETE FROM migration_run WHERE project_id = ?", [project_id])
        connection.execute(
            """
            UPDATE data_version
               SET parent_data_version_id = NULL
             WHERE project_id = ?
            """,
            [project_id],
        )
        connection.execute("DELETE FROM data_version WHERE project_id = ?", [project_id])
        connection.execute("DELETE FROM migration_event WHERE project_id = ?", [project_id])
        connection.execute(
            "DELETE FROM project_operation_intent WHERE project_id = ?",
            [project_id],
        )
        connection.execute("DELETE FROM migration_project WHERE project_id = ?", [project_id])
        # Identity reservations remain as non-user-visible tombstones. DuckDB
        # cannot delete a referenced parent key in the same transaction even
        # after its child rows were deleted, and retaining the UUID prevents a
        # deleted evidence identity from ever being reused.
