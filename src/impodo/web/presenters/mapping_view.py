"""Mapping page view-model helpers."""

from __future__ import annotations

import json
from time import perf_counter

from fastapi import Request
from fastapi.responses import HTMLResponse

from ...derived_entities import (
    DerivedEntityRule,
    derived_dataset_links,
    derived_mapping_samples,
    preview_derived_entities,
    related_dataset_links,
)
from ...domain.schema.governance import BusinessKeyStatus
from ...domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...domain.source_binding import (
    FileSourceBinding,
    OdooSourceBinding,
    SourceOriginKind,
)
from ...domain.mapping.contracts import (
    MAX_CONTROL_TOTALS_PER_DATASET,
    ScalarFieldMapping,
    ScalarValueSource,
)
from ...domain.mapping.scalar_values import (
    ScalarValueError,
    evaluate_scalar_mapping_value,
)
from ...domain.mapping.validation.evidence import mapping_issue_fingerprint
from ...domain.mapping.create_field_policy import supports_create_default_capture
from ...quality import (
    MAX_MANAGER_RULES_PER_DATASET,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRuleFamily,
    manager_quality_rule,
)
from ...workspace_errors import WorkspaceError
from ..constants import DEFAULT_MAPPING_FIELDS_PER_PAGE, MAPPING_FIELD_PAGE_SIZES
from ..context import WebContext
from ..forms import (
    _positive_query_int,
    _text,
)
from .common import _render
from .mapping_forms import (
    _canonical_mapping_type,
    _related_business_keys,
    _resolver_business_key,
    _standard_reference_business_key,
)
from .mapping_impact import (
    _mapping_field_page_size,
    _mapping_return_url,
)


def _render_mapping(
    request: Request,
    context: WebContext,
    workspace_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    session_error = request.session.pop("mapping_error", None)
    if error is None and isinstance(session_error, str):
        error = session_error
    if error is None and request.query_params.get("save_error") == "request_rejected":
        error = (
            "The mapping request exceeded a safety limit or could not be read. "
            "No mapping change was saved; the last working draft is loaded."
        )
    physical_selection = context.queries.get_source_selection(workspace_id)
    preparation_plan = context.queries.get_derived_entity_plan(workspace_id)
    source_catalogs = (
        context.queries.get_source_catalogs(workspace_id)
        if physical_selection is not None
        else ()
    )
    selection = context.queries.get_mapping_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    governance = context.queries.get_schema_governance(workspace_id)
    revision = context.queries.get_mapping_revision(workspace_id)
    stored_validation = (
        context.queries.get_mapping_validation(
            workspace_id, revision.version
        )
        if revision
        else None
    )
    stored_submission = (
        context.queries.get_mapping_submission(
            workspace_id, revision.version
        )
        if revision
        else None
    )
    working_draft = context.queries.get_mapping_working_draft(workspace_id)
    odoo_pinned = bool(
        selection is not None
        and selection.datasets
        and all(
            item.origin is SourceOriginKind.ODOO
            for item in selection.datasets
        )
    )
    working_draft_is_current, active_definition = _active_mapping_state(
        selection,
        schema,
        governance,
        revision,
        working_draft,
    )
    has_unvalidated_changes = bool(
        working_draft_is_current
        and working_draft is not None
        and (
            revision is None
            or working_draft.content_hash != revision.definition.content_hash
        )
    )
    validation = None if has_unvalidated_changes else stored_validation
    submission = None if has_unvalidated_changes else stored_submission
    dataset_count = len(selection.datasets) if selection is not None else 0
    active_dataset_index = min(
        _positive_query_int(
            request.query_params.get("mapping_dataset"),
            default=0,
        ),
        max(dataset_count - 1, 0),
    )
    scalar_page = _positive_query_int(
        request.query_params.get("scalar_page"),
        default=1,
    )
    scalar_page_size = _mapping_field_page_size(
        request.query_params.get("scalar_page_size")
    )
    relation_page = _positive_query_int(
        request.query_params.get("relation_page"),
        default=1,
    )
    relation_page_size = _mapping_field_page_size(
        request.query_params.get("relation_page_size")
    )
    relation_query = request.query_params.get("relation_query", "").strip()[:128]
    field_query = request.query_params.get("field_query", "").strip()[:128]
    mapped_only = request.query_params.get("mapped_only") == "1"
    lookup_links, lookup_samples = _lookup_mapping_materials(
        physical_selection,
        preparation_plan,
        source_catalogs,
    )
    dataset_views = (
        _mapping_dataset_views(
            selection,
            schema,
            governance,
            active_definition.datasets if active_definition else (),
            source_catalogs,
            {
                index: request.query_params.get(f"target_model_{index}", "")
                for index, _item in enumerate(selection.datasets)
            },
            related_dataset_links(preparation_plan),
            lookup_links,
            lookup_samples,
            active_dataset_index=active_dataset_index,
            scalar_page=scalar_page,
            scalar_page_size=scalar_page_size,
            relation_page=relation_page,
            relation_page_size=relation_page_size,
            relation_query=relation_query,
            field_query=field_query,
            mapped_only=mapped_only,
        )
        if selection and schema
        else ()
    )
    _add_mapping_dataset_urls(request, workspace_id, dataset_views)
    readonly_field_recovery = _readonly_field_recovery(
        validation,
        selection,
        schema,
    )
    issue_views = _mapping_issue_views(
        request,
        workspace_id,
        validation,
        selection,
        schema,
        active_definition,
    )
    blocking_issue_views = tuple(
        item
        for item in issue_views
        if item["issue"].severity == "error"
        and item["issue"].code != "MAPPING_TARGET_FIELD_READONLY"
    )
    previous_check_issue_views = _mapping_issue_views(
        request,
        workspace_id,
        stored_validation if has_unvalidated_changes else None,
        selection,
        schema,
        active_definition,
    )
    previous_check_blocking_issue_views = tuple(
        item
        for item in previous_check_issue_views
        if item["issue"].severity == "error"
        and item["issue"].code != "MAPPING_TARGET_FIELD_READONLY"
    )
    warning_issues = tuple(
        {
            **item,
            "fingerprint": mapping_issue_fingerprint(item["issue"]),
        }
        for item in issue_views
        if item["issue"].severity == "warning"
    )
    visible_validation_issues = tuple(
        item
        for item in (validation.issues if validation else ())
        if item.code != "MAPPING_TARGET_FIELD_READONLY"
    )
    validation_problem_count = len(visible_validation_issues) + (
        1 if readonly_field_recovery else 0
    )
    next_step = _mapping_next_step(
        workspace_id=workspace_id,
        schema=schema,
        revision=revision,
        validation=validation,
        submission=submission,
        has_unvalidated_changes=has_unvalidated_changes,
        blocking_issue_views=blocking_issue_views,
        previous_check_blocking_issue_views=(
            previous_check_blocking_issue_views
        ),
        readonly_field_recovery=readonly_field_recovery,
    )
    quality_view = None
    if (
        revision is not None
        and not has_unvalidated_changes
        and selection is not None
        and schema is not None
        and selection.datasets
    ):
        quality_view = _quality_check_view(
            revision.definition,
            selection,
            schema,
            active_dataset_index,
            context.queries.get_current_quality_ruleset(workspace_id),
        )
    return _render(
        request,
        "mapping/page.html",
        workspace_id=workspace_id,
        workspace_state=context.queries.get(workspace_id),
        selection=selection,
        schema=schema,
        governance=governance,
        odoo_pinned=odoo_pinned,
        revision=revision,
        validation=validation,
        submission=submission,
        working_draft=working_draft,
        working_draft_is_current=working_draft_is_current,
        working_draft_is_stale=(
            working_draft is not None and not working_draft_is_current
        ),
        has_unvalidated_changes=has_unvalidated_changes,
        dataset_views=dataset_views,
        warning_issues=warning_issues,
        readonly_field_recovery=readonly_field_recovery,
        visible_validation_issues=visible_validation_issues,
        validation_problem_count=validation_problem_count,
        blocking_issue_views=blocking_issue_views,
        next_step=next_step,
        quality_view=quality_view,
        recipe_application=None,
        error=error,
        status_code=status_code,
    )


def _render_mapping_field_catalog(
    request: Request,
    context: WebContext,
    workspace_id: str,
) -> HTMLResponse:
    """Render only one active field catalogue from saved local evidence."""

    started = perf_counter()
    catalog_kind = (
        "relation"
        if request.query_params.get("catalog") == "relation"
        else "scalar"
    )
    workspace_read_started = perf_counter()
    snapshot = context.queries.get_mapping_field_catalog_snapshot(workspace_id)
    workspace_read_ms = (perf_counter() - workspace_read_started) * 1000
    physical_selection = snapshot.physical_selection
    preparation_plan = snapshot.preparation_plan
    source_catalogs = snapshot.source_catalogs
    selection = context.queries.get_mapping_source_selection(workspace_id)
    schema = snapshot.schema
    governance = snapshot.governance
    revision = snapshot.revision
    working_draft = snapshot.working_draft
    _working_draft_is_current, active_definition = _active_mapping_state(
        selection,
        schema,
        governance,
        revision,
        working_draft,
    )
    if selection is None or schema is None or not selection.datasets:
        return HTMLResponse(
            "The saved Odoo field catalogue is not available.",
            status_code=409,
        )

    dataset_count = len(selection.datasets)
    active_dataset_index = min(
        _positive_query_int(
            request.query_params.get("mapping_dataset"),
            default=0,
        ),
        dataset_count - 1,
    )
    lookup_links, lookup_samples = _lookup_mapping_materials(
        physical_selection,
        preparation_plan,
        source_catalogs,
    )
    dataset_views = _mapping_dataset_views(
        selection,
        schema,
        governance,
        active_definition.datasets if active_definition else (),
        source_catalogs,
        {
            index: request.query_params.get(f"target_model_{index}", "")
            for index, _item in enumerate(selection.datasets)
        },
        related_dataset_links(preparation_plan),
        lookup_links,
        lookup_samples,
        active_dataset_index=active_dataset_index,
        scalar_page=(
            _positive_query_int(
                request.query_params.get("scalar_page"),
                default=1,
            )
            if catalog_kind == "scalar"
            else 1
        ),
        scalar_page_size=_mapping_field_page_size(
            request.query_params.get("scalar_page_size")
        ),
        relation_page=(
            _positive_query_int(
                request.query_params.get("relation_page"),
                default=1,
            )
            if catalog_kind == "relation"
            else 1
        ),
        relation_page_size=_mapping_field_page_size(
            request.query_params.get("relation_page_size")
        ),
        relation_query=(
            request.query_params.get("relation_query", "").strip()[:128]
            if catalog_kind == "relation"
            else ""
        ),
        field_query=request.query_params.get("field_query", "").strip()[:128],
        mapped_only=request.query_params.get("mapped_only") == "1",
    )
    _add_mapping_dataset_urls(request, workspace_id, dataset_views)
    active_view = next(
        (view for view in dataset_views if view["active"]),
        None,
    )
    if active_view is None:
        return HTMLResponse(
            "The requested Odoo field catalogue is not available.",
            status_code=409,
        )

    projection_ms = (perf_counter() - started) * 1000
    view_build_ms = max(0.0, projection_ms - workspace_read_ms)
    render_started = perf_counter()
    template = request.app.state.templates.env.get_template(
        "mapping/page.html"
    )
    block_name = f"{catalog_kind}_field_catalog"
    block = template.blocks.get(block_name)
    if block is None:
        raise RuntimeError(f"Mapping {catalog_kind}-field template block is missing")
    template_context = template.new_context(
        {
            "request": request,
            "workspace_id": workspace_id,
            "dataset_index": active_view["index"],
            "view": active_view,
        }
    )
    html = "".join(block(template_context))
    render_ms = (perf_counter() - render_started) * 1000
    response = HTMLResponse(html)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Server-Timing"] = (
        f"workspace_read;dur={workspace_read_ms:.1f}, "
        f"view_build;dur={view_build_ms:.1f}, "
        f"projection;dur={projection_ms:.1f}, render;dur={render_ms:.1f}, "
        f"total;dur={(perf_counter() - started) * 1000:.1f}"
    )
    return response


def _active_mapping_state(
    selection,
    schema,
    governance,
    revision,
    working_draft,
):
    expected_schema_hash = None
    if governance is not None:
        expected_schema_hash = governance.content_hash
    elif schema is not None:
        expected_schema_hash = schema.content_hash
    working_draft_is_current = bool(
        working_draft is not None
        and selection is not None
        and expected_schema_hash is not None
        and working_draft.definition.source_selection_hash
        == selection.content_hash
        and working_draft.definition.schema_hash == expected_schema_hash
    )
    if working_draft_is_current and working_draft is not None:
        return True, working_draft.definition
    if revision is not None:
        return False, revision.definition
    return False, None


def _lookup_mapping_materials(
    physical_selection,
    preparation_plan,
    source_catalogs,
):
    lookup_links = derived_dataset_links(preparation_plan)
    lookup_samples: dict[str, dict[str, tuple[str | None, ...]]] = {}
    if preparation_plan is None or physical_selection is None:
        return lookup_links, lookup_samples
    lookup_rules = tuple(
        rule
        for rule in preparation_plan.rules
        if isinstance(rule, DerivedEntityRule)
    )
    for link, rule in zip(lookup_links, lookup_rules, strict=True):
        lookup_samples[link.derived_dataset_id] = derived_mapping_samples(
            link,
            preview_derived_entities(
                rule,
                physical_selection,
                source_catalogs,
            ),
        )
    return lookup_links, lookup_samples


def _add_mapping_dataset_urls(
    request: Request,
    workspace_id: str,
    dataset_views,
) -> None:
    for view in dataset_views:
        view["edit_url"] = _mapping_return_url(
            request,
            workspace_id,
            mapping_dataset=view["index"],
            scalar_page=1,
            relation_page=1,
            save_error=None,
        )
        if not view["active"]:
            continue
        view["scalar_page_size_options"] = tuple(
            {
                "size": size,
                "url": _mapping_return_url(
                    request,
                    workspace_id,
                    scalar_page=1,
                    scalar_page_size=(
                        None
                        if size == DEFAULT_MAPPING_FIELDS_PER_PAGE
                        else size
                    ),
                    save_error=None,
                ),
            }
            for size in MAPPING_FIELD_PAGE_SIZES
        )
        view["relation_page_size_options"] = tuple(
            {
                "size": size,
                "url": _mapping_return_url(
                    request,
                    workspace_id,
                    relation_page=1,
                    relation_page_size=(
                        None
                        if size == DEFAULT_MAPPING_FIELDS_PER_PAGE
                        else size
                    ),
                    save_error=None,
                ),
            }
            for size in MAPPING_FIELD_PAGE_SIZES
        )
        view["scalar_previous_url"] = (
            _mapping_return_url(
                request,
                workspace_id,
                scalar_page=int(view["scalar_page"]) - 1,
                save_error=None,
            )
            if int(view["scalar_page"]) > 1
            else None
        )
        view["scalar_next_url"] = (
            _mapping_return_url(
                request,
                workspace_id,
                scalar_page=int(view["scalar_page"]) + 1,
                save_error=None,
            )
            if int(view["scalar_page"]) < int(view["scalar_page_count"])
            else None
        )
        view["relation_previous_url"] = (
            _mapping_return_url(
                request,
                workspace_id,
                relation_page=int(view["relation_page"]) - 1,
                save_error=None,
            )
            if int(view["relation_page"]) > 1
            else None
        )
        view["relation_next_url"] = (
            _mapping_return_url(
                request,
                workspace_id,
                relation_page=int(view["relation_page"]) + 1,
                save_error=None,
            )
            if int(view["relation_page"]) < int(view["relation_page_count"])
            else None
        )


def _readonly_field_recovery(validation, selection, schema):
    """Group readonly write findings into one data-manager recovery action."""

    if validation is None or selection is None or schema is None:
        return None
    readonly_issues = tuple(
        item
        for item in validation.issues
        if item.code == "MAPPING_TARGET_FIELD_READONLY"
    )
    if not readonly_issues:
        return None
    model_by_name = {item.name: item for item in schema.models}
    dataset_by_id = {item.dataset_id: item for item in selection.datasets}
    entries_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for issue in readonly_issues:
        model = model_by_name.get(issue.target_model or "")
        field = next(
            (
                item
                for item in (model.fields if model is not None else ())
                if item.name == issue.target_field
            ),
            None,
        )
        dataset = dataset_by_id.get(issue.dataset_id or "")
        key = (
            issue.dataset_id or "",
            issue.target_model or "",
            issue.target_field or "",
        )
        entries_by_key[key] = {
            "dataset_label": (
                dataset.name if dataset is not None else "Source table"
            ),
            "model_label": (
                model.label if model is not None else issue.target_model or "Odoo"
            ),
            "field_label": (
                field.label if field is not None else issue.target_field or "Field"
            ),
            "target_field": issue.target_field or "",
        }
    entries = tuple(
        entries_by_key[key]
        for key in sorted(
            entries_by_key,
            key=lambda item: (
                entries_by_key[item]["dataset_label"].casefold(),
                entries_by_key[item]["model_label"].casefold(),
                entries_by_key[item]["field_label"].casefold(),
                item,
            ),
        )
    )
    return {"count": len(entries), "entries": entries}


def _mapping_issue_views(
    request,
    workspace_id,
    validation,
    selection,
    schema,
    definition,
):
    """Add business labels and direct recovery actions to validation issues."""

    if validation is None or selection is None or schema is None:
        return ()
    datasets = {
        item.dataset_id: (index, item)
        for index, item in enumerate(selection.datasets)
    }
    models = {item.name: item for item in schema.models}
    fields_by_model = {
        model.name: {field.name: field for field in model.fields}
        for model in schema.models
    }
    dispositions = {
        (dataset.dataset_id, item.target_field)
        for dataset in (definition.datasets if definition is not None else ())
        for item in dataset.target_field_dispositions
    }
    value_providers = {
        (dataset.dataset_id, item.target_field)
        for dataset in (definition.datasets if definition is not None else ())
        for item in (*dataset.fields, *dataset.relationships)
    }
    provided_target_fields = dispositions | value_providers
    views = []
    for issue in validation.issues:
        located = datasets.get(issue.dataset_id or "")
        dataset_index = located[0] if located is not None else None
        source_dataset = located[1] if located is not None else None
        model = models.get(issue.target_model or "")
        field = fields_by_model.get(issue.target_model or "", {}).get(
            issue.target_field or ""
        )
        fix_url = None
        if dataset_index is not None:
            updates = {
                "mapping_dataset": dataset_index,
                "scalar_page": 1,
                "relation_page": 1,
            }
            if field is not None and field.type in {
                "many2one",
                "many2many",
                "one2many",
            }:
                updates["relation_query"] = field.label or field.name
                updates["field_query"] = None
            elif field is not None:
                updates["field_query"] = field.label or field.name
                updates["relation_query"] = None
            fix_url = (
                f"{_mapping_return_url(request, workspace_id, **updates)}"
                f"#mapping-dataset-{dataset_index}"
            )
        views.append(
            {
                "issue": issue,
                "dataset_index": dataset_index,
                "dataset_label": (
                    source_dataset.name
                    if source_dataset is not None
                    else "Source table"
                ),
                "model_label": (
                    model.label
                    if model is not None
                    else issue.target_model or "Odoo"
                ),
                "field_label": (
                    field.label
                    if field is not None
                    else issue.target_field or "Field"
                ),
                "field_type": field.type if field is not None else "",
                "fix_url": fix_url,
                "can_choose_default": (
                    issue.code == "MAPPING_ODOO_DEFAULT_AVAILABLE"
                    and dataset_index is not None
                    and field is not None
                ),
                "can_check_default": (
                    issue.code == "MAPPING_REQUIRED_FIELD_UNMAPPED"
                    and dataset_index is not None
                    and field is not None
                    and supports_create_default_capture(field)
                ),
                "default_value_label": (
                    _odoo_default_value_label(field)
                    if field is not None and field.create_default_present
                    else ""
                ),
                "can_choose_managed": (
                    issue.code == "MAPPING_REQUIRED_FIELD_UNMAPPED"
                    and dataset_index is not None
                    and field is not None
                    and (
                        field.type in {"one2many", "many2many"}
                        or field.computed is True
                        or field.related is True
                    )
                ),
                "has_value_provider": (
                    issue.dataset_id,
                    issue.target_field,
                )
                in provided_target_fields,
            }
        )
    return tuple(views)


def _mapping_next_step(
    *,
    workspace_id,
    schema,
    revision,
    validation,
    submission,
    has_unvalidated_changes,
    blocking_issue_views,
    previous_check_blocking_issue_views,
    readonly_field_recovery,
):
    """Return one visible next action and every reason it is unavailable."""

    if submission is not None:
        return {
            "label": "Continue to Prepare data",
            "available": True,
            "kind": "link",
            "href": f"/workspaces/{workspace_id}/prepare",
            "button_style": "secondary",
            "blockers": (),
            "previous_check_items": (),
        }
    blockers = []
    if has_unvalidated_changes:
        blockers.append(
            {
                "title": "Saved changes have not been checked yet",
                "message": "Check the current matches before confirming them.",
                "action_label": "Check matches",
                "action": "draft",
            }
        )
        previous_check_items = previous_check_blocking_issue_views
    else:
        previous_check_items = ()
        if schema is not None and schema.origin.value == "LOCAL_MANUAL":
            blockers.append(
                {
                    "title": "Odoo fields have not been verified",
                    "message": (
                        "Refresh the selected fields from Odoo before confirming."
                    ),
                }
            )
        if revision is None or validation is None:
            blockers.append(
                {
                    "title": "Matches have not been checked",
                    "message": "Check the current matches before confirming them.",
                    "action_label": "Check matches",
                    "action": "draft",
                }
            )
        elif validation.status.value == "INVALID":
            if readonly_field_recovery is not None:
                blockers.append(
                    {
                        "title": (
                            f"Odoo manages {readonly_field_recovery['count']} "
                            "selected field"
                            f"{'s' if readonly_field_recovery['count'] != 1 else ''}"
                        ),
                        "message": (
                            "Remove these write matches before confirming."
                        ),
                        "action_label": (
                            "Remove this field match"
                            if readonly_field_recovery["count"] == 1
                            else (
                                "Remove "
                                f"{readonly_field_recovery['count']} field matches"
                            )
                        ),
                        "action": "remove_readonly",
                        "readonly_recovery": readonly_field_recovery,
                    }
                )
            default_reviews = tuple(
                item
                for item in blocking_issue_views
                if item["issue"].code == "MAPPING_ODOO_DEFAULT_AVAILABLE"
            )
            if default_reviews:
                blockers.append(
                    {
                        "title": (
                            f"Review {len(default_reviews)} Odoo default"
                            f"{'s' if len(default_reviews) != 1 else ''}"
                        ),
                        "message": (
                            "Odoo returned these create values for this exact "
                            "target and company context. Confirm them together "
                            "or match the fields yourself."
                        ),
                        "action_label": (
                            f"Use {len(default_reviews)} Odoo default"
                            f"{'s' if len(default_reviews) != 1 else ''}"
                        ),
                        "action": "confirm_defaults",
                        "default_reviews": default_reviews,
                    }
                )
            default_checks = tuple(
                item
                for item in blocking_issue_views
                if item["can_check_default"]
            )
            if default_checks:
                blockers.append(
                    {
                        "title": (
                            f"Let Odoo decide for {len(default_checks)} required "
                            f"field{'s' if len(default_checks) != 1 else ''}"
                        ),
                        "message": (
                            "Impodo will check the defaults read-only for this "
                            "exact Odoo target and company context. During loading, "
                            "Impodo leaves these fields out so Odoo applies those "
                            "defaults."
                        ),
                        "action_label": "Let Odoo decide",
                        "action": "refresh_defaults",
                        "default_checks": default_checks,
                    }
                )
            blockers.extend(
                {
                    "title": (
                        f"{item['dataset_label']}: {item['field_label']} "
                        "needs attention"
                    ),
                    "message": item["issue"].message,
                    "issue_view": item,
                }
                for item in blocking_issue_views
                if item not in default_reviews and item not in default_checks
            )
    return {
        "label": "Confirm field matches",
        "available": not blockers,
        "kind": "submit",
        "action": "submit",
        "button_style": "primary",
        "blockers": tuple(blockers),
        "previous_check_items": tuple(previous_check_items),
    }


def _odoo_default_value_label(field) -> str:
    """Render one verified scalar default in business-readable form."""

    value = field.create_default_value
    if field.type == "selection":
        label = next(
            (
                str(choice_label)
                for code, choice_label in field.selection
                if str(code) == str(value)
            ),
            str(value),
        )
        return f"{label} ({value})"
    if field.type == "boolean":
        return "Yes" if value else "No"
    return str(value)


def _mapping_dataset_views(
    selection,
    schema,
    governance,
    existing_datasets,
    source_catalogs=(),
    selected_models=None,
    related_links=(),
    derived_links=(),
    prepared_source_samples=None,
    *,
    active_dataset_index=None,
    scalar_page=1,
    scalar_page_size=DEFAULT_MAPPING_FIELDS_PER_PAGE,
    relation_page=1,
    relation_page_size=DEFAULT_MAPPING_FIELDS_PER_PAGE,
    relation_query="",
    field_query="",
    mapped_only=False,
) -> tuple[dict[str, object], ...]:
    existing_by_id = {item.dataset_id: item for item in existing_datasets}
    models = {item.name: item for item in schema.models}
    keys = tuple(governance.business_keys) if governance else ()
    confirmed = tuple(
        item
        for item in keys
        if item.status is BusinessKeyStatus.CONFIRMED
    )
    link_by_child = {item.child_dataset_id: item for item in related_links}
    link_by_parent = {item.parent_dataset_id: item for item in related_links}
    parent_ids = {item.parent_dataset_id for item in related_links}
    derived_by_dataset = {
        item.derived_dataset_id: item for item in derived_links
    }
    derived_by_consumer: dict[str, list[object]] = {}
    for item in derived_links:
        derived_by_consumer.setdefault(item.consumer_dataset_id, []).append(item)
    prepared_source_samples = prepared_source_samples or {}

    def selected_mapping_model_name(dataset_index, source_dataset) -> str:
        if isinstance(source_dataset.source, OdooSourceBinding):
            return source_dataset.source.model
        existing = existing_by_id.get(source_dataset.dataset_id)
        derived_link = derived_by_dataset.get(source_dataset.dataset_id)
        selected_override = (
            selected_models.get(dataset_index, "")
            if selected_models is not None
            else ""
        )
        if selected_override in models:
            return selected_override
        if existing and existing.target_model in models:
            return existing.target_model
        if (
            derived_link is not None
            and derived_link.target_model in models
        ):
            return derived_link.target_model
        return next(
            (
                item.model
                for item in confirmed
                if item.model in models
            ),
            schema.models[0].name if schema.models else "",
        )

    selected_model_by_dataset = {
        source_dataset.dataset_id: selected_mapping_model_name(
            dataset_index,
            source_dataset,
        )
        for dataset_index, source_dataset in enumerate(selection.datasets)
    }
    result: list[dict[str, object]] = []
    for dataset_index, source_dataset in enumerate(selection.datasets):
        odoo_pinned = source_dataset.origin is SourceOriginKind.ODOO
        active = (
            active_dataset_index is None
            or dataset_index == active_dataset_index
        )
        source_samples = prepared_source_samples.get(
            source_dataset.dataset_id,
            _mapping_source_samples(source_dataset, source_catalogs),
        )
        captured_field_names = {
            column.source_name for column in source_dataset.columns
        }
        existing = existing_by_id.get(source_dataset.dataset_id)
        derived_link = derived_by_dataset.get(source_dataset.dataset_id)
        selected_model_name = selected_model_by_dataset[
            source_dataset.dataset_id
        ]
        model = models.get(selected_model_name)
        model_keys = tuple(
            item for item in confirmed if item.model == selected_model_name
        )
        matching_rule_labels = _matching_rule_labels(model_keys, models)
        existing_identity_fields = tuple(
            target
            for component in (
                (*existing.target_identity, *existing.target_scope)
                if existing
                else ()
            )
            for target in component.target_fields
        )
        selected_key = next(
            (
                item
                for item in model_keys
                if (*item.key_fields, *item.scope_fields)
                == existing_identity_fields
            ),
            model_keys[0] if model_keys else None,
        )
        field_by_name = (
            {item.name: item for item in model.fields} if model else {}
        )
        existing_components = {
            item.target_fields[0]: item
            for item in (
                (*existing.target_identity, *existing.target_scope)
                if existing
                else ()
            )
            if len(item.target_fields) == 1
        }
        identity_rows: list[dict[str, object]] = []
        if selected_key is not None:
            for target_field in (
                *selected_key.key_fields,
                *selected_key.scope_fields,
            ):
                metadata = field_by_name.get(target_field)
                component = existing_components.get(target_field)
                related_keys = _related_business_keys(
                    confirmed,
                    metadata.relation if metadata is not None else None,
                    include_supporting_name=(
                        metadata is not None and metadata.type == "many2one"
                    ),
                )
                standard_related_key = _standard_reference_business_key(
                    metadata.relation if metadata is not None else None
                )
                selected_related_key = _resolver_business_key(
                    component.resolver if component else None,
                    related_keys,
                )
                identity_rows.append(
                    {
                        "target_field": target_field,
                        "scope": target_field in selected_key.scope_fields,
                        "metadata": metadata,
                        "relational": (
                            metadata is not None
                            and metadata.type == "many2one"
                        ),
                        "selected_sources": (
                            component.source_column_keys
                            if component
                            else (
                                (derived_link.name_column_key,)
                                if derived_link is not None
                                and target_field
                                == derived_link.target_name_field
                                else ()
                            )
                        ),
                        "related_keys": related_keys,
                        "related_key_labels": _matching_rule_labels(
                            related_keys,
                            models,
                        ),
                        "selected_related_key": selected_related_key,
                        "recommended_related_key_id": (
                            standard_related_key.key_id
                            if standard_related_key is not None
                            and standard_related_key in related_keys
                            else ""
                        ),
                    }
                )
        identity_targets = {
            row["target_field"] for row in identity_rows
        }
        scalar_by_target = (
            {item.target_field: item for item in existing.fields}
            if existing
            else {}
        )
        disposition_by_target = (
            {
                item.target_field: item
                for item in existing.target_field_dispositions
            }
            if existing
            else {}
        )
        schema_scalar_fields = tuple(
            field
            for field in (model.fields if model else ())
            if field.type not in {"many2one", "many2many", "one2many"}
        )
        indexed_scalar_fields = tuple(
            (field_index, field)
            for field_index, field in enumerate(schema_scalar_fields)
            if (
                not field.readonly
                or (
                    field.name in scalar_by_target
                    and scalar_by_target[field.name].validate_only
                )
            )
        )
        all_scalar_fields = tuple(
            field for _index, field in indexed_scalar_fields
        )
        numeric_fields = tuple(
            field
            for field in all_scalar_fields
            if field.type in {"integer", "float", "monetary"}
        )
        existing_controls = existing.effective_control_totals if existing else ()
        existing_control_ids = (
            tuple(item.control_id for item in existing.control_definitions)
            if existing
            else ()
        )
        control_total_slots = tuple(
            {
                "index": index,
                "control_id": (
                    existing_control_ids[index]
                    if index < len(existing_control_ids)
                    else ""
                ),
                "control": (
                    existing_controls[index]
                    if index < len(existing_controls)
                    else None
                ),
            }
            for index in range(MAX_CONTROL_TOTALS_PER_DATASET)
        )
        scalar_candidates = tuple(
            (field_index, field)
            for field_index, field in indexed_scalar_fields
            if field.name not in identity_targets
        )
        normalized_query = field_query.casefold()
        matching_scalars = tuple(
            (field_index, field)
            for field_index, field in scalar_candidates
            if (
                not normalized_query
                or normalized_query
                in f"{field.label} {field.name} {field.type}".casefold()
            )
            and (
                not mapped_only
                or field.name in scalar_by_target
                or field.name in disposition_by_target
            )
        )
        scalar_page_count = max(
            1,
            (len(matching_scalars) + scalar_page_size - 1)
            // scalar_page_size,
        )
        current_scalar_page = min(max(scalar_page, 1), scalar_page_count)
        scalar_start = (current_scalar_page - 1) * scalar_page_size
        visible_scalars = (
            matching_scalars[
                scalar_start : scalar_start + scalar_page_size
            ]
            if active
            else ()
        )
        scalar_rows = tuple(
            {
                "index": field_index,
                "metadata": field,
                "mapping": scalar_by_target.get(field.name),
                "disposition": disposition_by_target.get(field.name),
                "value_mappings_json": _value_mappings_json(
                    scalar_by_target[field.name].value_mappings
                    if field.name in scalar_by_target
                    else ()
                ),
                "selection_rules_json": _selection_rules_json(
                    scalar_by_target[field.name].selection_rules
                    if field.name in scalar_by_target
                    else None
                ),
                "matched_choice_count": len(
                    scalar_by_target[field.name].value_mappings
                    if field.name in scalar_by_target
                    else ()
                ),
                "text_steps_json": _text_steps_json(
                    scalar_by_target[field.name].transform.text_steps
                    if field.name in scalar_by_target
                    else ()
                ),
                "show_phone_cleanup_quick_start": _is_phone_field(field),
                "selection_choice_count": (
                    len(field.selection)
                    if field.type == "selection"
                    else 0
                ),
                "literal_selection_available": (
                    field.name not in scalar_by_target
                    or scalar_by_target[field.name].literal_value is None
                    or str(scalar_by_target[field.name].literal_value)
                    in {str(value) for value, _label in field.selection}
                ),
                "canonical_type": _canonical_mapping_type(field.type),
                "source_samples": source_samples,
                "recommended_source_column": (
                    derived_link.name_column_key
                    if derived_link is not None
                    and field.name == derived_link.target_name_field
                    and field.name not in scalar_by_target
                    else None
                ),
                "preview": _scalar_mapping_preview(
                    scalar_by_target.get(field.name),
                    source_samples,
                    source_dataset.columns,
                ),
                "write_eligible": _odoo_pinned_write_eligible(field),
                "write_baseline_captured": field.name in captured_field_names,
                "write_approved": bool(
                    existing is not None
                    and field.name in existing.approved_write_fields
                ),
                "write_block_reason": _odoo_pinned_write_block_reason(
                    field,
                    baseline_captured=field.name in captured_field_names,
                ),
            }
            for field_index, field in visible_scalars
        )
        relation_by_target = (
            {item.target_field: item for item in existing.relationships}
            if existing
            else {}
        )
        schema_relation_fields = tuple(
            field
            for field in (model.fields if model else ())
            if field.type in {"many2one", "many2many", "one2many"}
        )
        indexed_relation_fields = tuple(
            (relation_index, field)
            for relation_index, field in enumerate(schema_relation_fields)
            if (
                not field.readonly
                or (
                    field.name in relation_by_target
                    and relation_by_target[field.name].validate_only
                )
            )
        )
        all_relation_fields = tuple(
            field for _index, field in indexed_relation_fields
        )
        relation_recommendations: dict[str, dict[str, object]] = {}
        related_link = link_by_child.get(source_dataset.dataset_id)
        if related_link is not None:
            parent_model = selected_model_by_dataset.get(
                related_link.parent_dataset_id
            )
            for field in all_relation_fields:
                if (
                    field.name not in identity_targets
                    and field.type in {"many2one", "many2many"}
                    and field.relation == parent_model
                ):
                    relation_recommendations[field.name] = {
                        "dataset_id": related_link.parent_dataset_id,
                        "source_columns": related_link.reference_column_keys,
                    }
        for link in derived_by_consumer.get(source_dataset.dataset_id, ()):
            derived_model = selected_model_by_dataset.get(link.derived_dataset_id)
            matches = tuple(
                field
                for field in all_relation_fields
                if field.name not in identity_targets
                and field.type == "many2one"
                and field.relation == derived_model
            )
            if len(matches) == 1:
                relation_recommendations[matches[0].name] = {
                    "dataset_id": link.derived_dataset_id,
                    "source_columns": (link.source_column_key,),
                    "kind": "extracted_lookup",
                }
        normalized_relation_query = relation_query.casefold()
        relation_candidates = sorted(
            (
                (relation_index, field)
                for relation_index, field in indexed_relation_fields
                if field.name not in identity_targets
                and (
                    not normalized_relation_query
                    or normalized_relation_query
                    in (
                        f"{field.label} {field.name} {field.type} "
                        f"{field.relation or ''}"
                    ).casefold()
                )
            ) if not odoo_pinned else (),
            key=lambda item: (
                0
                if (
                    item[1].name in relation_by_target
                    or item[1].name in disposition_by_target
                )
                else (
                    1
                    if item[1].name in relation_recommendations
                    else 2
                ),
                item[1].label.casefold(),
                item[1].name,
            ),
        )
        relation_page_count = max(
            1,
            (len(relation_candidates) + relation_page_size - 1)
            // relation_page_size,
        )
        current_relation_page = min(max(relation_page, 1), relation_page_count)
        relation_start = (current_relation_page - 1) * relation_page_size
        visible_relations = (
            relation_candidates[
                relation_start : relation_start + relation_page_size
            ]
            if active
            else ()
        )
        relation_rows: list[dict[str, object]] = []
        for relation_index, field in visible_relations:
            mapping = relation_by_target.get(field.name)
            recommendation = relation_recommendations.get(field.name)
            related_keys = _related_business_keys(
                confirmed,
                field.relation,
                include_supporting_name=field.type == "many2one",
            )
            standard_related_key = _standard_reference_business_key(
                field.relation
            )
            row: dict[str, object] = {
                "index": relation_index,
                "metadata": field,
                "mapping": mapping,
                "disposition": disposition_by_target.get(field.name),
                "related_keys": related_keys,
                "related_key_labels": _matching_rule_labels(
                    related_keys,
                    models,
                ),
                "selected_key": _resolver_business_key(
                    mapping.resolver if mapping else None,
                    related_keys,
                ),
                "recommended_key_id": (
                    standard_related_key.key_id
                    if standard_related_key is not None
                    and standard_related_key in related_keys
                    else ""
                ),
                "value_mappings_json": _value_mappings_json(
                    mapping.resolver.value_mappings if mapping else ()
                ),
                "matched_choice_count": len(
                    mapping.resolver.value_mappings if mapping else ()
                ),
            }
            if recommendation is not None:
                row["recommended_dataset_id"] = recommendation["dataset_id"]
                row["recommended_source_columns"] = recommendation[
                    "source_columns"
                ]
                if recommendation.get("kind"):
                    row["recommendation_kind"] = recommendation["kind"]
            relation_rows.append(row)
        result.append(
            {
                "index": dataset_index,
                "active": active,
                "source": source_dataset,
                "source_by_key": {
                    item.stable_key: item for item in source_dataset.columns
                },
                "source_samples": source_samples,
                "mapping": existing,
                "odoo_pinned": odoo_pinned,
                "approved_write_fields": (
                    existing.approved_write_fields if existing else ()
                ),
                "target_field_dispositions": (
                    existing.target_field_dispositions if existing else ()
                ),
                "selected_model": selected_model_name,
                "model": model,
                "models": schema.models,
                "business_keys": model_keys,
                "matching_rule_labels": matching_rule_labels,
                "selected_key": selected_key,
                "identity_rows": tuple(identity_rows),
                "scalar_rows": scalar_rows,
                "numeric_fields": numeric_fields,
                "control_total_slots": control_total_slots,
                "scalar_catalog_total": len(scalar_candidates),
                "scalar_matching_total": len(matching_scalars),
                "scalar_mapped_total": sum(
                    field.name in scalar_by_target
                    or field.name in disposition_by_target
                    for field in all_scalar_fields
                ),
                "scalar_page": current_scalar_page,
                "scalar_page_count": scalar_page_count,
                "scalar_page_size": scalar_page_size,
                "relation_rows": tuple(relation_rows),
                "relation_catalog_total": len(all_relation_fields),
                "relation_matching_total": len(relation_candidates),
                "relation_mapped_total": sum(
                    field.name in relation_by_target
                    or field.name in disposition_by_target
                    for field in all_relation_fields
                ),
                "relation_page": current_relation_page,
                "relation_page_count": relation_page_count,
                "relation_page_size": relation_page_size,
                "relation_query": relation_query,
                "field_query": field_query,
                "mapped_only": mapped_only,
                "other_datasets": tuple(
                    item
                    for item in selection.datasets
                    if item.dataset_id != source_dataset.dataset_id
                ),
                "related_role": (
                    "lookup"
                    if source_dataset.dataset_id in derived_by_dataset
                    else (
                        "child"
                        if source_dataset.dataset_id in link_by_child
                        else (
                            "parent"
                            if source_dataset.dataset_id in parent_ids
                            else None
                        )
                    )
                ),
                "recommended_source_identity": (
                    derived_link.canonical_key_column_key,
                )
                if derived_link is not None
                else (
                    link_by_child[
                        source_dataset.dataset_id
                    ].child_identity_column_keys
                    if source_dataset.dataset_id in link_by_child
                    else (
                        link_by_parent[
                            source_dataset.dataset_id
                        ].reference_column_keys
                        if source_dataset.dataset_id in link_by_parent
                        else ()
                    )
                ),
            }
        )
    return tuple(result)


def _odoo_pinned_write_eligible(field) -> bool:
    """Mirror the domain Tier-1 gate for an explanatory browser view only."""

    return bool(
        field.type in CURRENT_ODOO_SOURCE_POLICY.writable_field_types
        and field.stored is True
        and field.readonly is False
        and field.computed is False
        and field.related is False
        and field.translated is False
        and field.company_dependent is False
    )


def _odoo_pinned_write_block_reason(
    field,
    *,
    baseline_captured: bool,
) -> str:
    if not baseline_captured:
        return "Capture this field first so Impodo has its original value."
    if _odoo_pinned_write_eligible(field):
        return ""
    return (
        "This field is outside the current safe update policy; it remains "
        "available only as source context."
    )


def _matching_rule_labels(keys, models) -> dict[str, str]:
    """Present confirmed rules through their governed Odoo field labels."""

    labels: dict[str, str] = {}
    for key in keys:
        if key.description:
            labels[key.key_id] = key.description
            continue
        model = models.get(key.model)
        field_labels = (
            {field.name: field.label for field in model.fields}
            if model is not None
            else {}
        )
        key_label = " + ".join(
            field_labels.get(field_name, field_name)
            for field_name in key.key_fields
        )
        scope_label = " + ".join(
            field_labels.get(field_name, field_name)
            for field_name in key.scope_fields
        )
        labels[key.key_id] = (
            f"{key_label} within {scope_label}"
            if scope_label
            else key_label
        )
    return labels


def _quality_check_view(
    definition,
    selection,
    schema,
    dataset_index,
    current_ruleset,
) -> dict[str, object] | None:
    source = selection.datasets[dataset_index]
    mapping = next(
        (
            item
            for item in definition.datasets
            if item.dataset_id == source.dataset_id
        ),
        None,
    )
    if mapping is None:
        return None
    model = next(
        (item for item in schema.models if item.name == mapping.target_model),
        None,
    )
    if model is None:
        return None
    mapped = {item.target_field for item in mapping.fields}
    field_choices = tuple(
        {"name": field.name, "label": field.label or field.name}
        for field in model.fields
        if field.name in mapped
        and field.type not in {"many2one", "many2many", "one2many"}
    )
    rules_current = bool(
        current_ruleset is not None
        and current_ruleset.mapping_hash == definition.content_hash
        and current_ruleset.schema_hash == definition.schema_hash
    )
    existing = (
        tuple(
            item
            for item in current_ruleset.manager_rules
            if item.dataset == source.name
        )
        if rules_current
        else ()
    )
    return {
        "dataset_id": source.dataset_id,
        "dataset_name": source.name,
        "field_choices": field_choices,
        "slots": tuple(
            {
                "index": index,
                "rule": existing[index] if index < len(existing) else None,
            }
            for index in range(MAX_MANAGER_RULES_PER_DATASET)
        ),
        "automatic_checks": (
            "Required values",
            "Valid types, formats and choices",
            "Known lookup values",
            "Ready linked records",
            "Duplicate Odoo matches after preparation",
        ),
        "rules_current": rules_current,
    }


def _manager_quality_rules_from_form(
    form,
    *,
    workspace_id: str,
    dataset: str,
    allowed_fields: set[str],
):
    rules = []
    allowed_families = {
        QualityRuleFamily.REQUIRED_IF,
        QualityRuleFamily.EXACTLY_ONE_OF,
        QualityRuleFamily.ORDERED_COMPARISON,
        QualityRuleFamily.EQUALITY,
        QualityRuleFamily.INEQUALITY,
    }
    for index in range(MAX_MANAGER_RULES_PER_DATASET):
        name = _text(form, f"quality_name_{index}")
        family_value = _text(form, f"quality_family_{index}")
        field_a = _text(form, f"quality_field_a_{index}")
        field_b = _text(form, f"quality_field_b_{index}")
        equals = _text(form, f"quality_equals_{index}")
        outcome_value = _text(form, f"quality_outcome_{index}")
        owner_value = _text(form, f"quality_owner_{index}")
        if not any(
            (
                name,
                family_value,
                field_a,
                field_b,
                equals,
                outcome_value,
                owner_value,
            )
        ):
            continue
        if not name or len(name) > 80:
            raise WorkspaceError("Give each optional data check a short name")
        try:
            family = QualityRuleFamily(family_value)
        except ValueError as error:
            raise WorkspaceError(
                "Choose a supported business data check"
            ) from error
        if family not in allowed_families:
            raise WorkspaceError("Choose a supported business data check")
        if field_a not in allowed_fields or field_b not in allowed_fields:
            raise WorkspaceError("Choose two currently mapped Odoo fields")
        if field_a == field_b:
            raise WorkspaceError(
                "Choose two different fields for a business check"
            )
        if family is QualityRuleFamily.REQUIRED_IF and not equals:
            raise WorkspaceError(
                "Enter the condition value for the required-field check"
            )
        try:
            outcome = QualityOutcomePolicy(outcome_value)
        except ValueError as error:
            raise WorkspaceError(
                "Choose what should happen when a check fails"
            ) from error
        if outcome not in {
            QualityOutcomePolicy.WARNING,
            QualityOutcomePolicy.QUARANTINE,
            QualityOutcomePolicy.BLOCK,
        }:
            raise WorkspaceError(
                "Choose a supported outcome for the data check"
            )
        try:
            owner = QualityOwnerRole(owner_value)
        except ValueError as error:
            raise WorkspaceError(
                "Choose who should review the data check"
            ) from error
        rules.append(
            manager_quality_rule(
                workspace_id=workspace_id,
                dataset=dataset,
                family=family,
                name=name,
                input_fields=(field_a, field_b),
                parameters=(
                    {"equals": equals}
                    if family is QualityRuleFamily.REQUIRED_IF
                    else {}
                ),
                outcome=outcome,
                owner_role=owner,
            )
        )
    if len({item.rule_id for item in rules}) != len(rules):
        raise WorkspaceError(
            "Optional data checks must have unique names and choices"
        )
    return tuple(rules)


def _value_mappings_json(mappings) -> str:
    return json.dumps(
        [
            {
                "source_value": item.source_value,
                "target_value": item.target_value,
            }
            for item in mappings
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _selection_rules_json(rule_set) -> str:
    if rule_set is None:
        return json.dumps(
            {"rules": [], "otherwise_value": None},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return json.dumps(
        {
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "conditions": [
                        {
                            "condition_id": condition.condition_id,
                            "source_column_key": condition.source_column_key,
                            "operator": condition.operator.value,
                            "comparison_value": condition.comparison_value,
                            "value_type": condition.value_type,
                        }
                        for condition in rule.conditions
                    ],
                    "target_value": rule.target_value,
                    "join": rule.join.value,
                }
                for rule in rule_set.rules
            ],
            "otherwise_value": rule_set.otherwise_value,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _text_steps_json(steps) -> str:
    return json.dumps(
        [
            {
                "kind": item.kind,
                "search_value": item.search_value,
                "replacement_value": item.replacement_value,
                "search_mode": item.search_mode,
                "replace_all": item.replace_all,
                "characters": item.characters,
            }
            for item in steps
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _is_phone_field(field) -> bool:
    """Return whether a scalar field should show the phone quick start."""

    if field.type not in {"char", "text"}:
        return False
    searchable = f"{field.name} {field.label}".casefold()
    return any(
        word in searchable
        for word in ("phone", "mobile", "telephone", "téléphone")
    )


def _mapping_source_samples(
    source_dataset,
    source_catalogs,
) -> dict[str, tuple[str | None, ...]]:
    if not isinstance(source_dataset.source, FileSourceBinding):
        return {}
    binding = source_dataset.source
    catalog = next(
        (
            item
            for item in source_catalogs
            if item.file_id == binding.file_id
            and item.source_sha256 == binding.source_sha256
            and item.content_hash == binding.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog is not None else ())
            if item.table_key == binding.table_key
        ),
        None,
    )
    if table is None:
        return {}
    result: dict[str, tuple[str | None, ...]] = {}
    for column in source_dataset.columns:
        values = tuple(
            row[column.ordinal - 1]
            for row in table.preview_rows
            if column.ordinal > 0 and column.ordinal <= len(row)
        )
        result[column.stable_key] = values[:3]
    return result


def _scalar_mapping_preview(
    mapping: ScalarFieldMapping | None,
    source_samples: dict[str, tuple[str | None, ...]],
    source_columns=(),
) -> dict[str, str] | None:
    if mapping is None:
        return None
    if mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
        return {
            "raw": "Not sent",
            "proposed": "Odoo runtime default",
            "status": "deferred",
        }
    raw: object = None
    if mapping.value_source is ScalarValueSource.CONSTANT:
        raw = mapping.literal_value
    elif (
        mapping.value_source is ScalarValueSource.CONDITIONAL_RULES
        and mapping.selection_rules is not None
    ):
        source_count = len(
            {
                condition.source_column_key
                for rule in mapping.selection_rules.rules
                for condition in rule.conditions
            }
        )
        raw = f"{source_count} source column(s)"
    elif mapping.source_column_key:
        samples = source_samples.get(mapping.source_column_key, ())
        raw = samples[0] if samples else None
    try:
        proposed = evaluate_scalar_mapping_value(
            mapping,
            raw,
            source_values_by_ordinal={
                column.ordinal: (
                    source_samples.get(column.stable_key, (None,))[0]
                    if source_samples.get(column.stable_key)
                    else None
                )
                for column in source_columns
            },
            source_values_by_key={
                column.stable_key: (
                    source_samples.get(column.stable_key, (None,))[0]
                    if source_samples.get(column.stable_key)
                    else None
                )
                for column in source_columns
            },
        )
    except ScalarValueError as error:
        return {
            "raw": _display_mapping_value(raw),
            "proposed": str(error),
            "status": "error",
        }
    return {
        "raw": _display_mapping_value(raw),
        "proposed": _display_mapping_value(proposed),
        "status": "ok",
    }


def _display_mapping_value(value: object) -> str:
    if value is None:
        return "∅"
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _safe_spreadsheet_text(value: object) -> object:
    """Prevent CSV values from becoming formulas when opened in Excel."""

    if not isinstance(value, str):
        return value
    if value.lstrip("\t\r\n").startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value
