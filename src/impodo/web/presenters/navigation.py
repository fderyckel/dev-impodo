"""Build the project workflow navigation from current local evidence.

The presenter keeps page location separate from migration progress.  It uses
only bounded local projections and never contacts Odoo or changes project
state.  Downstream stages are evaluated only after their prerequisites are
current, so a changed upstream choice immediately locks stale later work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ...domain.errors import ReadinessError
from ...domain.reconciliation import ReconciliationRunStatus
from impodo.application.workspace.execution.job_models import LoadJob
from impodo.application.workspace.preparation.job_models import PreparationJob
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState, WorkspaceStatus
from impodo.application.workspace.views import WorkspaceOwnerView
from ..context import WebContext
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

    @property
    def available(self) -> bool:
        return self.href is not None


@dataclass(frozen=True, slots=True)
class WorkspaceNavigation:
    """Complete navigation context for one rendered workspace page."""

    workspace_id: str
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
    "workspace_datasets.html": ("source", "Saved source tables"),
    "workspace_derived_entities.html": (
        "source",
        "Separate combined information",
    ),
    "workspace_schema.html": ("odoo", "Choose Odoo records"),
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
    context: WebContext,
    workspace_state: WorkspaceState,
    template_name: str,
    *,
    current_path: str = "",
    migration_project_name: str | None = None,
    workspace_view: WorkspaceOwnerView | None = None,
) -> WorkspaceNavigation:
    """Return the one user journey allowed by canonical workspace ownership."""

    navigation = _build_authoring_workspace_navigation(
        context,
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
        )
    return _recipe_application_navigation(
        navigation,
        project_id=workspace_view.project_id,
        migration_run_id=workspace_view.migration_run_id,
    )


def _build_authoring_workspace_navigation(
    context: WebContext,
    workspace_state: WorkspaceState,
    template_name: str,
    *,
    current_path: str = "",
    migration_project_name: str | None = None,
) -> WorkspaceNavigation:
    """Build the source-mode-specific Authoring evidence projection."""

    current_workspace_state = context.queries.get(workspace_state.workspace_id)
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
        # This journey ends at a protected source download until a separately
        # bound destination contract exists.  Legacy same-database pinned-update
        # evidence must not make cross-instance transfer stages look available.
        model_catalog = context.queries.get_odoo_model_catalog(
            current_workspace_state.workspace_id
        )
        schema = context.queries.get_odoo_schema_catalog(
            current_workspace_state.workspace_id
        )
        schema_attention = bool(schema and schema.pending_refresh)
        capture_selections = (
            context.queries.get_current_odoo_capture_selections(
                current_workspace_state.workspace_id
            )
            if schema is not None
            else ()
        )
        capture_plans_complete = bool(schema) and {
            item.model for item in capture_selections
        } == {item.name for item in schema.models}
        try:
            frozen_source = context.queries.get_source_selection(
                current_workspace_state.workspace_id
            )
        except WorkspaceError:
            frozen_source = None
        workspace_id = current_workspace_state.workspace_id
        select_complete = capture_plans_complete and not schema_attention
        if template_name == "workspace_target.html":
            viewed_stage_id = "connection"
            viewed_page_label = "Source connection"
        elif template_name == "workspace_odoo_capture_selection.html":
            viewed_stage_id = (
                "download"
                if select_complete or frozen_source is not None
                else "select"
            )
            viewed_page_label = (
                "Freeze source datasets"
                if select_complete or frozen_source is not None
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
                            model_catalog is not None
                            and schema is not None
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
                    if schema is not None
                    else ()
                ),
            ),
            WorkflowStage(
                stage_id="download",
                number=3,
                label="Download and freeze",
                href=(
                    f"/workspaces/{workspace_id}/sources"
                    if select_complete or frozen_source is not None
                    else None
                ),
                status=(
                    "complete"
                    if frozen_source is not None
                    else ("current" if select_complete else "locked")
                ),
                status_label=(
                    "Download complete"
                    if frozen_source is not None
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
                        complete=frozen_source is not None,
                    ),
                )
                if capture_plans_complete
                else (),
            ),
            WorkflowStage(
                stage_id="destination",
                number=4,
                label="Connect destination Odoo",
                href=None,
                status="locked",
                status_label="Not yet available",
            ),
            WorkflowStage(
                stage_id="destination-match",
                number=5,
                label="Match destination data",
                href=None,
                status="locked",
                status_label="Destination required",
            ),
            WorkflowStage(
                stage_id="transfer-order",
                number=6,
                label="Validate transfer order",
                href=None,
                status="locked",
                status_label="Destination matching required",
            ),
            WorkflowStage(
                stage_id="transfer-review",
                number=7,
                label="Review transfer",
                href=None,
                status="locked",
                status_label="Transfer plan required",
            ),
            WorkflowStage(
                stage_id="destination-load",
                number=8,
                label="Load destination Odoo",
                href=None,
                status="locked",
                status_label="Not yet available",
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
    try:
        source_selection = context.queries.get_source_selection(workspace_id)
        source_configurations = context.queries.get_source_configurations(workspace_id)
        derived_plan = context.queries.get_derived_entity_plan(workspace_id)
    except WorkspaceError:
        source_selection = None
        source_configurations = ()
        derived_plan = None
    sources_confirmed = bool(current_workspace_state.source_files) and (
        len(source_configurations) == len(current_workspace_state.source_files)
        and all(item.selected_table_keys for item in source_configurations)
    )
    source_complete = source_selection is not None
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
                    complete=bool(derived_plan and derived_plan.rules),
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

    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    governance = context.queries.get_schema_governance(workspace_id)
    schema_complete = schema is not None and governance is not None
    schema_attention = bool(schema and schema.pending_refresh)
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

    revision = context.queries.get_mapping_revision(workspace_id)
    submission = (
        context.queries.get_mapping_submission(workspace_id, revision.version)
        if revision is not None
        else None
    )
    mapping_complete = bool(
        revision is not None
        and submission is not None
        and submission.mapping_id == revision.mapping_id
        and submission.mapping_content_hash == revision.definition.content_hash
    )
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

    active_job = (
        context.preparation_jobs.active(workspace_id)
        if context.preparation_jobs is not None
        else None
    )
    if active_job is not None:
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
                            f"{active_job.job_id}"
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

    staging = context.preflight.current_staging(workspace_id)
    resolution = context.resolution.current_summary(workspace_id) if staging else None
    quality = context.quality.current_summary(workspace_id) if staging else None
    if quality is not None and quality.staging_run_id != staging.run_id:
        quality = None
    normalization = context.normalization.current_summary(workspace_id) if quality else None
    if normalization is not None and (
        normalization.staging_run_id != staging.run_id
        or normalization.quality_run_id != quality.run_id
    ):
        normalization = None
    preparation_complete = bool(normalization and normalization.frozen)
    preparation_attention = bool(
        resolution and resolution.status in {"BLOCKED", "REVIEW_REQUIRED"}
    ) or bool(
        normalization
        and not normalization.frozen
        and normalization.decisions_left
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
            complete=staging is not None,
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
    if resolution is not None:
        preparation_pages.append(
            _page(
                workspace_id,
                "resolution",
                "Review possible duplicates",
                "/resolution",
                complete=resolution.status == "FROZEN",
                attention=resolution.status in {"BLOCKED", "REVIEW_REQUIRED"},
            )
        )
    if normalization is not None:
        preparation_pages.append(
            _page(
                workspace_id,
                "normalization",
                "Approve prepared data",
                "/normalization",
                complete=normalization.frozen,
                attention=bool(normalization.decisions_left),
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

    report = context.preflight.current_report(workspace_id)
    review_complete = bool(report and report.status == "READY")
    review_attention = bool(report and report.status != "READY")
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
    active_load_job = (
        context.load_jobs.active(workspace_id)
        if context.load_jobs is not None
        else None
    )
    try:
        preview = context.execution.current_preview(workspace_id)
    except (ReadinessError, WorkspaceError):
        preview = None
        load_status = "attention"
        load_label = "Needs attention"
    reconciliation = None
    if active_load_job is not None:
        load_label = "In progress"
    elif preview is not None:
        if preview.snapshot.write_count == 0:
            load_status = "complete"
            load_label = "No changes needed"
        elif preview.current_run is not None:
            reconciliation = context.reconciliation.current(workspace_id)
            if (
                reconciliation is not None
                and reconciliation.status is ReconciliationRunStatus.VERIFIED
            ):
                load_status = "complete"
                load_label = "Complete"
            elif reconciliation is not None:
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
    if active_load_job is not None:
        confirm_page = WorkflowPage(
            page_id="load-confirm",
            label="Confirm and load",
            href=(
                f"/workspaces/{workspace_id}/load/progress/"
                f"{active_load_job.job_id}"
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
    elif preview is not None and preview.current_run is not None:
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
                reconciliation is not None
                and reconciliation.status is ReconciliationRunStatus.VERIFIED
            ),
            attention=(
                reconciliation is None
                or reconciliation.status is not ReconciliationRunStatus.VERIFIED
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
) -> WorkspaceNavigation:
    """Present fresh data and Odoo review as one run-owned setup journey."""

    workspace_id = workspace_view.workspace_id
    purpose = workspace_view.migration_run.purpose.value
    run_kind = "test-runs" if purpose == "TEST" else "production-runs"
    fresh_home = (
        f"/projects/{workspace_view.project_id}/test-runs/"
        f"{workspace_view.migration_run_id}/fresh-data"
        if purpose == "TEST"
        else None
    )
    run_home = fresh_home or (
        f"/projects/{workspace_view.project_id}/{run_kind}/"
        f"{workspace_view.migration_run_id}/activate"
    )
    odoo_home = (
        f"/projects/{workspace_view.project_id}/runs/"
        f"{workspace_view.migration_run_id}/odoo"
        if purpose == "TEST"
        else f"/workspaces/{workspace_id}/schema"
    )
    fresh_complete = workspace_view.data_version.state.value == "FROZEN"
    source_stage = _find_stage(navigation.stages, "source")
    odoo_stage = _find_stage(navigation.stages, "odoo")
    fresh_href = fresh_home or (
        f"/workspaces/{workspace_id}/datasets#tables-ready"
        if fresh_complete
        else (
            source_stage.href
            if navigation.registered and source_stage is not None and source_stage.href
            else navigation.setup_href
        )
    )
    fresh = WorkflowStage(
        stage_id="fresh",
        number=1,
        label="Fresh data",
        href=fresh_href,
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
        href=run_home if fresh_complete and odoo_complete else None,
        status="current" if fresh_complete and odoo_complete else "locked",
        status_label=(
            "Current" if fresh_complete and odoo_complete else "Finish Odoo check first"
        ),
    )
    if template_name in {"workspace_files.html", "workspace_sources.html", "workspace_datasets.html", "workspace_derived_entities.html"}:
        viewed_stage_id = "fresh"
    elif template_name in {"workspace_schema.html", "workspace_target.html"}:
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
    return WorkspaceNavigation(
        workspace_id=workspace_id,
        migration_project_name=navigation.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=fresh_href,
        overview_href=run_home,
        overview_active=False,
        current_stage_id=current_stage_id,
        current_stage_label=next(
            stage.label for stage in stages if stage.stage_id == current_stage_id
        ),
        viewed_stage_id=viewed_stage_id,
        viewed_page_label=navigation.viewed_page_label,
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
) -> WorkspaceNavigation:
    """Collapse an application workspace into the run's review-and-load step."""

    run_home = f"/projects/{project_id}/runs/{migration_run_id}"
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
                href=run_home,
                status="complete",
                status_label="Complete",
            ),
            WorkflowStage(
                stage_id="odoo",
                number=2,
                label="Check Odoo",
                href=f"{run_home}/odoo",
                status="complete",
                status_label="Complete",
            ),
            WorkflowStage(
                stage_id="review",
                number=3,
                label="Review and load",
                href=f"/workspaces/{navigation.workspace_id}/prepare",
                status=review_status,
                status_label=review_label,
            ),
        ),
        "review",
    )
    return WorkspaceNavigation(
        workspace_id=navigation.workspace_id,
        migration_project_name=navigation.migration_project_name,
        registered=True,
        setup_active=False,
        setup_href=run_home,
        overview_href=run_home,
        overview_active=False,
        current_stage_id="review",
        current_stage_label="Review and load",
        viewed_stage_id="review",
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
) -> WorkflowStage:
    return WorkflowStage(
        stage_id=stage_id,
        number=number,
        label=label,
        href=f"/workspaces/{workspace_id}{suffix}",
        status=status,
        status_label=status_label,
        pages=pages,
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
