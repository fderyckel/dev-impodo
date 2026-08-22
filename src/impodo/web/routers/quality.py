"""Stage-F routes for editing guided manager-authored quality checks."""

from __future__ import annotations
from fastapi import Request
from fastapi.responses import RedirectResponse
from ...quality import MAX_MANAGER_RULES_PER_DATASET
from ...workspace_errors import WorkspaceError
from ..security import require_session
from fastapi import APIRouter
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash
from ..presenters.mapping_impact import _mapping_return_url
from ..presenters.mapping_view import _manager_quality_rules_from_form


def build_quality_router(context: WebContext) -> APIRouter:
    """Build routes that translate check forms into ``QualityService`` calls."""

    router = APIRouter()

    @router.post("/workspaces/{project_id}/mapping/quality")
    async def save_project_quality_checks(request: Request, project_id: str):
        """Save guided business checks without exposing the rule contract."""

        require_session(request)
        form = await request.form()
        allowed = {"csrf_token", "quality_dataset_id"} | {
            f"quality_{field}_{index}"
            for index in range(MAX_MANAGER_RULES_PER_DATASET)
            for field in (
                "name",
                "family",
                "field_a",
                "field_b",
                "equals",
                "outcome",
                "owner",
            )
        }
        _secure_form(request, form, allowed)
        try:
            dataset_id = _text(form, "quality_dataset_id")
            configuration = context.quality.configuration(project_id, dataset_id)
            manager_rules = tuple(
                _manager_quality_rules_from_form(
                    form,
                    project_id=project_id,
                    dataset=configuration.dataset_name,
                    allowed_fields=set(configuration.allowed_fields),
                )
            )
            context.quality.save_manager_rules(
                configuration,
                manager_rules,
                actor=context.actor,
            )
        except (ValueError, WorkspaceError) as error:
            request.session["mapping_error"] = str(error)
            return RedirectResponse(
                _mapping_return_url(request, project_id),
                status_code=303,
            )
        _flash(
            request,
            (
                f"Saved {len(manager_rules)} "
                "optional business data check(s). Recommended checks remain on."
            ),
        )
        return RedirectResponse(
            _mapping_return_url(request, project_id),
            status_code=303,
        )

    return router
