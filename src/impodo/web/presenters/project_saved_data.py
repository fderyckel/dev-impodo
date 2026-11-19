"""Build the project overview's saved-set summary from the local registry."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.application.odoo_saved_data_jobs import SavedOdooJob
from impodo.domain.odoo_saved_data import SavedOdooSetSummary
from impodo.domain.project.foundation import MigrationNotFoundError
from impodo.domain.project.models import MigrationProject, MigrationProjectStatus
from impodo.domain.shared.access import Actor, AuthorizationError, Capability
from impodo.web.context import WebContext

from .project_workspace_action import ProjectOverviewAction

_VISIBLE_SETS = 3


@dataclass(frozen=True, slots=True)
class ProjectSavedDataOverview:
    """Separate existing saved sets from optional capture and import actions."""

    sets: tuple[SavedOdooSetSummary, ...]
    has_more: bool
    package_import_available: bool
    active_job: SavedOdooJob | None
    internal_workspace_ids: frozenset[str]


def project_saved_data_overview(
    context: WebContext, project_id: str, *, workspace_ids: tuple[str, ...] = ()
) -> ProjectSavedDataOverview | None:
    """Read one bounded page after the caller authorizes the parent project."""

    if context.saved_odoo_data is None:
        return None
    summaries = context.saved_odoo_data.repository.list_set_summaries(
        project_id, limit=_VISIBLE_SETS + 1
    )
    return ProjectSavedDataOverview(
        sets=summaries[:_VISIBLE_SETS],
        has_more=len(summaries) > _VISIBLE_SETS,
        package_import_available=context.saved_odoo_archive is not None,
        active_job=(
            context.saved_odoo_jobs.active_for_project(project_id)
            if context.saved_odoo_jobs is not None
            else None
        ),
        internal_workspace_ids=(
            context.saved_odoo_data.repository.internal_capture_workspaces(
                project_id, workspace_ids
            )
            if summaries and workspace_ids
            else frozenset()
        ),
    )


def active_saved_data_action(
    overview: ProjectSavedDataOverview | None, project: MigrationProject
) -> ProjectOverviewAction | None:
    """Decide whether progress owns this page before opening any workflow evidence."""

    if overview is None or overview.active_job is None or project.status in {
        MigrationProjectStatus.CLOSED, MigrationProjectStatus.ARCHIVED,
    }:
        return None
    job = overview.active_job
    base = f"/projects/{project.project_id}/saved-odoo-data"
    return ProjectOverviewAction(
        status="current",
        status_label="Queued" if job.status == "QUEUED" else "In progress",
        title="Capturing Odoo data" if job.kind == "UPDATE" else "Restoring saved Odoo data",
        detail=job.message,
        action_label="View progress",
        href=f"{base}?selected={job.saved_data_id}&job={job.job_id}",
    )


def saved_data_action(
    overview_action: ProjectOverviewAction,
    saved_data_overview: ProjectSavedDataOverview | None,
    project: MigrationProject,
    *,
    context: WebContext,
    actor: Actor,
) -> ProjectOverviewAction:
    """Lead with a current saved-data job or a sole set in an otherwise idle setup."""

    if saved_data_overview is None or project.status in {
        MigrationProjectStatus.CLOSED,
        MigrationProjectStatus.ARCHIVED,
    }:
        return overview_action
    base = f"/projects/{project.project_id}/saved-odoo-data"
    if not saved_data_overview.sets:
        return overview_action
    item, *additional_sets = saved_data_overview.sets
    if not overview_action.saved_data_candidate and (
        additional_sets or item.source_workspace_id != overview_action.eligible_saved_source_id
    ):
        return overview_action
    if additional_sets:
        return ProjectOverviewAction(
            status="current",
            status_label="Saved Odoo data",
            title="Choose a saved set",
            action_label="View saved sets",
            href=base,
        )
    detail = (
        f"Version {item.latest_version} · Captured {item.captured_at:%d %b %Y %H:%M %Z}"
        if item.captured_at
        else "No data captured yet"
    )
    if not item.imported and actor.has(Capability.PROTECTED_EVIDENCE_READ):
        try:
            owner = context.workspace_access.resolve(
                item.source_workspace_id,
                actor=actor,
                capability=Capability.PROTECTED_EVIDENCE_READ,
            )
        except (AuthorizationError, MigrationNotFoundError):
            owner = None
        if (
            owner
            and owner.project_id == project.project_id
            and context.saved_odoo_data is not None
        ):
            labels = context.saved_odoo_data.source_companies.source_labels(
                item.source_workspace_id,
                target_hash=item.source_target_hash,
                company_hash=item.source_company_hash,
            )
            if labels:
                database, company = labels
                detail = f"Source database {database} · Company {company} · {detail}"
    return ProjectOverviewAction(
        status="complete" if item.captured_at else "current",
        status_label="Saved Odoo data" if item.captured_at else "Configuration saved",
        title=item.label,
        detail=detail,
        action_label="Open saved set",
        href=f"{base}?selected={item.saved_data_id}",
        source_mode=overview_action.source_mode,
        featured_saved_set_id=item.saved_data_id,
    )
