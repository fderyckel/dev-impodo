"""Bounded application read model for shared workflow navigation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from impodo.application.workspace.execution.navigation import ExecutionNavigationState
from impodo.application.workspace.execution.service import (
    ExecutionNavigationPreview,
    ExecutionService,
)
from impodo.domain.workspace.workbench import WorkspaceState


@dataclass(frozen=True, slots=True)
class WorkspaceNavigationFacts:
    """Scalar and identifier-only evidence needed by the navigation presenter."""

    workspace_id: str
    source_selection_hash: str = ""
    source_configuration_count: int = 0
    selected_source_configuration_count: int = 0
    derived_rules_present: bool = False
    odoo_model_catalog_present: bool = False
    schema_present: bool = False
    schema_attention: bool = False
    schema_content_hash: str = ""
    schema_models: tuple[str, ...] = ()
    capture_models: tuple[str, ...] = ()
    governance_present: bool = False
    mapping_complete: bool = False
    staging_run_id: str = ""
    resolution_status: str = ""
    resolution_staging_run_id: str = ""
    quality_run_id: str = ""
    quality_staging_run_id: str = ""
    normalization_run_id: str = ""
    normalization_staging_run_id: str = ""
    normalization_quality_run_id: str = ""
    normalization_status: str = ""
    normalization_decision_count: int = 0
    normalization_reviewed_count: int = 0
    preflight_status: str = ""
    execution_state: ExecutionNavigationState | None = None
    transfer_execution_run_id: str = ""
    transfer_reconciliation_status: str = ""
    active_preparation_job_id: str = ""
    active_load_job_id: str = ""
    execution_preview: ExecutionNavigationPreview | None = None

    @property
    def source_complete(self) -> bool:
        """Tell navigation whether a frozen source selection exists.

        Called by the navigation presenter and stage-gating templates; it is a
        display-level fact, not proof that source data is fit for preparation.
        """
        return bool(self.source_selection_hash)

    @property
    def schema_complete(self) -> bool:
        """Tell navigation whether both schema capture and governance exist.

        Called by the navigation presenter. It deliberately does not inspect
        mapping completeness, because schema and mapping have separate gates.
        """
        return self.schema_present and self.governance_present

    @property
    def normalization_frozen(self) -> bool:
        """Expose whether the normalization decision set is immutable.

        Called by stage navigation and readiness presentation; the
        normalization service remains responsible for state transitions.
        """
        return self.normalization_status == "FROZEN"

    @property
    def normalization_decisions_left(self) -> int:
        """Return the non-negative remaining manager-review count.

        Called by navigation presentation to show progress. The count is
        derived from persisted facts and never changes workflow state.
        """
        return max(
            0,
            self.normalization_decision_count - self.normalization_reviewed_count,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceNavigationSnapshot:
    """Workspace state and navigation facts read under one storage snapshot."""

    workspace_state: WorkspaceState
    facts: WorkspaceNavigationFacts


class WorkspaceNavigationRepository(Protocol):
    """Read compact navigation facts without performing workflow decisions."""

    def get(self, workspace_id: str) -> WorkspaceNavigationFacts:
        """Read facts for ``WorkspaceNavigationQueryService.get``.

        This compatibility read need not share a transaction with the
        workbench state; page composition should prefer ``get_snapshot``.
        """
        ...

    def get_snapshot(self, workspace_id: str) -> WorkspaceNavigationSnapshot:
        """Read state and facts consistently for ``get_for_workspace``."""
        ...


class ActiveJobReader(Protocol):
    def active(self, workspace_id: str):
        """Return the in-flight job used to enrich navigation, if any."""
        ...


class WorkspaceNavigationQueryService:
    """Combine one durable read with bounded in-memory job state."""

    def __init__(
        self,
        repository: WorkspaceNavigationRepository,
        execution: ExecutionService,
        *,
        preparation_jobs: ActiveJobReader | None = None,
        load_jobs: ActiveJobReader | None = None,
    ) -> None:
        self.repository = repository
        self.execution = execution
        self.preparation_jobs = preparation_jobs
        self.load_jobs = load_jobs

    def get(self, workspace_state: WorkspaceState) -> WorkspaceNavigationFacts:
        """Enrich already-read workbench state with live job and load facts.

        Compatibility callers that already hold ``WorkspaceState`` use this
        method. New page routes call ``get_for_workspace`` so the durable state
        and facts come from one repository snapshot.
        """
        facts = self.repository.get(workspace_state.workspace_id)
        return self._with_runtime_facts(facts, workspace_state)

    def get_for_workspace(self, workspace_id: str) -> WorkspaceNavigationSnapshot:
        """Read Overview state and navigation facts through one connection."""

        snapshot = self.repository.get_snapshot(workspace_id)
        return replace(
            snapshot,
            facts=self._with_runtime_facts(
                snapshot.facts,
                snapshot.workspace_state,
            ),
        )

    def _with_runtime_facts(
        self,
        facts: WorkspaceNavigationFacts,
        workspace_state: WorkspaceState,
    ) -> WorkspaceNavigationFacts:
        preparation_job = (
            self.preparation_jobs.active(workspace_state.workspace_id)
            if self.preparation_jobs is not None
            else None
        )
        load_job = (
            self.load_jobs.active(workspace_state.workspace_id)
            if self.load_jobs is not None
            else None
        )
        preview = (
            self.execution.navigation_preview(
                facts.execution_state,
                workspace_state=workspace_state,
            )
            if facts.execution_state is not None
            else None
        )
        return replace(
            facts,
            active_preparation_job_id=(
                str(preparation_job.job_id) if preparation_job is not None else ""
            ),
            active_load_job_id=(
                str(load_job.job_id) if load_job is not None else ""
            ),
            execution_preview=preview,
        )
