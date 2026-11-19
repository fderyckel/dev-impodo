"""Enrich only the selected project result with local journal totals and its company."""

from dataclasses import replace

from impodo.application.workspace.navigation import WorkspaceNavigationSnapshot
from impodo.domain.reconciliation import ReconciliationRunStatus
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.workbench import SourceMode
from impodo.web.context import WebContext


def project_outcome_snapshot(
    context: WebContext, workspace: MigrationWorkspace, snapshot: WorkspaceNavigationSnapshot,
    *, actor: Actor,
) -> WorkspaceNavigationSnapshot:
    """Keep stage navigation and ongoing jobs independent of result-specific reads."""

    facts = snapshot.facts
    if facts.active_load_job_id or not actor.has(Capability.PROTECTED_EVIDENCE_READ):
        return snapshot
    execution = facts.execution_state
    transfer = snapshot.workspace_state.source_mode is SourceMode.ODOO
    if transfer:
        run_id = facts.transfer_execution_run_id
    elif execution:
        run_id = execution.execution_run_id
    else:
        return snapshot
    if not run_id:
        return snapshot
    target = context.migration_run_target_setup.get(workspace.migration_run_id, actor=actor)
    outcome = context.navigation.repository.get_project_load_outcome(
        workspace.workspace_id, run_id,
        company_binding_hash=target.company_binding_hash if target else "",
        read_context_hash=execution.summary.read_context_hash if execution and not transfer else "",
        include_counts=transfer or (execution is not None and execution.reconciliation_status != ReconciliationRunStatus.VERIFIED),
    )
    return replace(snapshot, facts=replace(facts, project_load_outcome=outcome))
