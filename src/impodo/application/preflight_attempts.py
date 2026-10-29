"""Value-free contracts for restart-safe Final review comparisons.

The application contract deliberately carries only owner identities, evidence
hashes, coarse progress, and safe terminal information.  Local composition
persists it in the DuckDB registry; a future server adapter may persist the
same meaning in PostgreSQL without moving detailed comparison evidence out of
the Workspace data plane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from impodo.domain.project.foundation import (
    MigrationOperationIntent,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
)
from impodo.domain.shared.access import Actor


class PreflightPublicationCommittedError(RuntimeError):
    """Report a failed registration-file refresh after evidence committed."""


@dataclass(frozen=True, slots=True)
class PreflightComparisonRequest:
    """Bind one browser submission to its current bounded evidence meaning."""

    operation_id: str
    project_id: str
    workspace_id: str
    semantic_input_hash: str
    workspace_revision: int
    data_version_id: str
    migration_run_id: str
    recipe_application_id: str | None
    requested_at: datetime
    read_credential_binding_hash: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.project_id, "project_id"),
            (self.workspace_id, "workspace_id"),
            (self.data_version_id, "data_version_id"),
            (self.migration_run_id, "migration_run_id"),
        ):
            require_uuid(value, name)
        if self.recipe_application_id is not None:
            require_uuid(self.recipe_application_id, "recipe_application_id")
        require_hash(self.semantic_input_hash, "semantic_input_hash")
        if self.read_credential_binding_hash:
            require_hash(
                self.read_credential_binding_hash, "read_credential_binding_hash"
            )
        require_revision(self.workspace_revision, "workspace_revision")
        require_aware(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class PreflightComparisonReceipt:
    """Identify one verified, committed comparison without customer values."""

    operation_id: str
    workspace_id: str
    preflight_run_id: str
    frozen_input_hash: str
    requirement_plan_hash: str
    result_hash: str
    manifest_hash: str
    execution_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.workspace_id, "workspace_id"),
            (self.preflight_run_id, "preflight_run_id"),
        ):
            require_uuid(value, name)
        for value, name in (
            (self.frozen_input_hash, "frozen_input_hash"),
            (self.requirement_plan_hash, "requirement_plan_hash"),
            (self.result_hash, "result_hash"),
            (self.manifest_hash, "manifest_hash"),
        ):
            require_hash(value, name)
        if self.execution_snapshot_hash:
            require_hash(
                self.execution_snapshot_hash,
                "execution_snapshot_hash",
            )

    def to_mapping(self) -> dict[str, object]:
        """Return the bounded registry result payload."""

        return {
            "execution_snapshot_hash": self.execution_snapshot_hash,
            "frozen_input_hash": self.frozen_input_hash,
            "manifest_hash": self.manifest_hash,
            "operation_id": self.operation_id,
            "preflight_run_id": self.preflight_run_id,
            "requirement_plan_hash": self.requirement_plan_hash,
            "result_hash": self.result_hash,
            "workspace_id": self.workspace_id,
        }


class PreflightAttemptStore(Protocol):
    """Persist bounded local attempt state outside detailed Workspace evidence."""

    def reserve_preflight_comparison(
        self,
        request: PreflightComparisonRequest,
        *,
        actor: Actor,
    ) -> MigrationOperationIntent: ...

    def get_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
    ) -> MigrationOperationIntent: ...

    def list_pending_preflight_comparisons(
        self,
    ) -> tuple[MigrationOperationIntent, ...]: ...

    def advance_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
        phase: str,
    ) -> MigrationOperationIntent: ...

    def commit_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
        *,
        result: Mapping[str, object],
    ) -> MigrationOperationIntent: ...

    def fail_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
        *,
        failure_code: str,
        stage: str = "FAILED",
    ) -> MigrationOperationIntent: ...


__all__ = [
    "PreflightAttemptStore",
    "PreflightComparisonReceipt",
    "PreflightComparisonRequest",
    "PreflightPublicationCommittedError",
]
