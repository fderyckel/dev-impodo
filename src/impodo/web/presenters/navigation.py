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
from ...projects import MigrationProject, ProjectStatus
from ...workspace_errors import WorkspaceError
from ..context import WebContext


@dataclass(frozen=True, slots=True)
class WorkflowPage:
    """One page inside a user-facing migration stage."""

    page_id: str
    label: str
    href: str
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
class ProjectNavigation:
    """Complete navigation context for one rendered project page."""

    project_id: str
    project_name: str
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
    "project_overview.html": ("", "Project overview"),
    "project_sources.html": ("source", "Check source files"),
    "project_datasets.html": ("source", "Choose tables"),
    "project_derived_entities.html": (
        "source",
        "Separate combined information",
    ),
    "project_schema.html": ("odoo", "Choose Odoo records"),
    "project_mapping.html": ("match", "Match fields"),
    "project_transformation_impact.html": (
        "match",
        "Preview rule effects",
    ),
    "project_prepare.html": ("prepare", "Start preparation"),
    "project_preparation_progress.html": (
        "prepare",
        "Preparation progress",
    ),
    "project_resolution.html": ("prepare", "Review possible duplicates"),
    "project_normalization.html": ("prepare", "Approve prepared data"),
    "project_summary.html": ("review", "Final review"),
    "project_load.html": ("load", "Load into Odoo"),
}


def build_project_navigation(
    context: WebContext,
    project: MigrationProject,
    template_name: str,
    *,
    current_path: str = "",
) -> ProjectNavigation:
    """Return one request-scoped workflow snapshot for the rendered page."""

    current_project = context.queries.get(project.project_id)
    viewed_stage_id, viewed_page_label = _TEMPLATE_LOCATION.get(
        template_name,
        ("", "Project setup"),
    )
    if current_project.status is not ProjectStatus.REGISTERED:
        stages = _locked_stages(current_project.project_id)
        return ProjectNavigation(
            project_id=current_project.project_id,
            project_name=current_project.name,
            registered=False,
            setup_active=True,
            setup_href=f"/projects/{current_project.project_id}/details",
            overview_href=None,
            overview_active=False,
            current_stage_id="setup",
            current_stage_label="Set up project",
            viewed_stage_id="",
            viewed_page_label=viewed_page_label,
            stages=stages,
        )

    project_id = current_project.project_id
    source_selection = context.queries.get_source_selection(project_id)
    source_configurations = context.queries.get_source_configurations(project_id)
    derived_plan = context.queries.get_derived_entity_plan(project_id)
    sources_confirmed = bool(current_project.source_files) and (
        len(source_configurations) == len(current_project.source_files)
        and all(item.selected_table_keys for item in source_configurations)
    )
    source_complete = source_selection is not None
    stages: list[WorkflowStage] = [
        _stage(
            project_id,
            "source",
            1,
            "Source data",
            "/sources",
            status=("complete" if source_complete else "current"),
            status_label=("Complete" if source_complete else "Current"),
            pages=(
                _page(
                    project_id,
                    "source-files",
                    "Check source files",
                    "/sources",
                    complete=sources_confirmed or source_complete,
                ),
                _page(
                    project_id,
                    "datasets",
                    "Choose tables",
                    "/datasets",
                    complete=source_complete,
                ),
                _page(
                    project_id,
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
        stages.extend(_locked_stages(project_id, after="source"))
        return _navigation(
            current_project,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
        )

    schema = context.queries.get_odoo_schema_catalog(project_id)
    governance = context.queries.get_schema_governance(project_id)
    schema_complete = schema is not None and governance is not None
    stages.append(
        _stage(
            project_id,
            "odoo",
            2,
            "Odoo data",
            "/schema",
            status=("complete" if schema_complete else "current"),
            status_label=("Complete" if schema_complete else "Current"),
            pages=(
                _page(
                    project_id,
                    "schema",
                    "Choose Odoo records",
                    "/schema",
                    complete=schema_complete,
                ),
            ),
        )
    )
    if not schema_complete:
        stages.extend(_locked_stages(project_id, after="odoo"))
        return _navigation(
            current_project,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
        )

    revision = context.queries.get_mapping_revision(project_id)
    submission = (
        context.queries.get_mapping_submission(project_id, revision.version)
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
            project_id,
            "match",
            3,
            "Match data",
            "/mapping",
            status=("complete" if mapping_complete else "current"),
            status_label=("Complete" if mapping_complete else "Current"),
            pages=(
                _page(
                    project_id,
                    "mapping",
                    "Match fields",
                    "/mapping",
                    complete=mapping_complete,
                ),
                _page(
                    project_id,
                    "transformation-impact",
                    "Preview rule effects",
                    "/mapping/transformation-impact",
                    optional=True,
                ),
            ),
        )
    )
    if not mapping_complete:
        stages.extend(_locked_stages(project_id, after="match"))
        return _navigation(
            current_project,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
        )

    active_job = (
        context.preparation_jobs.active(project_id)
        if context.preparation_jobs is not None
        else None
    )
    if active_job is not None:
        stages.append(
            _stage(
                project_id,
                "prepare",
                4,
                "Prepare data",
                "/prepare",
                status="current",
                status_label="In progress",
                pages=(
                    _page(
                        project_id,
                        "prepare",
                        "Start preparation",
                        "/prepare",
                    ),
                    WorkflowPage(
                        page_id="preparation-progress",
                        label="Preparation progress",
                        href=(
                            f"/projects/{project_id}/preparation/"
                            f"{active_job.job_id}"
                        ),
                        status="current",
                        status_label="In progress",
                    ),
                ),
            )
        )
        stages.extend(_locked_stages(project_id, after="prepare"))
        return _navigation(
            current_project,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
        )

    staging = context.preflight.current_staging(project_id)
    resolution = context.resolution.current_summary(project_id) if staging else None
    quality = context.quality.current_summary(project_id) if staging else None
    if quality is not None and quality.staging_run_id != staging.run_id:
        quality = None
    normalization = context.normalization.current_summary(project_id) if quality else None
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
            project_id,
            "prepare",
            "Start preparation",
            "/prepare",
            complete=staging is not None,
        )
    ]
    if template_name == "project_preparation_progress.html" and current_path:
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
                project_id,
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
                project_id,
                "normalization",
                "Approve prepared data",
                "/normalization",
                complete=normalization.frozen,
                attention=bool(normalization.decisions_left),
            )
        )
    stages.append(
        _stage(
            project_id,
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
        stages.extend(_locked_stages(project_id, after="prepare"))
        return _navigation(
            current_project,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
        )

    report = context.preflight.current_report(project_id)
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
            project_id,
            "review",
            5,
            "Final review",
            "/summary",
            status=review_status,
            status_label=review_label,
            pages=(
                _page(
                    project_id,
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
        stages.extend(_locked_stages(project_id, after="review"))
        return _navigation(
            current_project,
            template_name,
            viewed_stage_id,
            viewed_page_label,
            stages,
        )

    load_status = "current"
    load_label = "Current"
    try:
        preview = context.execution.current_preview(project_id)
    except (ReadinessError, WorkspaceError):
        preview = None
        load_status = "attention"
        load_label = "Needs attention"
    if preview is not None:
        if preview.snapshot.write_count == 0:
            load_status = "complete"
            load_label = "No changes needed"
        elif preview.current_run is not None:
            reconciliation = context.reconciliation.current(project_id)
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
    stages.append(
        _stage(
            project_id,
            "load",
            6,
            "Load into Odoo",
            "/load",
            status=load_status,
            status_label=load_label,
            pages=(
                _page(
                    project_id,
                    "load",
                    "Preview, load and verify",
                    "/load",
                    complete=load_status == "complete",
                    attention=load_status == "attention",
                ),
            ),
        )
    )
    return _navigation(
        current_project,
        template_name,
        viewed_stage_id,
        viewed_page_label,
        stages,
    )


def _navigation(
    project: MigrationProject,
    template_name: str,
    viewed_stage_id: str,
    viewed_page_label: str,
    stages: list[WorkflowStage],
) -> ProjectNavigation:
    current_stage = next(
        (
            stage
            for stage in stages
            if stage.status in {"current", "attention"}
        ),
        stages[-1],
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
    return ProjectNavigation(
        project_id=project.project_id,
        project_name=project.name,
        registered=True,
        setup_active=False,
        setup_href=f"/projects/{project.project_id}/details",
        overview_href=f"/projects/{project.project_id}/overview",
        overview_active=template_name == "project_overview.html",
        current_stage_id=current_stage.stage_id,
        current_stage_label=current_stage.label,
        viewed_stage_id=viewed_stage_id,
        viewed_page_label=viewed_page_label,
        stages=active_stages,
    )


def _stage(
    project_id: str,
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
        href=f"/projects/{project_id}{suffix}",
        status=status,
        status_label=status_label,
        pages=pages,
    )


def _page(
    project_id: str,
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
        href=f"/projects/{project_id}{suffix}",
        status=status,
        status_label=status_label,
        optional=optional,
    )


def _locked_stages(
    project_id: str,
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
