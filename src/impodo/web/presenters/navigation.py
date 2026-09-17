"""Build the project workflow navigation from current local evidence.

The presenter keeps page location separate from migration progress.  It uses
only bounded local projections and never contacts Odoo or changes project
state.  Downstream stages are evaluated only after their prerequisites are
current, so a changed upstream choice immediately locks stale later work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ...domain.reconciliation import ReconciliationRunStatus
from impodo.application.preflight_jobs import PreflightJob
from impodo.application.workspace.execution.job_models import LoadJob
from impodo.application.workspace.navigation import WorkspaceNavigationFacts
from impodo.application.workspace.preparation.job_models import PreparationJob
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState, WorkspaceStatus
from impodo.application.workspace.views import WorkspaceOwnerView
from ..workspace_journeys import (
    WorkspaceJourney,
    classify_workspace_journey,
)


@dataclass(frozen=True, slots=True)
class WorkflowPage:
    """One page inside a user-facing migration stage."""

    page_id: str
    label: str
    href: str | None
    status: str = "available"
    status_label: str = "Available"
    current: bool = False
    optional: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowStage:
    """One durable stage shown in the project sidebar and overview."""

    stage_id: str
    number: int
    label: str
    href: str | None
    status: str
    status_label: str
    pages: tuple[WorkflowPage, ...] = ()
    active: bool = False
    pages_always_visible: bool = False

    @property
    def available(self) -> bool:
        return self.href is not None


@dataclass(frozen=True, slots=True)
class WorkflowNavigation:
    """Shared sidebar presentation for a workspace or a whole Recipe run."""

    migration_project_name: str
    registered: bool
    setup_active: bool
    setup_href: str
    overview_href: str | None
    overview_active: bool
    current_stage_id: str
    current_stage_label: str
    viewed_stage_id: str
    viewed_page_label: str
    stages: tuple[WorkflowStage, ...]
    journey: str = WorkspaceJourney.AUTHORING.value
    journey_label: str = "Workspace"
    overview_label: str = "Data version overview"
    current_work_label: str = "Current data-version work"
    odoo_access_href: str | None = None
    odoo_access_active: bool = False

    @property
    def current_stage(self) -> WorkflowStage | None:
        return next(
            (
                stage
                for stage in self.stages
                if stage.stage_id == self.current_stage_id
            ),
            None,
        )

    @property
    def viewed_stage(self) -> WorkflowStage | None:
        return next(
            (
                stage
                for stage in self.stages
                if stage.stage_id == self.viewed_stage_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceNavigation(WorkflowNavigation):
    """Navigation attached to one actual workspace."""

    workspace_id: str


def build_recipe_run_navigation(
    *,
    project_id: str,
    migration_run_id: str,
    migration_project_name: str,
    run_purpose: str,
    complete: bool,
    odoo_needs_attention: bool,
) -> WorkflowNavigation:
    """Render run progress without opening an application's workspace store."""

    run_home = f"/projects/{project_id}/runs/{migration_run_id}"
    run_kind = "test-runs" if run_purpose == "TEST" else "production-runs"
    fresh_home = f"/projects/{project_id}/{run_kind}/{migration_run_id}/fresh-data"
    stages = (
        WorkflowStage("fresh", 1, "Fresh data", fresh_home, "complete", "Complete"),
        WorkflowStage(
            "odoo", 2, "Check Odoo", f"{run_home}/odoo",
            "attention" if odoo_needs_attention else "complete",
            "Needs attention" if odoo_needs_attention else "Complete",
        ),
        WorkflowStage(
            "review", 3, "Review and load", run_home,
            "complete" if complete else "current",
            "Verified" if complete else "Current", active=True,
        ),
    )
    current = stages[1] if odoo_needs_attention else stages[2]
    return WorkflowNavigation(
        migration_project_name=migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=fresh_home,
        overview_href=run_home,
        overview_active=False,
        current_stage_id=current.stage_id,
        current_stage_label=current.label,
        viewed_stage_id="review",
        viewed_page_label="",
        stages=stages,
        journey="RECIPE_RUN",
        journey_label="Recipe run",
        overview_label="Run overview",
        current_work_label="Current run work",
    )


_TEMPLATE_LOCATION = {
    "workspace_overview.html": ("", "Project overview"),
    "workspace_sources.html": ("source", "Check source files"),
    "workspace_odoo_capture_selection.html": (
        "source",
        "Define bounded Odoo capture",
    ),
    "workspace_odoo_capture_progress.html": (
        "source",
        "Freeze Odoo records",
    ),
    "workspace_transfer_destination.html": (
        "destination",
        "Connect destination Odoo",
    ),
    "workspace_destination_matching.html": (
        "destination-match",
        "Match destination data",
    ),
    "workspace_transfer_order.html": (
        "transfer-order",
        "Validate transfer order",
    ),
    "workspace_transfer_review.html": (
        "transfer-review",
        "Review transfer",
    ),
    "workspace_transfer_preflight.html": (
        "destination-load",
        "Read-only destination preflight",
    ),
    "workspace_transfer_load.html": (
        "destination-load",
        "Prepare exact destination load",
    ),
    "workspace_transfer_load_progress.html": (
        "destination-load",
        "Loading destination Odoo",
    ),
    "workspace_datasets.html": ("source", "Saved source tables"),
    "workspace_derived_entities.html": (
        "source",
        "Separate combined information",
    ),
    "workspace_target.html": ("", "Odoo access"),
    "workspace_schema.html": ("odoo", "Choose Odoo records"),
    "project_recipe_run_progress.html": ("odoo", "Odoo check progress"),
    "project_recipe_target_matches.html": ("odoo", "Review target values"),
    "mapping/page.html": ("match", "Match fields"),
    "workspace_transformation_impact.html": (
        "match",
        "Review rule effects",
    ),
    "workspace_prepare.html": ("prepare", "Start preparation"),
    "workspace_preparation_progress.html": (
        "prepare",
        "Preparation progress",
    ),
    "workspace_resolution.html": ("prepare", "Review possible duplicates"),
    "workspace_normalization.html": ("prepare", "Approve prepared data"),
    "workspace_summary.html": ("review", "Final review"),
    "workspace_load.html": ("load", "Load into Odoo"),
    "workspace_load_progress.html": ("load", "Loading into Odoo"),
}


def build_workspace_navigation(
    facts: WorkspaceNavigationFacts,
    workspace_state: WorkspaceState,
    template_name: str,
    *,
    current_path: str = "",
    migration_project_name: str | None = None,
    workspace_view: WorkspaceOwnerView | None = None,
    run_setup_complete: bool | None = None,
    fresh_data_complete: bool | None = None,
) -> WorkspaceNavigation:
    """Return the one user journey allowed by canonical workspace ownership."""

    navigation = _build_authoring_workspace_navigation(
        facts,
        workspace_state,
        template_name,
        current_path=current_path,
        migration_project_name=migration_project_name,
    )
    if workspace_view is None:
        return navigation
    journey = classify_workspace_journey(
        workspace_view.migration_run.purpose,
        workspace_view.migration_workspace.recipe_application_id,
    )
    if journey is WorkspaceJourney.AUTHORING:
        return navigation
    if journey is WorkspaceJourney.RECIPE_RUN_SETUP:
        return _recipe_run_setup_navigation(
            navigation,
            workspace_view,
            template_name=template_name,
            run_setup_complete=run_setup_complete,
            fresh_data_complete=fresh_data_complete,
        )
    return _recipe_application_navigation(
        navigation,
        project_id=workspace_view.project_id,
        migration_run_id=workspace_view.migration_run_id,
        run_purpose=workspace_view.migration_run.purpose.value,
        template_name=template_name,
    )


def _build_authoring_workspace_navigation(
    facts: WorkspaceNavigationFacts,
    workspace_state: WorkspaceState,
    template_name: str,
    *,
    current_path: str = "",
    migration_project_name: str | None = None,
) -> WorkspaceNavigation:
    """Build the source-mode-specific Authoring evidence projection."""

    current_workspace_state = workspace_state
    navigation_name = migration_project_name or current_workspace_state.name
    viewed_stage_id, viewed_page_label = _TEMPLATE_LOCATION.get(
        template_name,
        ("", "Project setup"),
    )
    if template_name == "workspace_load.html":
        if current_path.endswith("/load/confirm"):
            viewed_page_label = "Confirm and load"
        elif current_path.endswith("/load/outcome"):
            viewed_page_label = "Verify result"
        else:
            viewed_page_label = "Check changes"
    if template_name == "workspace_transfer_load.html":
        if current_path.endswith("/transfer-load/confirm"):
            viewed_page_label = "Confirm and load"
        elif current_path.endswith("/transfer-load/outcome"):
            viewed_page_label = "Verify destination result"
        else:
            viewed_page_label = "Prepare exact destination load"
    if current_workspace_state.status is not WorkspaceStatus.REGISTERED:
        stages = _locked_stages(current_workspace_state.workspace_id)
        setup_page = (
            "files"
            if current_workspace_state.source_mode is SourceMode.FILE
            else "target"
        )
        return WorkspaceNavigation(
            workspace_id=current_workspace_state.workspace_id,
            migration_project_name=navigation_name,
            registered=False,
            setup_active=True,
            setup_href=f"/workspaces/{current_workspace_state.workspace_id}/{setup_page}",
            overview_href=None,
            overview_active=False,
            current_stage_id="setup",
            current_stage_label="Add source data",
            viewed_stage_id="",
            viewed_page_label=viewed_page_label,
            stages=stages,
        )

    if current_workspace_state.source_mode is SourceMode.ODOO:
        # Cross-instance transfers advance only through separately bound source
        # and destination evidence. Legacy same-database pinned-update evidence
        # must not make transfer stages look available.
        model_catalog_present = facts.odoo_model_catalog_present
        schema_present = facts.schema_present
        schema_attention = facts.schema_attention
        capture_plans_complete = schema_present and (
            set(facts.capture_models) == set(facts.schema_models)
        )
        frozen_source_hash = facts.source_selection_hash
        workspace_id = current_workspace_state.workspace_id
        select_complete = capture_plans_complete and not schema_attention
        destination_match_ready = bool(
            frozen_source_hash
            and schema_present
            and current_workspace_state.destination_match_ready(
                source_selection_hash=frozen_source_hash,
                source_schema_hash=facts.schema_content_hash,
            )
        )
        transfer_order_ready = bool(
            frozen_source_hash
            and schema_present
            and current_workspace_state.transfer_order_ready(
                source_selection_hash=frozen_source_hash,
                source_schema_hash=facts.schema_content_hash,
            )
        )
        transfer_review_current = bool(
            frozen_source_hash
            and schema_present
            and current_workspace_state.transfer_review_current(
                source_selection_hash=frozen_source_hash,
                source_schema_hash=facts.schema_content_hash,
            )
        )
        transfer_review_approved = bool(
            frozen_source_hash
            and schema_present
            and current_workspace_state.transfer_review_approved(
                source_selection_hash=frozen_source_hash,
                source_schema_hash=facts.schema_content_hash,
            )
        )
        transfer_preflight_current = bool(
            frozen_source_hash
            and schema_present
            and current_workspace_state.transfer_preflight_current(
                source_selection_hash=frozen_source_hash,
                source_schema_hash=facts.schema_content_hash,
            )
        )
        transfer_preflight_ready = bool(
            frozen_source_hash
            and schema_present
            and current_workspace_state.transfer_preflight_ready(
                source_selection_hash=frozen_source_hash,
                source_schema_hash=facts.schema_content_hash,
            )
        )
        transfer_run_id = facts.transfer_execution_run_id
        transfer_reconciliation_status = facts.transfer_reconciliation_status
        transfer_verified = bool(
            transfer_reconciliation_status == ReconciliationRunStatus.VERIFIED.value
        )
        if template_name == "workspace_target.html":
            viewed_stage_id = "connection"
            viewed_page_label = "Source connection"
        elif template_name == "workspace_odoo_capture_selection.html":
            viewed_stage_id = (
                "download"
                if select_complete or frozen_source_hash
                else "select"
            )
            viewed_page_label = (
                "Freeze source datasets"
                if select_complete or frozen_source_hash
                else "Define capture plans"
            )
        elif template_name == "workspace_odoo_capture_progress.html":
            viewed_stage_id = "download"
            viewed_page_label = "Freeze source datasets"
        elif template_name == "workspace_schema.html":
            viewed_stage_id = "select"
            viewed_page_label = "Choose record types and fields"
        elif template_name == "mapping/page.html":
            viewed_stage_id = "destination-match"

        stages = [
            _stage(
                workspace_id,
                "connection",
                1,
                "Connect source Odoo",
                "/target",
                status="complete",
                status_label="Connected",
                pages=(
                    _page(
                        workspace_id,
                        "source-connection",
                        "Source connection",
                        "/target",
                        complete=True,
                    ),
                ),
            ),
            _stage(
                workspace_id,
                "select",
                2,
                "Select data to download",
                "/schema",
                status=(
                    "attention"
                    if schema_attention
                    else ("complete" if select_complete else "current")
                ),
                status_label=(
                    "Review Odoo changes"
                    if schema_attention
                    else ("Selection complete" if select_complete else "Current")
                ),
                pages=(
                    _page(
                        workspace_id,
                        "odoo-fields",
                        "Choose record types and fields",
                        "/schema",
                        complete=(
                            model_catalog_present
                            and schema_present
                            and not schema_attention
                        ),
                        attention=schema_attention,
                    ),
                )
                + (
                    (
                        _page(
                            workspace_id,
                            "odoo-capture-selection",
                            "Define capture plans",
                            "/sources",
                            complete=capture_plans_complete,
                        ),
                    )
                    if schema_present
                    else ()
                ),
            ),
            WorkflowStage(
                stage_id="download",
                number=3,
                label="Download and freeze",
                href=(
                    f"/workspaces/{workspace_id}/sources"
                    if select_complete or frozen_source_hash
                    else None
                ),
                status=(
                    "complete"
                    if frozen_source_hash
                    else ("current" if select_complete else "locked")
                ),
                status_label=(
                    "Download complete"
                    if frozen_source_hash
                    else (
                        "Ready to download"
                        if select_complete
                        else "Finish data selection first"
                    )
                ),
                pages=(
                    _page(
                        workspace_id,
                        "odoo-capture",
                        "Freeze source datasets",
                        "/sources#current-capture",
                        complete=bool(frozen_source_hash),
                    ),
                )
                if capture_plans_complete
                else (),
            ),
            WorkflowStage(
                stage_id="destination",
                number=4,
                label="Connect destination Odoo",
                href=(
                    f"/workspaces/{workspace_id}/transfer-destination"
                    if frozen_source_hash
                    else None
                ),
                status=(
                    "complete"
                    if current_workspace_state.destination_verified
                    else ("current" if frozen_source_hash else "locked")
                ),
                status_label=(
                    "Connected"
                    if current_workspace_state.destination_verified
                    else (
                        "Current"
                        if frozen_source_hash
                        else "Download source first"
                    )
                ),
                pages=(
                    _page(
                        workspace_id,
                        "transfer-destination",
                        "Connect destination Odoo",
                        "/transfer-destination",
                        complete=current_workspace_state.destination_verified,
                    ),
                )
                if frozen_source_hash
                else (),
            ),
            WorkflowStage(
                stage_id="destination-match",
                number=5,
                label="Match destination data",
                href=(
                    f"/workspaces/{workspace_id}/destination-matching"
                    if current_workspace_state.destination_verified
                    and frozen_source_hash
                    else None
                ),
                status=(
                    "complete"
                    if destination_match_ready
                    else (
                        "attention"
                        if current_workspace_state.destination_match_plan is not None
                        and current_workspace_state.destination_verified
                        else (
                            "current"
                            if current_workspace_state.destination_verified
                            and frozen_source_hash
                            else "locked"
                        )
                    )
                ),
                status_label=(
                    "Matching ready"
                    if destination_match_ready
                    else (
                        "Review matching"
                        if current_workspace_state.destination_match_plan is not None
                        and current_workspace_state.destination_verified
                        else (
                            "Current"
                            if current_workspace_state.destination_verified
                            and frozen_source_hash
                            else "Destination required"
                        )
                    )
                ),
                pages=(
                    _page(
                        workspace_id,
                        "destination-matching",
                        "Match destination data",
                        "/destination-matching",
                        complete=destination_match_ready,
                        attention=(
                            current_workspace_state.destination_match_plan is not None
                            and not destination_match_ready
                        ),
                    ),
                )
                if current_workspace_state.destination_verified
                and frozen_source_hash
                else (),
            ),
            WorkflowStage(
                stage_id="transfer-order",
                number=6,
                label="Validate transfer order",
                href=(
                    f"/workspaces/{workspace_id}/transfer-order"
                    if destination_match_ready
                    else None
                ),
                status=(
                    "complete"
                    if transfer_order_ready
                    else (
                        "attention"
                        if current_workspace_state.transfer_order_plan is not None
                        and destination_match_ready
                        else ("current" if destination_match_ready else "locked")
                    )
                ),
                status_label=(
                    "Order ready"
                    if transfer_order_ready
                    else (
                        "Review order"
                        if current_workspace_state.transfer_order_plan is not None
                        and destination_match_ready
                        else (
                            "Current"
                            if destination_match_ready
                            else "Destination matching required"
                        )
                    )
                ),
                pages=(
                    _page(
                        workspace_id,
                        "transfer-order",
                        "Validate transfer order",
                        "/transfer-order",
                        complete=transfer_order_ready,
                        attention=(
                            current_workspace_state.transfer_order_plan is not None
                            and not transfer_order_ready
                        ),
                    ),
                )
                if destination_match_ready
                else (),
            ),
            WorkflowStage(
                stage_id="transfer-review",
                number=7,
                label="Review transfer",
                href=(
                    f"/workspaces/{workspace_id}/transfer-review"
                    if transfer_order_ready
                    else None
                ),
                status=(
                    "complete"
                    if transfer_review_approved
                    else (
                        "attention"
                        if current_workspace_state.transfer_review_package is not None
                        and transfer_order_ready
                        else ("current" if transfer_order_ready else "locked")
                    )
                ),
                status_label=(
                    "Transfer approved"
                    if transfer_review_approved
                    else (
                        "Approval required"
                        if transfer_review_current
                        else (
                            "Rebuild review"
                            if current_workspace_state.transfer_review_package
                            is not None
                            and transfer_order_ready
                            else (
                                "Current"
                                if transfer_order_ready
                                else "Transfer order required"
                            )
                        )
                    )
                ),
                pages=(
                    _page(
                        workspace_id,
                        "transfer-review",
                        "Review transfer",
                        "/transfer-review",
                        complete=transfer_review_approved,
                        attention=(
                            current_workspace_state.transfer_review_package
                            is not None
                            and not transfer_review_approved
                        ),
                    ),
                )
                if transfer_order_ready
                else (),
            ),
            WorkflowStage(
                stage_id="destination-load",
                number=8,
                label="Load destination Odoo",
                href=(
                    f"/workspaces/{workspace_id}/transfer-load/outcome"
                    if transfer_run_id
                    else (
                        f"/workspaces/{workspace_id}/transfer-load"
                        if transfer_preflight_ready
                        else (
                            f"/workspaces/{workspace_id}/transfer-preflight"
                            if transfer_review_approved
                            else None
                        )
                    )
                ),
                status=(
                    "complete"
                    if transfer_verified
                    else (
                        "attention"
                        if transfer_run_id
                        or (
                            transfer_preflight_current
                            and not transfer_preflight_ready
                        )
                        else ("current" if transfer_review_approved else "locked")
                    )
                ),
                status_label=(
                    "Destination load verified"
                    if transfer_verified
                    else (
                        "Verify saved load outcome"
                        if transfer_run_id
                        else (
                            "Ready to prepare and load"
                            if transfer_preflight_ready
                            else (
                                "Destination drift found"
                                if transfer_preflight_current
                                else (
                                    "Run read-only preflight"
                                    if transfer_review_approved
                                    else "Transfer approval required"
                                )
                            )
                        )
                    )
                ),
                pages=(
                    _page(
                        workspace_id,
                        "transfer-preflight",
                        "Read-only destination preflight",
                        "/transfer-preflight",
                        complete=transfer_preflight_ready,
                        attention=(
                            transfer_preflight_current
                            and not transfer_preflight_ready
                        ),
                    ),
                )
                + (
                    (
                        _page(
                            workspace_id,
                            "transfer-load",
                            "Prepare and load destination",
                            "/transfer-load",
                            complete=bool(transfer_run_id),
                            attention=(
                                bool(transfer_run_id) and not transfer_verified
                            ),
                        ),
                    )
                    if transfer_preflight_ready or transfer_run_id
                    else ()
                )
                + (
                    (
                        _page(
                            workspace_id,
                            "transfer-verify",
                            "Verify destination result",
                            "/transfer-load/outcome",
                            complete=transfer_verified,
                            attention=not transfer_verified,
                        ),
                    )
                    if transfer_run_id
                    else ()
                )
                if transfer_review_approved
                else (),
            ),
        ]
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    workspace_id = current_workspace_state.workspace_id
    sources_confirmed = bool(current_workspace_state.source_files) and (
        facts.source_configuration_count == len(current_workspace_state.source_files)
        and facts.selected_source_configuration_count
        == facts.source_configuration_count
    )
    source_complete = facts.source_complete
    stages: list[WorkflowStage] = [
        _stage(
            workspace_id,
            "source",
            1,
            "Source data",
            "/sources",
            status=("complete" if source_complete else "current"),
            status_label=("Complete" if source_complete else "Current"),
            pages=(
                _page(
                    workspace_id,
                    "source-files",
                    "Check source files",
                    "/sources",
                    complete=sources_confirmed or source_complete,
                ),
                _page(
                    workspace_id,
                    "datasets",
                    (
                        "Saved source tables"
                        if source_complete
                        else "Save table choices"
                    ),
                    (
                        "/datasets#tables-ready"
                        if source_complete
                        else "/sources#table-choices"
                    ),
                    complete=source_complete,
                ),
                _page(
                    workspace_id,
                    "derived-entities",
                    "Separate combined information",
                    "/derived-entities",
                    complete=facts.derived_rules_present,
                    optional=True,
                ),
            ),
        )
    ]
    if not source_complete:
        stages.extend(_locked_stages(workspace_id, after="source"))
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    schema_complete = facts.schema_complete
    schema_attention = facts.schema_attention
    stages.append(
        _stage(
            workspace_id,
            "odoo",
            2,
            "Odoo data",
            "/schema",
            status=(
                "attention"
                if schema_attention
                else ("complete" if schema_complete else "current")
            ),
            status_label=(
                "Needs attention"
                if schema_attention
                else ("Complete" if schema_complete else "Current")
            ),
            pages=(
                _page(
                    workspace_id,
                    "schema",
                    "Choose Odoo records",
                    "/schema",
                    complete=schema_complete,
                ),
            ),
        )
    )
    if not schema_complete:
        stages.extend(_locked_stages(workspace_id, after="odoo"))
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    mapping_complete = facts.mapping_complete
    stages.append(
        _stage(
            workspace_id,
            "match",
            3,
            "Match data",
            "/mapping",
            status=("complete" if mapping_complete else "current"),
            status_label=("Complete" if mapping_complete else "Current"),
            pages=(
                _page(
                    workspace_id,
                    "mapping",
                    "Match fields",
                    "/mapping",
                    complete=mapping_complete,
                ),
                _page(
                    workspace_id,
                    "transformation-impact",
                    "Review rule effects",
                    "/mapping/transformation-impact",
                    optional=True,
                ),
            ),
        )
    )
    if not mapping_complete:
        stages.extend(_locked_stages(workspace_id, after="match"))
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    active_job_id = facts.active_preparation_job_id
    if active_job_id:
        stages.append(
            _stage(
                workspace_id,
                "prepare",
                4,
                "Prepare data",
                "/prepare",
                status="current",
                status_label="In progress",
                pages=(
                    _page(
                        workspace_id,
                        "prepare",
                        "Start preparation",
                        "/prepare",
                    ),
                    WorkflowPage(
                        page_id="preparation-progress",
                        label="Preparation progress",
                        href=(
                            f"/workspaces/{workspace_id}/preparation/"
                            f"{active_job_id}"
                        ),
                        status="current",
                        status_label="In progress",
                    ),
                ),
            )
        )
        stages.extend(_locked_stages(workspace_id, after="prepare"))
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    staging_run_id = facts.staging_run_id
    resolution_status = (
        facts.resolution_status
        if facts.resolution_staging_run_id == staging_run_id
        else ""
    )
    quality_run_id = (
        facts.quality_run_id
        if facts.quality_staging_run_id == staging_run_id
        else ""
    )
    normalization_current = bool(
        quality_run_id
        and facts.normalization_staging_run_id == staging_run_id
        and facts.normalization_quality_run_id == quality_run_id
    )
    preparation_complete = bool(
        normalization_current and facts.normalization_frozen
    )
    preparation_attention = bool(
        resolution_status in {"BLOCKED", "REVIEW_REQUIRED"}
    ) or bool(
        normalization_current
        and not facts.normalization_frozen
        and facts.normalization_decisions_left
    )
    preparation_status = (
        "complete"
        if preparation_complete
        else ("attention" if preparation_attention else "current")
    )
    preparation_label = (
        "Complete"
        if preparation_complete
        else ("Needs attention" if preparation_attention else "Current")
    )
    preparation_pages = [
        _page(
            workspace_id,
            "prepare",
            "Start preparation",
            "/prepare",
            complete=bool(staging_run_id),
        )
    ]
    if template_name == "workspace_preparation_progress.html" and current_path:
        preparation_pages.append(
            WorkflowPage(
                page_id="preparation-progress",
                label="Preparation progress",
                href=current_path,
                status="available",
                status_label="Saved attempt",
            )
        )
    if resolution_status:
        preparation_pages.append(
            _page(
                workspace_id,
                "resolution",
                "Review possible duplicates",
                "/resolution",
                complete=resolution_status == "FROZEN",
                attention=resolution_status in {"BLOCKED", "REVIEW_REQUIRED"},
            )
        )
    if normalization_current:
        preparation_pages.append(
            _page(
                workspace_id,
                "normalization",
                "Approve prepared data",
                "/normalization",
                complete=facts.normalization_frozen,
                attention=bool(facts.normalization_decisions_left),
            )
        )
    stages.append(
        _stage(
            workspace_id,
            "prepare",
            4,
            "Prepare data",
            "/prepare",
            status=preparation_status,
            status_label=preparation_label,
            pages=tuple(preparation_pages),
        )
    )
    if not preparation_complete:
        stages.extend(_locked_stages(workspace_id, after="prepare"))
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    review_complete = facts.preflight_status == "READY"
    review_attention = bool(
        facts.preflight_status and facts.preflight_status != "READY"
    )
    review_status = (
        "complete"
        if review_complete
        else ("attention" if review_attention else "current")
    )
    review_label = (
        "Complete"
        if review_complete
        else ("Needs attention" if review_attention else "Current")
    )
    stages.append(
        _stage(
            workspace_id,
            "review",
            5,
            "Final review",
            "/summary",
            status=review_status,
            status_label=review_label,
            pages=(
                _page(
                    workspace_id,
                    "summary",
                    "Review and compare",
                    "/summary",
                    complete=review_complete,
                    attention=review_attention,
                ),
            ),
        )
    )
    if not review_complete:
        stages.extend(_locked_stages(workspace_id, after="review"))
        return _navigation(
            current_workspace_state,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
            migration_project_name=navigation_name,
        )

    load_status = "current"
    load_label = "Current"
    active_load_job_id = facts.active_load_job_id
    preview = facts.execution_preview
    if preview is None:
        load_status = "attention"
        load_label = "Needs attention"
    reconciliation = None
    if active_load_job_id:
        load_label = "In progress"
    elif preview is not None:
        if preview.write_count == 0 and not preview.scope_error:
            load_status = "complete"
            load_label = "No changes needed"
        elif preview.current_run_id:
            reconciliation = preview.state.reconciliation_status
            if reconciliation == ReconciliationRunStatus.VERIFIED.value:
                load_status = "complete"
                load_label = "Complete"
            elif reconciliation:
                load_status = "attention"
                load_label = "Needs attention"
            else:
                load_label = "Verify outcome"
        elif not preview.can_load:
            load_status = "attention"
            load_label = "Needs attention"
    review_page = _page(
        workspace_id,
        "load-review",
        "Check changes",
        "/load/review",
        complete=preview is not None,
        attention=preview is not None and not preview.can_load,
    )
    if active_load_job_id:
        confirm_page = WorkflowPage(
            page_id="load-confirm",
            label="Confirm and load",
            href=(
                f"/workspaces/{workspace_id}/load/progress/"
                f"{active_load_job_id}"
            ),
            status="current",
            status_label="In progress",
        )
        outcome_page = WorkflowPage(
            page_id="load-outcome",
            label="Verify result",
            href=None,
            status="locked",
            status_label="Not ready",
        )
    elif preview is not None and preview.current_run_id:
        confirm_page = WorkflowPage(
            page_id="load-confirm",
            label="Confirm and load",
            href=None,
            status="complete",
            status_label="Complete",
        )
        outcome_page = _page(
            workspace_id,
            "load-outcome",
            "Verify result",
            "/load/outcome",
            complete=(
                reconciliation == ReconciliationRunStatus.VERIFIED.value
            ),
            attention=(
                reconciliation != ReconciliationRunStatus.VERIFIED.value
            ),
        )
    else:
        confirm_page = (
            _page(
                workspace_id,
                "load-confirm",
                "Confirm and load",
                "/load/confirm",
            )
            if preview is not None and preview.can_load
            else WorkflowPage(
                page_id="load-confirm",
                label="Confirm and load",
                href=None,
                status="locked",
                status_label="Review first",
            )
        )
        outcome_page = WorkflowPage(
            page_id="load-outcome",
            label="Verify result",
            href=None,
            status="locked",
            status_label="Not started",
        )
    stages.append(
        _stage(
            workspace_id,
            "load",
            6,
            "Load into Odoo",
            "/load",
            status=load_status,
            status_label=load_label,
            pages=(review_page, confirm_page, outcome_page),
        )
    )
    return _navigation(
        current_workspace_state,
        template_name,
        viewed_stage_id,
        viewed_page_label,
        stages,
        migration_project_name=navigation_name,
    )


def _navigation(
    workspace_state: WorkspaceState,
    template_name: str,
    viewed_stage_id: str,
    viewed_page_label: str,
    stages: list[WorkflowStage],
    *,
    migration_project_name: str | None = None,
) -> WorkspaceNavigation:
    current_stage = next(
        (
            stage
            for stage in stages
            if stage.status in {"current", "attention"}
        ),
        next(
            (stage for stage in reversed(stages) if stage.available),
            stages[-1],
        ),
    )


    active_stages = tuple(
        replace(
            stage,
            active=stage.stage_id == viewed_stage_id,
            pages=tuple(
                replace(
                    page,
                    current=(
                        stage.stage_id == viewed_stage_id
                        and page.label == viewed_page_label
                    ),
                )
                for page in stage.pages
            ),
        )
        for stage in stages
    )
    return WorkspaceNavigation(
        workspace_id=workspace_state.workspace_id,
        migration_project_name=migration_project_name or workspace_state.name,
        registered=True,
        setup_active=False,
        setup_href=f"/workspaces/{workspace_state.workspace_id}/overview",
        overview_href=f"/workspaces/{workspace_state.workspace_id}/overview",
        overview_active=template_name == "workspace_overview.html",
        odoo_access_href=(
            f"/workspaces/{workspace_state.workspace_id}/target"
            if workspace_state.source_mode is SourceMode.FILE
            else None
        ),
        odoo_access_active=(
            workspace_state.source_mode is SourceMode.FILE
            and template_name == "workspace_target.html"
        ),
        current_stage_id=current_stage.stage_id,
        current_stage_label=current_stage.label,
        viewed_stage_id=viewed_stage_id,
        viewed_page_label=viewed_page_label,
        stages=active_stages,
    )


def _recipe_run_setup_navigation(
    navigation: WorkspaceNavigation,
    workspace_view: WorkspaceOwnerView,
    *,
    template_name: str,
    run_setup_complete: bool | None = None,
    fresh_data_complete: bool | None = None,
) -> WorkspaceNavigation:
    """Present fresh data and Odoo review as one run-owned setup journey."""

    workspace_id = workspace_view.workspace_id
    purpose = workspace_view.migration_run.purpose.value
    run_kind = "test-runs" if purpose == "TEST" else "production-runs"
    fresh_home = (
        f"/projects/{workspace_view.project_id}/{run_kind}/"
        f"{workspace_view.migration_run_id}/fresh-data"
    )
    run_home = (
        f"/projects/{workspace_view.project_id}/{run_kind}/"
        f"{workspace_view.migration_run_id}/activate" if purpose == "PRODUCTION" else fresh_home
    )
    odoo_home = (
        f"/projects/{workspace_view.project_id}/runs/"
        f"{workspace_view.migration_run_id}/odoo"
    )
    fresh_complete = (workspace_view.data_version.state.value == "FROZEN"
                      if fresh_data_complete is None else fresh_data_complete)
    odoo_stage = _find_stage(navigation.stages, "odoo")
    fresh = WorkflowStage(
        stage_id="fresh",
        number=1,
        label="Fresh data",
        href=fresh_home,
        status="complete" if fresh_complete else "current",
        status_label="Complete" if fresh_complete else "Current",
    )
    if not fresh_complete:
        odoo_status = "locked"
        odoo_label = "Accept fresh data first"
        odoo_href = None
    elif odoo_stage is None or odoo_stage.status == "locked":
        odoo_status = "current"
        odoo_label = "Current"
        odoo_href = odoo_home
    else:
        odoo_status = odoo_stage.status
        odoo_label = odoo_stage.status_label
        odoo_href = odoo_home
    if purpose == "PRODUCTION" and fresh_complete:
        odoo_status = "complete" if run_setup_complete else "current"
        odoo_label = "Complete" if run_setup_complete else "Current"
        odoo_href = run_home if run_setup_complete else odoo_home
    odoo = WorkflowStage(
        stage_id="odoo",
        number=2,
        label="Check Odoo",
        href=odoo_href,
        status=odoo_status,
        status_label=odoo_label,
    )
    odoo_complete = odoo.status == "complete"
    review = WorkflowStage(
        stage_id="review",
        number=3,
        label="Review and load",
        href=(f"/projects/{workspace_view.project_id}/runs/{workspace_view.migration_run_id}"
              if purpose == "PRODUCTION" else run_home) if fresh_complete and odoo_complete else None,
        status="current" if fresh_complete and odoo_complete else "locked",
        status_label=(
            "Current" if fresh_complete and odoo_complete else "Finish Odoo check first"
        ),
    )
    if template_name in {"workspace_files.html", "workspace_sources.html", "workspace_datasets.html", "workspace_derived_entities.html", "project_run_fresh_data.html"}:
        viewed_stage_id = "fresh"
    elif template_name in {
        "workspace_schema.html",
        "workspace_target.html",
        "project_production_activation.html",
        "project_recipe_run_progress.html",
    }:
        viewed_stage_id = "odoo"
    else:
        viewed_stage_id = "review"
    current_stage_id = (
        "fresh"
        if not fresh_complete
        else "odoo"
        if not odoo_complete
        else "review"
    )
    stages = _activate_run_stages((fresh, odoo, review), viewed_stage_id)
    viewed_page_label = {
        "project_run_fresh_data.html": "Fresh data",
        "workspace_schema.html": "Review Odoo requirements",
        "project_production_activation.html": "Review Production readiness",
        "project_recipe_run_progress.html": "Odoo check progress",
    }.get(template_name, navigation.viewed_page_label)
    return WorkspaceNavigation(
        workspace_id=workspace_id,
        migration_project_name=navigation.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=fresh_home,
        overview_href=run_home,
        overview_active=False,
        current_stage_id=current_stage_id,
        current_stage_label=next(
            stage.label for stage in stages if stage.stage_id == current_stage_id
        ),
        viewed_stage_id=viewed_stage_id,
        viewed_page_label=viewed_page_label,
        stages=stages,
        journey=WorkspaceJourney.RECIPE_RUN_SETUP.value,
        journey_label="Recipe run",
        overview_label="Run setup",
        current_work_label="Current run work",
    )


def _recipe_application_navigation(
    navigation: WorkspaceNavigation,
    *,
    project_id: str,
    migration_run_id: str,
    run_purpose: str = "TEST",
    template_name: str = "",
) -> WorkspaceNavigation:
    """Collapse an application workspace into the run's review-and-load step."""

    run_home = f"/projects/{project_id}/runs/{migration_run_id}"
    fresh_home = (
        f"/projects/{project_id}/test-runs/{migration_run_id}/fresh-data"
        if run_purpose == "TEST" else
        f"/projects/{project_id}/production-runs/{migration_run_id}/fresh-data"
    )
    target_value_review = template_name in {
        "project_recipe_target_matches.html",
        "project_recipe_run_progress.html",
    }
    review_stages = tuple(
        stage
        for stage in navigation.stages
        if stage.stage_id in {"prepare", "review", "load"}
    )
    if review_stages and all(stage.status == "complete" for stage in review_stages):
        review_status = "complete"
        review_label = "Complete"
    elif any(stage.status == "attention" for stage in review_stages):
        review_status = "attention"
        review_label = "Needs attention"
    else:
        review_status = "current"
        review_label = "Current"
    stages = _activate_run_stages(
        (
            WorkflowStage(
                stage_id="fresh",
                number=1,
                label="Fresh data",
                href=fresh_home,
                status="complete",
                status_label="Complete",
            ),
            WorkflowStage(
                stage_id="odoo",
                number=2,
                label="Check Odoo",
                href=f"{run_home}/odoo",
                status="attention" if target_value_review else "complete",
                status_label=(
                    "Review target values" if target_value_review else "Complete"
                ),
            ),
            WorkflowStage(
                stage_id="review",
                number=3,
                label="Review and load",
                href=run_home,
                status="locked" if target_value_review else review_status,
                status_label=(
                    "Confirm target values first"
                    if target_value_review
                    else review_label
                ),
            ),
        ),
        "odoo" if target_value_review else "review",
    )
    return WorkspaceNavigation(
        workspace_id=navigation.workspace_id,
        migration_project_name=navigation.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=run_home,
        overview_href=run_home,
        overview_active=False,
        current_stage_id="odoo" if target_value_review else "review",
        current_stage_label=(
            "Check Odoo" if target_value_review else "Review and load"
        ),
        viewed_stage_id="odoo" if target_value_review else "review",
        viewed_page_label=navigation.viewed_page_label,
        stages=stages,
        journey=WorkspaceJourney.RECIPE_APPLICATION.value,
        journey_label="Recipe run",
        overview_label="Run overview",
        current_work_label="Current run work",
    )


def _find_stage(
    stages: tuple[WorkflowStage, ...],
    stage_id: str,
) -> WorkflowStage | None:
    return next((stage for stage in stages if stage.stage_id == stage_id), None)


def _activate_run_stages(
    stages: tuple[WorkflowStage, ...],
    viewed_stage_id: str,
) -> tuple[WorkflowStage, ...]:
    return tuple(
        replace(stage, active=stage.stage_id == viewed_stage_id)
        for stage in stages
    )


def build_preparation_workspace_navigation(job: PreparationJob) -> WorkspaceNavigation:
    """Build Stage-4 navigation entirely from the in-memory job snapshot."""

    workspace_id = job.workspace_id
    progress_url = f"/workspaces/{workspace_id}/preparation/{job.job_id}"
    stages = (
        WorkflowStage(
            stage_id="source",
            number=1,
            label="Source data",
            href=f"/workspaces/{workspace_id}/sources",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="odoo",
            number=2,
            label="Odoo data",
            href=f"/workspaces/{workspace_id}/schema",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="match",
            number=3,
            label="Match data",
            href=f"/workspaces/{workspace_id}/mapping",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="prepare",
            number=4,
            label="Prepare data",
            href=f"/workspaces/{workspace_id}/prepare",
            status="current",
            status_label=("In progress" if job.active else "Saved attempt"),
            pages=(
                WorkflowPage(
                    page_id="prepare",
                    label="Start preparation",
                    href=f"/workspaces/{workspace_id}/prepare",
                ),
                WorkflowPage(
                    page_id="preparation-progress",
                    label="Preparation progress",
                    href=progress_url,
                    status="current",
                    status_label=("In progress" if job.active else "Saved attempt"),
                    current=True,
                ),
            ),
            active=True,
        ),
        *_locked_stages(workspace_id, after="prepare"),
    )
    navigation = WorkspaceNavigation(
        workspace_id=workspace_id,
        migration_project_name=job.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=f"/workspaces/{workspace_id}/overview",
        overview_href=f"/workspaces/{workspace_id}/overview",
        overview_active=False,
        current_stage_id="prepare",
        current_stage_label="Prepare data",
        viewed_stage_id="prepare",
        viewed_page_label="Preparation progress",
        stages=stages,
    )
    if job.workspace.recipe_application_id is None:
        return navigation
    return _recipe_application_navigation(
        navigation,
        project_id=job.workspace.project_id,
        migration_run_id=job.workspace.migration_run_id,
        run_purpose=job.workspace.migration_run_purpose.value,
    )


def build_preflight_workspace_navigation(job: PreflightJob) -> WorkspaceNavigation:
    """Keep Stage-5 navigation visible while the comparison uses the workspace."""

    workspace_id = job.workspace_id
    summary_url = f"/workspaces/{workspace_id}/summary"
    progress_url = f"/workspaces/{workspace_id}/preflight/{job.job_id}"
    if job.active:
        status, status_label = "current", "In progress"
    elif job.status.value == "FAILED":
        status, status_label = "attention", "Needs attention"
    else:
        status, status_label = "complete", "Complete"
    prior_stages = tuple(
        WorkflowStage(
            stage_id=stage_id,
            number=number,
            label=label,
            href=f"/workspaces/{workspace_id}/{suffix}",
            status="complete",
            status_label="Complete",
        )
        for stage_id, number, label, suffix in (
            ("source", 1, "Source data", "sources"),
            ("odoo", 2, "Odoo data", "schema"),
            ("match", 3, "Match data", "mapping"),
            ("prepare", 4, "Prepare data", "prepare"),
        )
    )
    review_stage = WorkflowStage(
        stage_id="review",
        number=5,
        label="Final review",
        href=summary_url,
        status=status,
        status_label=status_label,
        pages=(
            WorkflowPage(
                page_id="summary",
                label="Review and compare",
                href=summary_url,
            ),
            WorkflowPage(
                page_id="preflight-progress",
                label="Comparison progress",
                href=progress_url,
                status=status,
                status_label=status_label,
                current=True,
            ),
        ),
        active=True,
    )
    navigation = WorkspaceNavigation(
        workspace_id=workspace_id,
        migration_project_name=job.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=f"/workspaces/{workspace_id}/overview",
        overview_href=f"/workspaces/{workspace_id}/overview",
        overview_active=False,
        current_stage_id="review",
        current_stage_label="Final review",
        viewed_stage_id="review",
        viewed_page_label="Comparison progress",
        stages=(*prior_stages, review_stage, *_locked_stages(workspace_id, after="review")),
    )
    if job.access_context.recipe_application_id is None:
        return navigation
    return _recipe_application_navigation(
        navigation,
        project_id=job.access_context.project_id,
        migration_run_id=job.access_context.migration_run_id,
        run_purpose=job.access_context.run_purpose or "TEST",
    )


def build_load_workspace_navigation(job: LoadJob) -> WorkspaceNavigation:
    """Build Stage-6 navigation without opening the busy workspace database."""

    workspace_id = job.workspace_id
    progress_url = f"/workspaces/{workspace_id}/load/progress/{job.job_id}"
    load_status = (
        "current"
        if job.active
        else "attention"
        if job.status.value == "FAILED"
        else "current"
    )
    load_label = (
        "In progress"
        if job.active
        else "Needs attention"
        if job.status.value == "FAILED"
        else "Verify outcome"
    )
    prior_stages = (
        WorkflowStage(
            stage_id="source",
            number=1,
            label="Source data",
            href=f"/workspaces/{workspace_id}/sources",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="odoo",
            number=2,
            label="Odoo data",
            href=f"/workspaces/{workspace_id}/schema",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="match",
            number=3,
            label="Match data",
            href=f"/workspaces/{workspace_id}/mapping",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="prepare",
            number=4,
            label="Prepare data",
            href=f"/workspaces/{workspace_id}/prepare",
            status="complete",
            status_label="Complete",
        ),
        WorkflowStage(
            stage_id="review",
            number=5,
            label="Final review",
            href=f"/workspaces/{workspace_id}/summary",
            status="complete",
            status_label="Complete",
        ),
    )
    load_stage = WorkflowStage(
        stage_id="load",
        number=6,
        label="Load into Odoo",
        href=progress_url,
        status=load_status,
        status_label=load_label,
        pages=(
            WorkflowPage(
                page_id="load-review",
                label="Check changes",
                href=f"/workspaces/{workspace_id}/load/review",
                status="complete",
                status_label="Complete",
            ),
            WorkflowPage(
                page_id="load-confirm",
                label="Confirm and load",
                href=progress_url,
                status=("current" if job.active else load_status),
                status_label=load_label,
                current=True,
            ),
            WorkflowPage(
                page_id="load-outcome",
                label="Verify result",
                href=(
                    f"/workspaces/{workspace_id}/load/outcome"
                    if job.status.value == "SUCCEEDED"
                    else None
                ),
                status=("available" if job.status.value == "SUCCEEDED" else "locked"),
                status_label=(
                    "Available" if job.status.value == "SUCCEEDED" else "Not ready"
                ),
            ),
        ),
        active=True,
    )
    navigation = WorkspaceNavigation(
        workspace_id=workspace_id,
        migration_project_name=job.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=f"/workspaces/{workspace_id}/overview",
        overview_href=f"/workspaces/{workspace_id}/overview",
        overview_active=False,
        current_stage_id="load",
        current_stage_label="Load into Odoo",
        viewed_stage_id="load",
        viewed_page_label="Confirm and load",
        stages=(*prior_stages, load_stage),
    )
    if job.access_context.recipe_application_id is None:
        return navigation
    return _recipe_application_navigation(
        navigation,
        project_id=job.access_context.project_id,
        migration_run_id=job.access_context.migration_run_id,
        run_purpose=job.access_context.run_purpose or "TEST",
    )


def _stage(
    workspace_id: str,
    stage_id: str,
    number: int,
    label: str,
    suffix: str,
    *,
    status: str,
    status_label: str,
    pages: tuple[WorkflowPage, ...],
    pages_always_visible: bool = False,
) -> WorkflowStage:
    return WorkflowStage(
        stage_id=stage_id,
        number=number,
        label=label,
        href=f"/workspaces/{workspace_id}{suffix}",
        status=status,
        status_label=status_label,
        pages=pages,
        pages_always_visible=pages_always_visible,
    )


def _page(
    workspace_id: str,
    page_id: str,
    label: str,
    suffix: str,
    *,
    complete: bool = False,
    attention: bool = False,
    optional: bool = False,
) -> WorkflowPage:
    if complete:
        status, status_label = "complete", "Complete"
    elif attention:
        status, status_label = "attention", "Needs attention"
    elif optional:
        status, status_label = "optional", "Optional"
    else:
        status, status_label = "available", "Available"
    return WorkflowPage(
        page_id=page_id,
        label=label,
        href=f"/workspaces/{workspace_id}{suffix}",
        status=status,
        status_label=status_label,
        optional=optional,
    )


def _locked_stages(
    workspace_id: str,
    *,
    after: str | None = None,
) -> tuple[WorkflowStage, ...]:
    definitions = (
        ("source", 1, "Source data", "/sources"),
        ("odoo", 2, "Odoo data", "/schema"),
        ("match", 3, "Match data", "/mapping"),
        ("prepare", 4, "Prepare data", "/prepare"),
        ("review", 5, "Final review", "/summary"),
        ("load", 6, "Load into Odoo", "/load"),
    )
    include = after is None
    stages: list[WorkflowStage] = []
    for stage_id, number, label, suffix in definitions:
        if include:
            stages.append(
                WorkflowStage(
                    stage_id=stage_id,
                    number=number,
                    label=label,
                    href=None,
                    status="locked",
                    status_label="Not yet available",
                )
            )
        if stage_id == after:
            include = True
    return tuple(stages)
