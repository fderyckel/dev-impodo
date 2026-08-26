"""Create and resume MigrationRun roots in the shared registry."""

from __future__ import annotations

from ...access import Actor
from ...domain.run.models import (
    MigrationRun,
)
from ...migration_foundation import (
    FaultInjector,
    MigrationOperationKind,
    MigrationOperationState,
)


class FoundationMigrationRunCommands:
    def create_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_workspace_revision: int,
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
            expected_revision=expected_workspace_revision,
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

    def resume_migration_run_creation(
        self,
        operation_id: str,
        *,
        actor: Actor,
    ) -> MigrationRun:
        intent = self._pending_create_intent(
            operation_id,
            MigrationOperationKind.MIGRATION_RUN_CREATE,
            "MIGRATION_RUN",
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_migration_run(intent.owner_id)
        stored = self._run_from_dict(dict(intent.detail["migration_run"]))
        return self.create_migration_run(
            stored,
            expected_workspace_revision=int(intent.expected_revision or 0),
            operation_id=intent.operation_id,
            request_hash=intent.request_hash,
            actor=actor,
        )
