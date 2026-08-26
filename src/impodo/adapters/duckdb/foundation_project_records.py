"""Persist MigrationProject roots through the shared registry coordinator."""

from __future__ import annotations

from datetime import datetime

from ...access import Actor
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    require_uuid,
)
from ...migration_projects import (
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
