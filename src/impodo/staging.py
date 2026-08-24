"""Durable publication boundary for target-independent canonical staging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .access import Actor
from .staging_contracts import (
    CanonicalControlTotal,
    CanonicalStagingRun,
    StagingDatasetReconciliation,
    StagingReconciliation,
)
from .domain.staging.preparation_session import StoredCanonicalStagingRun


class StagingRunStatus(StrEnum):
    """Lifecycle metadata kept outside immutable canonical evidence."""

    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class StagingRunSummary:
    """Small, UI-safe projection of one published canonical run.

    It points to the full :class:`CanonicalStagingRun` while carrying enough
    reconciliation counts for status pages and downstream readiness checks.
    """

    run_id: str
    workspace_id: str
    content_hash: str
    mapping_id: str
    mapping_version: int
    contract_version: int
    evaluator_version: int
    status: StagingRunStatus
    published_at: datetime
    published_by: str
    reconciliation: StagingReconciliation
    datasets: tuple[StagingDatasetReconciliation, ...]
    control_totals: tuple[CanonicalControlTotal, ...] = ()

    @property
    def total_rows(self) -> int:
        """Return all canonical rows represented by this run."""

        return self.reconciliation.total_rows

    @property
    def attention_rows(self) -> int:
        """Count rows blocked or quarantined during canonical preparation."""

        return (
            self.reconciliation.blocked_rows
            + self.reconciliation.quarantined_rows
        )

    @property
    def failed_control_total_count(self) -> int:
        """Count declared source/prepared totals that do not reconcile."""

        return sum(not item.passed for item in self.control_totals)

    @property
    def control_totals_passed(self) -> bool:
        """Whether every control total attached to this run passed."""

        return self.failed_control_total_count == 0


class CanonicalStagingRepository(Protocol):
    """Storage seam shared by browser evaluation and future ETL adapters.

    Stage-E application code publishes portable evidence through this port;
    the storage adapter owns run IDs, lifecycle status, and hash verification.
    """

    def publish_canonical_staging(
        self,
        workspace_id: str,
        run: CanonicalStagingRun | StoredCanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary:
        """Publish immutable canonical evidence and return its summary."""
        ...

    def get_current_staging_summary(
        self,
        workspace_id: str,
    ) -> StagingRunSummary | None:
        """Return the project's current published Stage-E summary."""
        ...

    def get_canonical_staging_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        expected_content_hash: str | None = None,
    ) -> CanonicalStagingRun | None:
        """Reload a run, optionally rejecting content-hash drift."""
        ...
