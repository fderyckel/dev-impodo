"""Expose bounded related-dataset authoring between Stages B and D.

Layer: web route. The router parses lookup-extraction and parent/child rules,
then delegates preview and optimistic plan revisions to
``DerivedEntityWorkspaceService``. A saved plan changes the effective datasets
visible to mapping but never edits frozen source bytes.

See ``docs/operations/08-related-dataset-authoring.md`` and
``tests/test_web_app.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...connectors import ConnectorError
from ...local_stack import LocalStackError
from ...projects import ProjectError
from ...secrets import SecretStoreError
from ...workspace_errors import WorkspaceError
from ..context import WebContext
from ..forms import _optional_int, _secure_form, _text
from ..presenters.common import _flash
from ..presenters.schema import _render_derived_entities
from ..security import require_session
from ..target_readers import _existing_catalog_model, _refresh_model_catalog


def build_derived_entities_router(context: WebContext) -> APIRouter:
    """Build derived-dataset preview, save, and delete routes."""

    router = APIRouter()

    @router.get(
        "/projects/{project_id}/derived-entities",
        response_class=HTMLResponse,
    )
    async def project_derived_entities(request: Request, project_id: str):
        require_session(request)
        return _render_derived_entities(request, context, project_id)

    @router.post("/projects/{project_id}/derived-entities/models/refresh")
    async def refresh_derived_entity_models(request: Request, project_id: str):
        """Load existing Odoo record types and return to lookup extraction."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        project = context.queries.get(project_id)
        try:
            catalog = await _refresh_model_catalog(context, project)
        except (
            ConnectorError,
            LocalStackError,
            ProjectError,
            SecretStoreError,
            WorkspaceError,
        ) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Loaded {len(catalog.models)} existing Odoo record type(s).",
        )
        return RedirectResponse(
            f"/projects/{project_id}/derived-entities#lookup-extraction",
            status_code=303,
        )

    @router.post("/projects/{project_id}/derived-entities/save")
    async def save_project_derived_entity(request: Request, project_id: str):
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
        source_binding = _text(form, "source_binding")
        if "|" not in source_binding:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error="Choose a frozen source column",
                status_code=422,
            )
        source_dataset_id, source_column_key = source_binding.split("|", 1)
        try:
            project = context.queries.get(project_id)
            target_model = _existing_catalog_model(
                context,
                project,
                _text(form, "target_model"),
            )
            plan, rule = context.derived_entities.save_rule(
                project_id,
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
                actor=context.actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Created the related table {rule.output_dataset_name}.",
        )
        return RedirectResponse(
            (
                f"/projects/{project_id}/derived-entities"
                f"#lookup-rule-{rule.rule_id}"
            ),
            status_code=303,
        )

    @router.post("/projects/{project_id}/derived-entities/lookup/preview")
    async def preview_project_derived_entity(
        request: Request,
        project_id: str,
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
                project_id,
                error="Choose a frozen source column",
                status_code=422,
            )
        source_dataset_id, source_column_key = source_binding.split("|", 1)
        try:
            project = context.queries.get(project_id)
            target_model = _existing_catalog_model(
                context,
                project,
                _text(form, "target_model"),
            )
            rule, preview = context.derived_entities.preview_lookup(
                project_id,
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
                project_id,
                error=str(error),
                status_code=422,
            )
        return _render_derived_entities(
            request,
            context,
            project_id,
            pending_lookup={"rule": rule, "preview": preview},
        )

    @router.post("/projects/{project_id}/derived-entities/related/preview")
    async def preview_project_related_datasets(
        request: Request,
        project_id: str,
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
                project_id,
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
                project_id,
                error=str(error),
                status_code=422,
            )
        return _render_derived_entities(
            request,
            context,
            project_id,
            pending_related={"rule": rule, "preview": preview},
        )

    @router.post("/projects/{project_id}/derived-entities/related/save")
    async def save_project_related_datasets(
        request: Request,
        project_id: str,
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
            plan, rule = context.derived_entities.save_related_split(
                project_id,
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
                actor=context.actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
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
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    @router.post(
        "/projects/{project_id}/derived-entities/{rule_id}/delete"
    )
    async def delete_project_derived_entity(
        request: Request,
        project_id: str,
        rule_id: str,
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_parent_version"},
        )
        try:
            context.derived_entities.delete_rule(
                project_id,
                rule_id,
                expected_parent_version=_optional_int(
                    _text(form, "expected_parent_version")
                ),
                actor=context.actor,
            )
        except (WorkspaceError, ValueError) as error:
            return _render_derived_entities(
                request,
                context,
                project_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Removed the saved separation rule.",
        )
        return RedirectResponse(
            f"/projects/{project_id}/derived-entities",
            status_code=303,
        )

    return router
