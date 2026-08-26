"""Create workspaces and publish bounded source projections."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ...access import Actor
from ...data_version_sources import (
    SourcePackageState,
    WorkspaceSourceProjection,
)
from ...domain.workspace.models import (
    MigrationWorkspace,
)
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    utc_now,
)


class FoundationWorkspaceCommands:
    def create_migration_workspace(
        self,
        workspace: MigrationWorkspace,
        *,
        expected_workspace_revision: int,
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
            expected_revision=expected_workspace_revision,
            detail={"migration_workspace": self._workspace_dict(workspace)},
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_migration_workspace(intent.owner_id)
        stored = self._workspace_from_dict(dict(intent.detail["migration_workspace"]))
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

    def resume_migration_workspace_creation(
        self,
        operation_id: str,
        *,
        actor: Actor,
    ) -> MigrationWorkspace:
        intent = self._pending_create_intent(
            operation_id,
            MigrationOperationKind.MIGRATION_WORKSPACE_CREATE,
            "MIGRATION_WORKSPACE",
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_migration_workspace(intent.owner_id)
        stored = self._workspace_from_dict(dict(intent.detail["migration_workspace"]))
        return self.create_migration_workspace(
            stored,
            expected_workspace_revision=int(intent.expected_revision or 0),
            operation_id=intent.operation_id,
            request_hash=intent.request_hash,
            actor=actor,
        )

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
                        occurred_at=datetime.fromisoformat(str(stored["created_at"])),
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
