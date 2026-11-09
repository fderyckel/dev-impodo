"""Open an exact saved Recipe in its eligible Authoring workspace."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from impodo.application.recipe_publication_service import (
    RecipeChangeReview,
    RecipeDraft,
)
from impodo.application.shared.identity import ResolvedRequestIdentity
from impodo.application.workspace.access import bind_workspace_access_context
from impodo.domain.data_version.models import DataVersionPurpose, DataVersionState
from impodo.domain.project.foundation import (
    MigrationNotFoundError,
    require_uuid,
)
from impodo.domain.recipe.models import (
    RecipeError,
    RecipePublication,
    RecipeRevisionRead,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaOrigin
from impodo.domain.workspace.models import MigrationWorkspace, MigrationWorkspaceState
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateNotFoundError,
)

from ..composition.page_reads import run_local_operation, run_page_read
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..presenters.recipes import build_recipe_change_view
from ..request_identity import request_identity
from ..security import require_session

_SESSION_KEY = "recipe_revision_review"


def can_revise_recipe(actor: Actor) -> bool:
    """Keep the revision action for people who can edit and publish Recipes."""

    return all(
        actor.has(capability)
        for capability in (
            Capability.RECIPE_VIEW,
            Capability.RECIPE_PUBLISH,
            Capability.MAPPING_EDIT,
        )
    )


@dataclass(frozen=True, slots=True)
class _AuthoringWorkspace:
    workspace_id: str
    data_version_id: str
    data_version_number: int
    database: str
    binding: str
    working_hash: str | None
    schema: OdooSchemaCatalog
    state: WorkspaceState

    @property
    def mapping_url(self) -> str:
        return f"/workspaces/{self.workspace_id}/mapping?step=check#recipe-revision-workspace"


def build_recipe_revision_router(context: WebContext) -> APIRouter:
    """Route an exact-version start and reviewed save through existing owners."""

    router = APIRouter()
    path = "/projects/{project_id}/recipes/{recipe_id}/revise"

    @router.get(path)
    async def review_recipe(
        request: Request,
        project_id: str,
        recipe_id: str,
        version: Annotated[int, Query(ge=1)],
    ):
        require_session(request)
        identity = request_identity(request, context.identity_resolver)
        return await run_page_read(
            _render_review, request, context, identity, project_id, recipe_id, version
        )

    @router.post(path)
    async def revise_recipe(
        request: Request,
        project_id: str,
        recipe_id: str,
        version: Annotated[int, Query(ge=1)],
    ):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "action",
                "review_id",
                "operation_id",
                "expected_recipe_revision",
                "draft_hash",
                "working_hash",
                "coverage_review_hash",
            },
        )
        identity = request_identity(request, context.identity_resolver)
        action = _text(form, "action")
        if action not in {"start", "create_draft", "approve_coverage", "publish"}:
            raise HTTPException(422, "Choose a Recipe revision action.")
        args = (request, context, identity, project_id, recipe_id, version)
        if action != "publish":
            started = await run_local_operation(
                _start_review, *args, create=action == "create_draft",
                coverage_review_hash=_text(form, "coverage_review_hash") if action == "approve_coverage" else "",
            )
            if isinstance(started, HTMLResponse):
                return started
            binding, mapping_url = started
            request.session[_SESSION_KEY] = binding
            return RedirectResponse(mapping_url, status_code=303)
        operation_id = _text(form, "operation_id")
        published = await run_local_operation(
            _publish_review,
            *args,
            review_id=_text(form, "review_id"),
            operation_id=operation_id,
            expected_revision=_text(form, "expected_recipe_revision"),
            draft_hash=_text(form, "draft_hash"),
            working_hash=_text(form, "working_hash") or None,
        )
        if isinstance(published, HTMLResponse):
            return published
        request.session[_SESSION_KEY]["published"] = operation_id
        if published.revision.version <= int(_text(form, "expected_recipe_revision")):
            _flash(
                request,
                f"These rules are already saved as Recipe v{published.revision.version}.",
            )
        else:
            _flash(
                request,
                f"Saved {published.recipe.display_name} as Recipe v{published.revision.version}.",
            )
        return RedirectResponse(
            f"/projects/{project_id}/recipes/{recipe_id}?version={published.revision.version}",
            status_code=303,
        )

    return router


def _selected_revision(
    context: WebContext,
    identity: ResolvedRequestIdentity,
    project_id: str,
    recipe_id: str,
    version: int,
) -> RecipeRevisionRead:
    project = context.project_queries.get_visible_project(
        project_id,
        scope=identity.organization_scope,
        authorization=identity.authorization,
        capability=Capability.RECIPE_PUBLISH,
    )
    if not can_revise_recipe(identity.actor):
        raise MigrationNotFoundError("Recipe revision not found")
    selection = (require_uuid(recipe_id, "recipe_id"), version)
    saved = context.recipes.read_revisions(
        project.project_id, (selection,), actor=identity.actor
    )[selection]
    if saved.recipe.archived_at is not None:
        raise MigrationNotFoundError("Recipe revision not found")
    return saved


def _authoring_workspace(
    context: WebContext,
    saved: RecipeRevisionRead,
    actor: Actor,
    revision_draft: MigrationWorkspace | None = None,
    *,
    read_working: bool = True,
) -> _AuthoringWorkspace | None:
    provenance = saved.envelope.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    if (
        revision_draft is not None
        and revision_draft.state is MigrationWorkspaceState.CLOSED
    ):
        return None
    workspace_id = (
        revision_draft.workspace_id
        if revision_draft is not None
        else provenance.get("origin_workspace_id")
    )
    data_version_id = provenance.get("origin_data_version_id")
    if not isinstance(workspace_id, str) or not isinstance(data_version_id, str):
        return None
    try:
        access = context.workspace_access.resolve(
            workspace_id, actor=actor, capability=Capability.MAPPING_EDIT
        )
        if (
            access.project_id != saved.recipe.project_id
            or access.data_version_id != data_version_id
            or access.recipe_application_id is not None
            or access.run_purpose != "AUTHORING"
        ):
            return None
        with bind_workspace_access_context(access):
            owner = context.workspace_views.get(workspace_id, actor=actor)
            delivery = owner.data_version
            if (
                owner.migration_workspace.state is not MigrationWorkspaceState.OPEN
                or delivery.purpose is not DataVersionPurpose.AUTHORING
                or delivery.state is not DataVersionState.FROZEN
            ):
                return None
            state = context.queries.get(workspace_id)
            if (
                state.source_mode is not SourceMode.FILE
                or state.odoo_connection_mode is None
            ):
                return None
            schema = context.queries.get_odoo_schema_catalog(workspace_id)
            target = target_identity_hash(
                connection_mode=state.odoo_connection_mode.value,
                base_url=state.odoo_base_url,
                database=state.odoo_database,
            )
            if (
                schema is None
                or schema.origin is not SchemaOrigin.LIVE_API
                or schema.connection_target_hash != target
            ):
                return None
            working = (
                context.queries.get_mapping_working_draft(workspace_id)
                if read_working
                else None
            )
    except (MigrationNotFoundError, WorkspaceStateNotFoundError):
        return None
    return _AuthoringWorkspace(
        workspace_id,
        data_version_id,
        delivery.version_number,
        state.odoo_database,
        content_hash(
            (
                delivery.source_package_hash,
                target,
                state.odoo_company.context_hash if state.odoo_company else None,
            )
        ),
        working.content_hash if working is not None else None,
        schema,
        state,
    )


def _draft(
    context: WebContext,
    saved: RecipeRevisionRead,
    workspace: _AuthoringWorkspace,
    actor: Actor,
) -> RecipeDraft:
    return context.recipe_publication.draft(
        project_id=saved.recipe.project_id,
        data_version_id=workspace.data_version_id,
        workspace_id=workspace.workspace_id,
        actor=actor,
    )


def _matches_baseline(
    saved: RecipeRevisionRead, draft: RecipeDraft, workspace: _AuthoringWorkspace
) -> bool:
    compiled = draft.compiled
    provenance = saved.envelope.get("provenance")
    return bool(
        isinstance(provenance, Mapping)
        and compiled is not None
        and draft.can_publish
        and compiled.semantic_hash == saved.revision.semantic_hash
        and compiled.schema_hash == provenance.get("schema_hash")
        and compiled.source_selection_hash == provenance.get("source_selection_hash")
        and workspace.working_hash in {None, compiled.mapping_content_hash}
    )


def _review_binding(
    saved: RecipeRevisionRead,
    workspace: _AuthoringWorkspace,
    identity: ResolvedRequestIdentity,
) -> dict[str, object]:
    return {
        "project_id": saved.recipe.project_id,
        "recipe_id": saved.recipe.recipe_id,
        "version": saved.revision.version,
        "workspace_id": workspace.workspace_id,
        "binding": workspace.binding,
        "principal": content_hash(
            (identity.actor.identity.issuer, identity.actor.identity.subject_id)
        ),
    }


def _active_review(session: object, binding: Mapping[str, object]) -> bool:
    return isinstance(session, Mapping) and all(
        session.get(key) == value for key, value in binding.items()
    )


def _revision_workspace(
    context: WebContext,
    saved: RecipeRevisionRead,
    actor: Actor,
    session: object,
    *,
    starting: bool = False,
) -> tuple[MigrationWorkspace | None, _AuthoringWorkspace | None]:
    # A running review keeps its original workspace even if someone opens another draft.
    if (
        isinstance(session, Mapping)
        and session.get("recipe_id") == saved.recipe.recipe_id
        and session.get("version") == saved.revision.version
        and not session.get("revision_draft")
    ):
        return None, _authoring_workspace(context, saved, actor)
    revision_draft = context.recipe_authoring_drafts.find(saved, actor=actor)
    applying_seed = (
        starting
        and revision_draft is not None
        and isinstance(session, Mapping)
        and session.get("pending_seed")
    )
    return revision_draft, _authoring_workspace(
        context, saved, actor, revision_draft, read_working=not applying_seed
    )


def _start_review(
    request: Request,
    context: WebContext,
    identity: ResolvedRequestIdentity,
    project_id: str,
    recipe_id: str,
    version: int,
    *,
    create: bool,
    coverage_review_hash: str = "",
) -> tuple[dict[str, object], str] | HTMLResponse:
    saved = _selected_revision(context, identity, project_id, recipe_id, version)
    session = request.session.get(_SESSION_KEY, {})
    if create:
        revision_draft = context.recipe_authoring_drafts.find(
            saved, actor=identity.actor
        )
        workspace = (
            _authoring_workspace(context, saved, identity.actor)
            if revision_draft is None
            else None
        )
    else:
        revision_draft, workspace = _revision_workspace(
            context,
            saved,
            identity.actor,
            session if not session.get("published") else None,
            starting=True,
        )
    draft = None
    try:
        if revision_draft is None:
            if workspace is not None:
                draft = _draft(context, saved, workspace, identity.actor)
                if _matches_baseline(saved, draft, workspace):
                    return {
                        **_review_binding(saved, workspace, identity),
                        "review_id": str(uuid4()),
                    }, workspace.mapping_url
            if not create:
                raise RecipeError("Use Create Authoring draft for this version.")
            revision_draft = context.recipe_authoring_drafts.create(
                saved, actor=identity.actor
            )
            workspace = None
        if revision_draft.state is MigrationWorkspaceState.CLOSED:
            raise RecipeError("This Authoring draft is closed.")
        if workspace is None:
            return {
                "project_id": project_id,
                "recipe_id": saved.recipe.recipe_id,
                "version": version,
                "workspace_id": revision_draft.workspace_id,
                "revision_draft": True,
                "pending_seed": True,
                "coverage_required": bool(
                    context.recipe_authoring_drafts.compiler.advanced_checks(
                        context.recipe_authoring_drafts.definition(saved),
                    )
                ),
            }, f"/workspaces/{revision_draft.workspace_id}/target"
        context.recipe_authoring_drafts.seed(
            saved, revision_draft, workspace.schema, workspace.state, actor=identity.actor,
            coverage_review_hash=coverage_review_hash,
        )
        return {
            **_review_binding(saved, workspace, identity),
            "review_id": str(uuid4()),
            "revision_draft": True,
        }, workspace.mapping_url
    except MigrationNotFoundError:
        raise
    except ValueError as error:
        return _render_review_page(
            request,
            context,
            identity,
            saved,
            workspace,
            revision_draft=revision_draft,
            draft=draft,
            error=str(error),
        )


def _publish_review(
    request: Request,
    context: WebContext,
    identity: ResolvedRequestIdentity,
    project_id: str,
    recipe_id: str,
    version: int,
    *,
    review_id: str,
    operation_id: str,
    expected_revision: str,
    draft_hash: str,
    working_hash: str | None,
) -> RecipePublication | HTMLResponse:
    saved = _selected_revision(context, identity, project_id, recipe_id, version)
    session = request.session.get(_SESSION_KEY)
    revision_draft, workspace = _revision_workspace(
        context, saved, identity.actor, session
    )
    try:
        if (
            workspace is None
            or not isinstance(session, Mapping)
            or not review_id
            or not _active_review(session, _review_binding(saved, workspace, identity))
            or session.get("review_id") != review_id
            or session.get("published") not in {None, operation_id}
            or workspace.working_hash != working_hash
        ):
            raise RecipeError(
                "The Authoring workspace or review changed. Review the current changes before saving."
            )
        return context.recipe_publication.publish(
            project_id=saved.recipe.project_id,
            data_version_id=workspace.data_version_id,
            workspace_id=workspace.workspace_id,
            recipe_id=saved.recipe.recipe_id,
            expected_recipe_revision=int(expected_revision),
            display_name=saved.recipe.display_name,
            business_purpose=saved.recipe.business_purpose,
            actor=identity.actor,
            operation_id=operation_id,
            change_review=RecipeChangeReview(version, draft_hash),
        )
    except MigrationNotFoundError:
        raise
    except ValueError as error:
        return _render_review_page(
            request,
            context,
            identity,
            saved,
            workspace,
            revision_draft=revision_draft,
            error=str(error),
        )


def _render_review(
    request: Request,
    context: WebContext,
    identity: ResolvedRequestIdentity,
    project_id: str,
    recipe_id: str,
    version: int,
) -> HTMLResponse:
    saved = _selected_revision(context, identity, project_id, recipe_id, version)
    session = request.session.get(_SESSION_KEY, {})
    revision_draft, workspace = _revision_workspace(
        context,
        saved,
        identity.actor,
        session if not session.get("published") else None,
    )
    return _render_review_page(
        request, context, identity, saved, workspace, revision_draft=revision_draft
    )


def _render_review_page(
    request: Request,
    context: WebContext,
    identity: ResolvedRequestIdentity,
    saved: RecipeRevisionRead,
    workspace: _AuthoringWorkspace | None,
    *,
    revision_draft: MigrationWorkspace | None = None,
    draft: RecipeDraft | None = None,
    error: str | None = None,
) -> HTMLResponse:
    session = request.session.get(_SESSION_KEY, {})
    active = (
        workspace is not None
        and not session.get("published")
        and _active_review(session, _review_binding(saved, workspace, identity))
    )
    if draft is None and workspace is not None and (revision_draft is None or active):
        draft = _draft(context, saved, workspace, identity.actor)
    eligible = (
        workspace is not None
        and draft is not None
        and _matches_baseline(saved, draft, workspace)
    )
    summary = latest_summary = None
    draft_hash = ""
    baseline = saved.envelope.get("recipe")
    latest_version = saved.recipe.current_recipe_revision
    coverage_review = None
    if revision_draft is not None and workspace is not None and not active and workspace.working_hash is None:
        try:
            coverage_review = context.recipe_authoring_drafts.coverage_review(
                saved, workspace.schema, actor=identity.actor,
            )
        except ValueError as coverage_error:
            error = str(coverage_error)
    if (
        active
        and workspace is not None
        and draft is not None
        and isinstance(baseline, Mapping)
    ):
        compiled = draft.compiled
        if (
            compiled is not None
            and draft.can_publish
            and workspace.working_hash in {None, compiled.mapping_content_hash}
        ):
            summary = build_recipe_change_view(baseline, compiled.recipe)
            latest_hash: object = saved.revision.semantic_hash
            if saved.revision.version != latest_version:
                latest = context.recipes.read_revision(
                    saved.recipe.recipe_id, latest_version, actor=identity.actor
                )
                latest_definition = latest.get("recipe")
                if isinstance(latest_definition, Mapping):
                    latest_summary = build_recipe_change_view(
                        latest_definition, compiled.recipe
                    )
                latest_hash = latest.get("semantic_hash")
            semantic_hash = compiled.semantic_hash
            if (
                semantic_hash != saved.revision.semantic_hash
                and semantic_hash != latest_hash
            ):
                draft_hash = RecipeChangeReview.hash_draft(compiled)
    return _render(
        request,
        "project_recipe_revision.html",
        recipe=saved.recipe,
        revision=saved.revision,
        workspace=workspace,
        revision_draft=revision_draft,
        coverage_review=coverage_review,
        can_approve_coverage=identity.actor.has(Capability.COVERAGE_SCOPE),
        active=active,
        eligible=eligible,
        summary=summary,
        latest_summary=latest_summary,
        draft_hash=draft_hash,
        review_id=session.get("review_id", ""),
        operation_id=str(uuid4()),
        latest_version=latest_version,
        issue=draft.issues[0] if draft and draft.issues else None,
        error=error,
        status_code=422 if error is not None else 200,
    )
