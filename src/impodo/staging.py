"""Durable publication boundary for target-independent canonical staging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .access import Actor
from .staging_contracts import (
    CanonicalStagingRun,
    StagingDatasetReconciliation,
    StagingReconciliation,
)


class StagingRunStatus(StrEnum):
    """Lifecycle metadata kept outside immutable canonical evidence."""

    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class StagingRunSummary:
    """Small, UI-safe projection of one published canonical run."""

    run_id: str
    project_id: str
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

    @property
    def total_rows(self) -> int:
        return self.reconciliation.total_rows

    @property
    def attention_rows(self) -> int:
        return (
            self.reconciliation.blocked_rows
            + self.reconciliation.quarantined_rows
        )


class CanonicalStagingRepository(Protocol):
    """Storage seam shared by browser evaluation and future ETL adapters."""

    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary: ...

    def get_current_staging_summary(
        self,
        project_id: str,
    ) -> StagingRunSummary | None: ...

    def get_canonical_staging_run(
        self,
        project_id: str,
        run_id: str,
    ) -> CanonicalStagingRun | None: ...
