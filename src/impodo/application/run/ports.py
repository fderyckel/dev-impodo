"""Persistence boundary owned by the run application service."""

from __future__ import annotations

from typing import Protocol

from ...access import Actor
from ...domain.run.models import MigrationRun
from ...migration_foundation import FaultInjector


class MigrationRunRepository(Protocol):
    def next_run_number(self, project_id: str) -> int: ...

    def create_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationRun: ...

    def get_migration_run(self, migration_run_id: str) -> MigrationRun: ...

    def list_migration_runs(self, project_id: str) -> tuple[MigrationRun, ...]: ...

    def save_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationRun: ...
