"""Expose bounded related-dataset authoring between source and Match data.

Layer: web route. The router parses exact joins, lookup extraction,
multi-column hierarchy, and parent/child rules, then delegates preview and
optimistic plan revisions to ``DerivedEntityWorkspaceService``. A saved plan
changes the effective datasets visible to mapping but never edits frozen
source bytes.

See ``docs/user/guides/related-tables.md`` and
``tests/integration/web/test_exact_join_workflow.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.adapters.odoo.local_stack import LocalStackError
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.structural import ExactJoinRule
from impodo.domain.workspace.derived_entities import (
    HierarchicalLookupRule,
    RelatedDatasetRule,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateError
from impodo.web.composition.target_readers import (
    _existing_catalog_model,
    _refresh_model_catalog,
)

from ..composition.page_reads import run_local_operation, run_page_read
from ..context import WebContext
from ..exact_join_forms import (
    EXACT_JOIN_FORM_FIELDS,
    exact_join_dataset_uses,
    parse_exact_join_form,
    resolve_exact_join_request,
    source_use_label,
)
from ..forms import _optional_int, _secure_form, _text
from ..presenters.common import _flash
from ..presenters.schema import _render_derived_entities
from ..request_identity import request_identity
from ..security import require_session


def build_derived_entities_router(context: WebContext) -> APIRouter:
    """Build derived-dataset preview, save, and delete routes."""

    router = APIRouter()

    @router.get(
        "/workspaces/{workspace_id}/derived-entities",
        response_class=HTMLResponse,
    )
    async def workspace_derived_entities(request: Request, workspace_id: str):
        require_session(request)
        return await run_page_read(
            _render_derived_entities, request, context, workspace_id
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/models/refresh")
    async def refresh_derived_entity_models(request: Request, workspace_id: str):
        """Load existing Odoo record types and return to the requesting form."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        actor = request_identity(request, context.identity_resolver).actor
        workspace_state = context.queries.get(workspace_id)
        try:
            catalog = await _refresh_model_catalog(
                context,
                workspace_state,
                actor=actor,
            )
        except (
            ConnectorError,
            LocalStackError,
            WorkspaceStateError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Loaded {len(catalog.models)} existing Odoo record type(s).",
        )
        return_anchor = (
            "hierarchy-extraction"
            if request.query_params.get("return_to") == "hierarchy"
            else "lookup-extraction"
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/derived-entities#{return_anchor}",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/save")
    async def save_workspace_derived_entity(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "expected_parent_version",
                "source_binding",
                "output_dataset_name",
                "target_model",
                "target_name_field",
                "external_id_namespace",
                "parent_separator",
                "blank_policy",
            },
        )
        actor = request_identity(request, context.identity_resolver).actor
        source_binding = _text(form, "source_binding")
        if "|" not in source_binding:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error="Choose a frozen source column",
                status_code=422,
            )
        source_dataset_id, source_column_key = source_binding.split("|", 1)
        try:
            workspace_state = context.queries.get(workspace_id)
            target_model = _existing_catalog_model(
                context,
                workspace_state,
                _text(form, "target_model"),
            )
            _plan, rule = context.derived_entities.save_rule(
                workspace_id,
                output_dataset_name=_text(form, "output_dataset_name"),
                source_dataset_id=source_dataset_id,
                source_column_key=source_column_key,
                target_model=target_model,
                target_name_field=_text(form, "target_name_field"),
                external_id_namespace=_text(form, "external_id_namespace"),
                parent_separator=_text(form, "parent_separator") or None,
                blank_policy=_text(form, "blank_policy"),
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Added {rule.output_dataset_name} to Match data. "
                "No Odoo records were created."
            ),
        )
        return RedirectResponse(
            (
                f"/workspaces/{workspace_id}/derived-entities"
                f"#lookup-rule-{rule.rule_id}"
            ),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/lookup/preview")
    async def preview_workspace_derived_entity(
        request: Request,
        workspace_id: str,
    ):
        form = await request.form()
        fields = {
            "csrf_token",
            "expected_parent_version",
            "source_binding",
            "output_dataset_name",
            "target_model",
            "target_name_field",
            "external_id_namespace",
            "parent_separator",
            "blank_policy",
        }
        _secure_form(request, form, fields)
        source_binding = _text(form, "source_binding")
        if "|" not in source_binding:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error="Choose a frozen source column",
                status_code=422,
            )
        source_dataset_id, source_column_key = source_binding.split("|", 1)
        try:
            workspace_state = context.queries.get(workspace_id)
            target_model = _existing_catalog_model(
                context,
                workspace_state,
                _text(form, "target_model"),
            )
            rule, preview = await run_in_threadpool(
                context.derived_entities.preview_lookup,
                workspace_id,
                output_dataset_name=_text(form, "output_dataset_name"),
                source_dataset_id=source_dataset_id,
                source_column_key=source_column_key,
                target_model=target_model,
                target_name_field=_text(form, "target_name_field"),
                external_id_namespace=_text(form, "external_id_namespace"),
                parent_separator=_text(form, "parent_separator") or None,
                blank_policy=_text(form, "blank_policy"),
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        return _render_derived_entities(
            request,
            context,
            workspace_id,
            pending_lookup={"rule": rule, "preview": preview},
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/join/preview")
    async def preview_workspace_exact_join(request: Request, workspace_id: str):
        """Review one exact enrichment over the complete accepted snapshots."""

        form = await request.form()
        _secure_form(request, form, EXACT_JOIN_FORM_FIELDS)
        try:
            submission = parse_exact_join_form(form)
        except ValueError as error:
            return await run_local_operation(
                _render_derived_entities,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )

        def review_join():
            try:
                selection = context.queries.get_source_selection(workspace_id)
                if selection is None:
                    raise WorkspaceError(
                        "Freeze source datasets before combining tables"
                    )
                plan = context.queries.get_derived_entity_plan(workspace_id)
                join_request, available = resolve_exact_join_request(
                    submission,
                    selection,
                    plan,
                )
                rule, review = context.derived_entities.preview_join(
                    workspace_id,
                    join_request,
                )
                dataset_uses = exact_join_dataset_uses(
                    submission,
                    join_request,
                    selection,
                    plan,
                )
                datasets = {
                    dataset.dataset_id: dataset for dataset in available.datasets
                }
                return _render_derived_entities(
                    request,
                    context,
                    workspace_id,
                    pending_join={
                        "rule": rule,
                        "review": review,
                        "left": datasets[rule.left_dataset_id],
                        "right": datasets[rule.right_dataset_id],
                        "left_use_label": source_use_label(
                            dataset_uses.get(rule.left_dataset_id)
                        ),
                        "right_use_label": source_use_label(
                            dataset_uses.get(rule.right_dataset_id)
                        ),
                        "expected_parent_version": (
                            plan.version if plan is not None else None
                        ),
                    },
                    join_form=submission,
                )
            except (WorkspaceError, ValueError) as error:
                return _render_derived_entities(
                    request,
                    context,
                    workspace_id,
                    error=str(error),
                    status_code=422,
                    join_form=submission,
                )

        return await run_local_operation(review_join)

    @router.post("/workspaces/{workspace_id}/derived-entities/join/save")
    async def save_workspace_exact_join(request: Request, workspace_id: str):
        """Recheck and save one versioned exact-join decision."""

        form = await request.form()
        _secure_form(request, form, EXACT_JOIN_FORM_FIELDS)
        actor = request_identity(request, context.identity_resolver).actor
        try:
            submission = parse_exact_join_form(form)
        except ValueError as error:
            return await run_local_operation(
                _render_derived_entities,
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )

        def save_join():
            try:
                selection = context.queries.get_source_selection(workspace_id)
                if selection is None:
                    raise WorkspaceError(
                        "Freeze source datasets before combining tables"
                    )
                plan = context.queries.get_derived_entity_plan(workspace_id)
                join_request, _available = resolve_exact_join_request(
                    submission,
                    selection,
                    plan,
                )
                dataset_uses = exact_join_dataset_uses(
                    submission,
                    join_request,
                    selection,
                    plan,
                )
                return context.derived_entities.save_join(
                    workspace_id,
                    join_request,
                    dataset_uses=dataset_uses,
                    expected_parent_version=submission.expected_parent_version,
                    actor=actor,
                )
            except (WorkspaceError, ValueError) as error:
                return _render_derived_entities(
                    request,
                    context,
                    workspace_id,
                    error=str(error),
                    status_code=422,
                    join_form=submission,
                )

        outcome = await run_local_operation(save_join)
        if isinstance(outcome, HTMLResponse):
            return outcome
        _plan, rule, _review = outcome
        _flash(
            request,
            (
                f"Created the combined table {rule.output_dataset_name}. "
                "The accepted source tables remain unchanged."
            ),
        )
        return RedirectResponse(
            (
                f"/workspaces/{workspace_id}/derived-entities"
                f"#join-rule-{rule.rule_id}"
            ),
            status_code=303,
        )

    hierarchy_fields = {
        "csrf_token",
        "expected_parent_version",
        "source_dataset_id",
        "hierarchy_level_1",
        "hierarchy_level_2",
        "hierarchy_level_3",
        "hierarchy_level_4",
        "hierarchy_level_5",
        "output_dataset_name",
        "target_model",
        "target_name_field",
        "external_id_namespace",
        "missing_parent_mode",
        "missing_parent_value",
        "missing_leaf",
        "all_blank_mode",
        "all_blank_value",
    }

    def hierarchy_values(form):
        levels = tuple(
            value
            for index in range(1, 6)
            for value in (_text(form, f"hierarchy_level_{index}"),)
            if value
        )
        missing_parent_mode = _text(form, "missing_parent_mode")
        all_blank_mode = _text(form, "all_blank_mode")
        return {
            "output_dataset_name": _text(form, "output_dataset_name"),
            "source_dataset_id": _text(form, "source_dataset_id"),
            "source_level_column_keys": levels,
            "target_name_field": _text(form, "target_name_field"),
            "external_id_namespace": _text(form, "external_id_namespace"),
            "missing_parent_mode": missing_parent_mode,
            "missing_parent_value": (
                _text(form, "missing_parent_value") or None
                if missing_parent_mode == "fixed"
                else None
            ),
            "missing_leaf": _text(form, "missing_leaf"),
            "all_blank_mode": all_blank_mode,
            "all_blank_value": (
                _text(form, "all_blank_value") or None
                if all_blank_mode == "fixed"
                else None
            ),
        }

    @router.post("/workspaces/{workspace_id}/derived-entities/hierarchy/preview")
    async def preview_workspace_hierarchy(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, hierarchy_fields)
        try:
            values = hierarchy_values(form)
            workspace_state = context.queries.get(workspace_id)
            target_model = _existing_catalog_model(
                context,
                workspace_state,
                _text(form, "target_model"),
            )
            rule, preview = context.derived_entities.preview_hierarchy(
                workspace_id,
                **values,
                target_model=target_model,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        return _render_derived_entities(
            request,
            context,
            workspace_id,
            pending_hierarchy={"rule": rule, "preview": preview},
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/hierarchy/save")
    async def save_workspace_hierarchy(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, hierarchy_fields)
        actor = request_identity(request, context.identity_resolver).actor
        try:
            values = hierarchy_values(form)
            workspace_state = context.queries.get(workspace_id)
            target_model = _existing_catalog_model(
                context,
                workspace_state,
                _text(form, "target_model"),
            )
            _plan, rule = context.derived_entities.save_hierarchy(
                workspace_id,
                **values,
                target_model=target_model,
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, f"Created the hierarchy table {rule.output_dataset_name}.")
        return RedirectResponse(
            (
                f"/workspaces/{workspace_id}/derived-entities"
                f"#lookup-rule-{rule.rule_id}"
            ),
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/related/preview")
    async def preview_workspace_related_datasets(
        request: Request,
        workspace_id: str,
    ):
        form = await request.form()
        fields = {
            "csrf_token",
            "expected_parent_version",
            "source_dataset_id",
            "parent_dataset_name",
            "child_dataset_name",
            "parent_key_column_key",
            "scope_column_key",
            "child_key_column_key",
            "blank_policy",
        }
        _secure_form(request, form, fields)
        try:
            rule, preview = context.derived_entities.preview_related_split(
                workspace_id,
                source_dataset_id=_text(form, "source_dataset_id"),
                parent_dataset_name=_text(form, "parent_dataset_name"),
                child_dataset_name=_text(form, "child_dataset_name"),
                parent_key_column_key=_text(form, "parent_key_column_key"),
                scope_column_key=_text(form, "scope_column_key") or None,
                child_key_column_key=_text(form, "child_key_column_key"),
                blank_policy=_text(form, "blank_policy"),
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        return _render_derived_entities(
            request,
            context,
            workspace_id,
            pending_related={"rule": rule, "preview": preview},
        )

    @router.post("/workspaces/{workspace_id}/derived-entities/related/save")
    async def save_workspace_related_datasets(
        request: Request,
        workspace_id: str,
    ):
        form = await request.form()
        fields = {
            "csrf_token",
            "expected_parent_version",
            "source_dataset_id",
            "parent_dataset_name",
            "child_dataset_name",
            "parent_key_column_key",
            "scope_column_key",
            "child_key_column_key",
            "blank_policy",
        }
        _secure_form(request, form, fields)
        actor = request_identity(request, context.identity_resolver).actor
        try:
            _plan, rule = context.derived_entities.save_related_split(
                workspace_id,
                source_dataset_id=_text(form, "source_dataset_id"),
                parent_dataset_name=_text(form, "parent_dataset_name"),
                child_dataset_name=_text(form, "child_dataset_name"),
                parent_key_column_key=_text(form, "parent_key_column_key"),
                scope_column_key=_text(form, "scope_column_key") or None,
                child_key_column_key=_text(form, "child_key_column_key"),
                blank_policy=_text(form, "blank_policy"),
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                f"Created the separate tables {rule.parent_dataset_name} and "
                f"{rule.child_dataset_name}."
            ),
        )
        return RedirectResponse(
            (
                f"/workspaces/{workspace_id}/derived-entities"
                f"#related-rule-{rule.rule_id}"
            ),
            status_code=303,
        )

    @router.post(
        "/workspaces/{workspace_id}/derived-entities/{rule_id}/delete"
    )
    async def delete_workspace_derived_entity(
        request: Request,
        workspace_id: str,
        rule_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_parent_version"},
        )
        actor = request_identity(request, context.identity_resolver).actor

        def delete_rule():
            plan = context.queries.get_derived_entity_plan(workspace_id)
            removed_rule = next(
                (
                    rule
                    for rule in (plan.rules if plan else ())
                    if rule.rule_id == rule_id
                ),
                None,
            )
            try:
                context.derived_entities.delete_rule(
                    workspace_id,
                    rule_id,
                    expected_parent_version=_optional_int(
                        _text(form, "expected_parent_version")
                    ),
                    actor=actor,
                )
            except (WorkspaceError, ValueError) as error:
                return _render_derived_entities(
                    request,
                    context,
                    workspace_id,
                    error=str(error),
                    status_code=422,
                )
            return removed_rule

        outcome = await run_local_operation(delete_rule)
        if isinstance(outcome, HTMLResponse):
            return outcome
        removed_rule = outcome
        _flash(
            request,
            (
                "Removed the saved combination."
                if isinstance(removed_rule, ExactJoinRule)
                else "Removed the saved separation rule."
            ),
        )
        return_anchor = (
            "combine-source-tables"
            if isinstance(removed_rule, ExactJoinRule)
            else "create-related-datasets"
            if isinstance(removed_rule, RelatedDatasetRule)
            else "hierarchy-extraction"
            if isinstance(removed_rule, HierarchicalLookupRule)
            else "lookup-extraction"
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/derived-entities#{return_anchor}",
            status_code=303,
        )

    return router
