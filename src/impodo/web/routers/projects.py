"""Translate the Stage A setup wizard into project lifecycle operations.

Layer: web route. The router creates a draft, collects project details and
governance, accepts immutable source files, configures the target, and invokes
registration. Validation and lifecycle meaning remain in ``ProjectService``;
artifact intake remains in ``SourceIntakeService``.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from ...access import AuthorizationError, Capability
from ...intake import SourceIntakeError
from ...local_stack import LocalStackError
from ...projects import (
    DataClassification,
    MigrationProject,
    ProjectConflictError,
    ProjectError,
    ProjectRegistrationError,
    ProjectSetupStep,
    ProjectStatus,
    SourceMode,
    project_setup_requirements_for_step,
    registration_problems,
)
from ...recipes import DataVersionPurpose, RecipeError
from ...domain.recipe_applications import RecipeApplicationError
from ...domain.recipe_qualifications import RecipeQualificationError
from ...secrets import SecretStoreError
from ..security import require_session
from fastapi import APIRouter
from ..constants import SOURCE_SYSTEMS
from ..context import WebContext
from ..forms import _form_values, _revision, _secure_form, _text
from ..presenters.common import _flash, _project_error, _render
from ..presenters.mapping_forms import _draft_or_redirect
from ..presenters.setup import blocking_setup_url
from ..target_credentials import (
    TargetCredentialRole,
    TargetCredentialRemovalReason,
    audit_removed_target_credentials,
    delete_target_credentials,
    get_target_credential,
)


def build_projects_router(context: WebContext) -> APIRouter:
    """Build project list, setup, registration, and deletion routes."""

    router = APIRouter()

    @router.get("/recipes", response_class=HTMLResponse)
    async def recipe_list(request: Request):
        require_session(request)
        return _render(
            request,
            "recipe_list.html",
            recipes=context.recipes.list(actor=context.actor),
        )

    @router.post("/recipes/{recipe_id}/delete")
    async def delete_recipe(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "recipe_revision", "workspace_revision"},
        )
        try:
            expected_recipe_revision = int(_text(form, "recipe_revision"))
            expected_workspace_revision = int(_text(form, "workspace_revision"))
            context.authorization.require(
                context.actor,
                Capability.RECIPE_DELETE,
            )
            recipe = context.recipes.get(recipe_id, actor=context.actor)
            versions = context.recipes.data_versions(recipe_id, actor=context.actor)
            current = next(
                item
                for item in versions
                if item.data_version_id == recipe.current_data_version_id
            )
            project = context.queries.get(current.workspace_project_id)
            context.recipes.validate_draft_deletion(
                recipe_id,
                expected_recipe_revision=expected_recipe_revision,
                expected_workspace_revision=expected_workspace_revision,
                actor=context.actor,
            )
            if (
                context.preparation_jobs is not None
                and context.preparation_jobs.active(project.project_id) is not None
            ):
                raise ProjectConflictError(
                    "Preparation is still running. Stop it before deleting "
                    "this project."
                )
            context.local_stack.forget_project(project.project_id)
            context.remote_connections.clear(project.project_id)
            context.odoo_provenance.delete_recipe_workspace_key(
                project.project_id,
                actor=context.actor,
            )
            removal_receipts = delete_target_credentials(
                context.secret_store,
                project,
                reason=TargetCredentialRemovalReason.RECIPE_DELETED,
            )
            audit_removed_target_credentials(
                context.projects,
                project,
                removal_receipts,
                actor=context.actor,
            )
            await run_in_threadpool(
                context.recipes.delete_draft,
                recipe_id,
                actor=context.actor,
                expected_recipe_revision=expected_recipe_revision,
                expected_workspace_revision=expected_workspace_revision,
            )
            if context.preparation_jobs is not None:
                context.preparation_jobs.delete_project_history(project.project_id)
        except AuthorizationError as error:
            raise HTTPException(
                status_code=403,
                detail="Not authorized to delete this Recipe",
            ) from error
        except (
            LocalStackError,
            SecretStoreError,
            ProjectError,
            RecipeError,
            StopIteration,
            ValueError,
        ) as error:
            return _render(
                request,
                "recipe_list.html",
                recipes=context.recipes.list(actor=context.actor),
                error=str(error),
                status_code=422,
            )
        _flash(request, f'Deleted Recipe "{recipe.display_name}".')
        return RedirectResponse("/recipes", status_code=303)

    @router.get("/recipes/new", response_class=HTMLResponse)
    async def new_recipe_form(request: Request):
        require_session(request)
        return _render(
            request,
            "recipe_new.html",
            source_systems=SOURCE_SYSTEMS,
            values={"creation_request_id": str(uuid4())},
        )

    @router.post("/recipes/new")
    async def new_recipe(request: Request):
        """Create the minimal draft and enter its governed setup sequence."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "creation_request_id",
                "name",
                "source_system",
                "source_mode",
            },
        )
        values = _form_values(form)
        try:
            recipe, project = context.recipe_authoring.create(
                actor=context.actor,
                name=values.get("name", ""),
                source_system=values.get("source_system", ""),
                source_mode=values.get("source_mode", SourceMode.FILE.value),
                creation_request_id=values.get("creation_request_id", ""),
            )
        except (ProjectError, RecipeError) as error:
            return _render(
                request,
                "recipe_new.html",
                source_systems=SOURCE_SYSTEMS,
                values=values,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(f"/recipes/{recipe.recipe_id}", status_code=303)

    @router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
    async def recipe_overview(request: Request, recipe_id: str):
        require_session(request)
        return _render_recipe_overview(request, context, recipe_id)

    @router.post("/recipes/{recipe_id}/parameters")
    async def save_recipe_parameter(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "name", "label", "value_type", "required"},
        )
        try:
            context.recipe_authoring.save_parameter_definition(
                recipe_id,
                name=_text(form, "name"),
                label=_text(form, "label"),
                value_type=_text(form, "value_type"),
                required=_text(form, "required") == "yes",
                actor=context.actor,
            )
        except (RecipeError, ValueError) as error:
            return _render_recipe_overview(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Recipe application input saved.")
        return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)

    @router.post("/recipes/{recipe_id}/parameters/remove")
    async def remove_recipe_parameter(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "name"})
        try:
            context.recipe_authoring.remove_parameter_definition(
                recipe_id,
                name=_text(form, "name"),
                actor=context.actor,
            )
        except (RecipeError, ValueError) as error:
            return _render_recipe_overview(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Recipe application input removed.")
        return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)

    @router.get("/recipes/{recipe_id}/qualification", response_class=HTMLResponse)
    async def recipe_qualification(request: Request, recipe_id: str):
        require_session(request)
        return _render_recipe_qualification(request, context, recipe_id)

    @router.post("/recipes/{recipe_id}/qualify")
    async def qualify_recipe(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "expected_recipe_revision",
                "create_count",
                "update_count",
                "unchanged_count",
                "verified_count",
            },
        )
        try:
            credential_generation, storage_class = _recipe_read_credential(
                context,
                recipe_id,
            )
            qualification = context.recipe_qualifications.qualify(
                recipe_id,
                expected_recipe_revision=int(_text(form, "expected_recipe_revision")),
                expected_outcomes={
                    "create_count": _text(form, "create_count"),
                    "update_count": _text(form, "update_count"),
                    "unchanged_count": _text(form, "unchanged_count"),
                    "verified_count": _text(form, "verified_count"),
                },
                credential_generation=credential_generation,
                credential_storage_class=storage_class,
                actor=context.actor,
            )
        except (
            RecipeError,
            RecipeApplicationError,
            RecipeQualificationError,
            ProjectError,
            SecretStoreError,
            ValueError,
        ) as error:
            return _render_recipe_qualification(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Recipe v{qualification.recipe_revision} qualified from the exact Test result.",
        )
        return RedirectResponse(
            f"/recipes/{recipe_id}/qualification",
            status_code=303,
        )

    @router.post("/recipes/{recipe_id}/cutover")
    async def select_recipe_cutover(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_recipe_revision"},
        )
        try:
            candidate = context.recipe_qualifications.select_current(
                recipe_id,
                expected_recipe_revision=int(_text(form, "expected_recipe_revision")),
                actor=context.actor,
            )
        except (RecipeError, RecipeQualificationError, ValueError) as error:
            return _render_recipe_qualification(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            f"Recipe v{candidate.recipe_revision} selected for rollout preparation.",
        )
        return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)

    @router.get("/recipes/{recipe_id}/test", response_class=HTMLResponse)
    async def start_recipe_test_form(request: Request, recipe_id: str):
        require_session(request)
        recipe = context.recipes.get(recipe_id, actor=context.actor)
        if recipe.current_recipe_revision is None:
            return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)
        envelope = context.recipes.read_revision(
            recipe_id,
            recipe.current_recipe_revision,
            actor=context.actor,
        )
        parameters = tuple(
            dict(envelope["recipe"]["parameter_definitions"]).get(
                "parameters",
                (),
            )
        )
        return _render(
            request,
            "recipe_test_start.html",
            recipe=recipe,
            parameters=parameters,
            values={},
        )

    @router.post("/recipes/{recipe_id}/test")
    async def start_recipe_test(request: Request, recipe_id: str):
        form = await request.form()
        recipe = context.recipes.get(recipe_id, actor=context.actor)
        if recipe.current_recipe_revision is None:
            return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)
        envelope = context.recipes.read_revision(
            recipe_id,
            recipe.current_recipe_revision,
            actor=context.actor,
        )
        parameters = tuple(
            dict(envelope["recipe"]["parameter_definitions"]).get(
                "parameters",
                (),
            )
        )
        parameter_fields = {
            f"parameter__{item['logical_parameter_id']}" for item in parameters
        }
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "expected_recipe_revision",
                "label",
                *parameter_fields,
            },
        )
        values = _form_values(form)
        supplied = {
            str(item["logical_parameter_id"]): _text(
                form,
                f"parameter__{item['logical_parameter_id']}",
            )
            for item in parameters
        }
        try:
            _data_version, project = (
                context.recipe_applications.start_test_data_version(
                    recipe_id,
                    expected_recipe_revision=int(
                        _text(form, "expected_recipe_revision")
                    ),
                    label=_text(form, "label"),
                    parameter_values=supplied,
                    actor=context.actor,
                )
            )
        except (RecipeError, RecipeApplicationError, ProjectError, ValueError) as error:
            return _render(
                request,
                "recipe_test_start.html",
                recipe=context.recipes.get(recipe_id, actor=context.actor),
                parameters=parameters,
                values=values,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Test data version created. Add the representative replacement files.",
        )
        return RedirectResponse(
            f"/projects/{project.project_id}/files",
            status_code=303,
        )

    @router.get("/recipes/{recipe_id}/production", response_class=HTMLResponse)
    async def start_recipe_production_form(request: Request, recipe_id: str):
        require_session(request)
        recipe = context.recipes.get(recipe_id, actor=context.actor)
        candidate = context.recipes.cutover_candidate(
            recipe_id,
            actor=context.actor,
        )
        if candidate is None:
            return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)
        envelope = context.recipes.read_revision(
            recipe_id,
            candidate.recipe_revision,
            actor=context.actor,
        )
        definition = dict(envelope["recipe"])
        return _render(
            request,
            "recipe_production_start.html",
            recipe=recipe,
            cutover_candidate=candidate,
            parameters=tuple(
                dict(definition["parameter_definitions"]).get("parameters", ())
            ),
            controls=tuple(dict(definition["control_definitions"]).get("controls", ())),
            values={},
        )

    @router.post("/recipes/{recipe_id}/production")
    async def start_recipe_production(request: Request, recipe_id: str):
        form = await request.form()
        candidate = context.recipes.cutover_candidate(
            recipe_id,
            actor=context.actor,
        )
        if candidate is None:
            return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)
        envelope = context.recipes.read_revision(
            recipe_id,
            candidate.recipe_revision,
            actor=context.actor,
        )
        definition = dict(envelope["recipe"])
        parameters = tuple(
            dict(definition["parameter_definitions"]).get("parameters", ())
        )
        controls = tuple(dict(definition["control_definitions"]).get("controls", ()))
        parameter_fields = {
            f"parameter__{item['logical_parameter_id']}" for item in parameters
        }
        control_fields = {f"control__{item['logical_control_id']}" for item in controls}
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "expected_recipe_revision",
                "expected_cutover_candidate_id",
                "label",
                *parameter_fields,
                *control_fields,
            },
        )
        values = _form_values(form)
        supplied_parameters = {
            str(item["logical_parameter_id"]): _text(
                form,
                f"parameter__{item['logical_parameter_id']}",
            )
            for item in parameters
        }
        supplied_controls = {
            str(item["logical_control_id"]): _text(
                form,
                f"control__{item['logical_control_id']}",
            )
            for item in controls
        }
        try:
            _data_version, project = (
                context.recipe_applications.start_production_data_version(
                    recipe_id,
                    expected_recipe_revision=int(
                        _text(form, "expected_recipe_revision")
                    ),
                    expected_cutover_candidate_id=_text(
                        form,
                        "expected_cutover_candidate_id",
                    ),
                    label=_text(form, "label"),
                    parameter_values=supplied_parameters,
                    control_values=supplied_controls,
                    actor=context.actor,
                )
            )
        except (
            RecipeError,
            RecipeApplicationError,
            ProjectError,
            ValueError,
        ) as error:
            return _render(
                request,
                "recipe_production_start.html",
                recipe=context.recipes.get(recipe_id, actor=context.actor),
                cutover_candidate=candidate,
                parameters=parameters,
                controls=controls,
                values=values,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Production data version created. Add the complete latest source package.",
        )
        return RedirectResponse(
            f"/projects/{project.project_id}/files",
            status_code=303,
        )

    @router.get("/recipes/{recipe_id}/application", response_class=HTMLResponse)
    async def recipe_application_review(request: Request, recipe_id: str):
        require_session(request)
        return _render_recipe_application(request, context, recipe_id)

    @router.post("/recipes/{recipe_id}/application/inputs")
    async def save_recipe_application_inputs(request: Request, recipe_id: str):
        form = await request.form()
        review = _recipe_application_review(context, recipe_id)
        parameter_fields = {
            f"parameter__{item['logical_parameter_id']}"
            for item in review.parameter_definitions
        }
        control_fields = {
            f"control__{item['logical_control_id']}"
            for item in review.control_definitions
        }
        override_fields = {
            f"override__{logical_id}" for logical_id in review.source_candidates
        }
        _secure_form(
            request,
            form,
            {"csrf_token", *parameter_fields, *control_fields, *override_fields},
        )
        parameters = {
            str(item["logical_parameter_id"]): _text(
                form,
                f"parameter__{item['logical_parameter_id']}",
            )
            for item in review.parameter_definitions
        }
        controls = {
            str(item["logical_control_id"]): _text(
                form,
                f"control__{item['logical_control_id']}",
            )
            for item in review.control_definitions
        }
        overrides = {
            logical_id: _text(form, f"override__{logical_id}")
            for logical_id in review.source_candidates
            if _text(form, f"override__{logical_id}")
        }
        try:
            context.recipe_applications.save_inputs(
                recipe_id,
                parameter_values=parameters,
                control_values=controls,
                overrides=overrides,
                actor=context.actor,
            )
        except (RecipeError, RecipeApplicationError, ProjectError, ValueError) as error:
            return _render_recipe_application(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Current data-version inputs saved.")
        return RedirectResponse(
            f"/recipes/{recipe_id}/application",
            status_code=303,
        )

    @router.post("/recipes/{recipe_id}/apply")
    async def apply_recipe(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        try:
            review = _recipe_application_review(context, recipe_id)
            credential = get_target_credential(
                context.secret_store,
                review.project,
                TargetCredentialRole.READ,
            )
            if credential is None:
                environment = review.data_version.purpose.value.title()
                raise RecipeApplicationError(
                    f"Enter and probe the read-only {environment} Odoo API key first"
                )
            evidence = context.recipe_applications.apply(
                recipe_id,
                credential_generation=credential.binding_hash,
                credential_storage_class=(
                    "OPERATING_SYSTEM_VAULT" if credential.persistent else "SESSION"
                ),
                actor=context.actor,
            )
        except (
            RecipeError,
            RecipeApplicationError,
            ProjectError,
            SecretStoreError,
            ValueError,
        ) as error:
            return _render_recipe_application(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        if evidence.status.value == "BLOCKED":
            return _render_recipe_application(
                request,
                context,
                recipe_id,
                error="Review the focused drift items before applying this Recipe.",
                status_code=422,
            )
        _flash(
            request,
            "Recipe applied. Review the fresh field matches before submitting them.",
        )
        return RedirectResponse(
            f"/projects/{evidence.workspace_project_id}/mapping",
            status_code=303,
        )

    @router.post("/recipes/{recipe_id}/publish")
    async def publish_recipe(request: Request, recipe_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "expected_recipe_revision"},
        )
        try:
            context.recipe_authoring.publish_current(
                recipe_id,
                expected_recipe_revision=int(_text(form, "expected_recipe_revision")),
                actor=context.actor,
            )
        except (RecipeError, ValueError) as error:
            return _render_recipe_overview(
                request,
                context,
                recipe_id,
                error=str(error),
                status_code=422,
            )
        updated = context.recipes.get(recipe_id, actor=context.actor)
        _flash(
            request,
            f"Published Recipe v{updated.current_recipe_revision}.",
        )
        return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)

    @router.get("/projects/{project_id}")
    async def open_project(request: Request, project_id: str):
        require_session(request)
        project = context.queries.get(project_id)
        destination = (
            "overview" if project.status is ProjectStatus.REGISTERED else "details"
        )
        return RedirectResponse(
            f"/projects/{project.project_id}/{destination}",
            status_code=303,
        )

    @router.get("/projects/{project_id}/overview", response_class=HTMLResponse)
    async def project_overview(request: Request, project_id: str):
        require_session(request)
        project = context.queries.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            return RedirectResponse(
                f"/projects/{project.project_id}/details",
                status_code=303,
            )
        return _render(
            request,
            "project_overview.html",
            project=project,
        )

    @router.get("/projects/{project_id}/details", response_class=HTMLResponse)
    async def project_details_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        return _render(
            request,
            "project_details.html",
            project=project,
            source_systems=SOURCE_SYSTEMS,
        )

    @router.post("/projects/{project_id}/details")
    async def project_details(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "name",
                "source_system",
                "export_status",
                "export_date",
                "description",
                "action",
            },
        )
        try:
            project = context.projects.update_details(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                name=_text(form, "name"),
                source_system=_text(form, "source_system"),
                export_status=_text(form, "export_status"),
                export_date=_text(form, "export_date"),
                description=_text(form, "description"),
            )
            context.recipe_authoring.synchronize_setup(
                project,
                actor=context.actor,
            )
        except (ProjectError, RecipeError) as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_details.html",
                error,
                source_systems=SOURCE_SYSTEMS,
            )
        if _text(form, "action") == "save_exit":
            _flash(request, "Draft saved.")
            return RedirectResponse("/recipes", status_code=303)
        if project_setup_requirements_for_step(
            project,
            ProjectSetupStep.DETAILS,
        ):
            return _render(
                request,
                "project_details.html",
                project=project,
                source_systems=SOURCE_SYSTEMS,
                setup_attention_requested=True,
                status_code=422,
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/governance",
            status_code=303,
        )

    @router.get("/projects/{project_id}/governance", response_class=HTMLResponse)
    async def project_governance_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        blocked = _blocked_setup_redirect(
            project,
            ProjectSetupStep.GOVERNANCE,
        )
        if blocked is not None:
            return blocked
        governance_was_saved = context.queries.has_project_audit_event(
            project_id,
            "PROJECT_GOVERNANCE_UPDATED",
        )
        return _render(
            request,
            "project_governance.html",
            project=project,
            data_classification_for_form=(
                project.data_classification.value
                if governance_was_saved
                else DataClassification.INTERNAL.value
            ),
        )

    @router.post("/projects/{project_id}/governance")
    async def project_governance(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "data_manager",
                "functional_owner",
                "business_unit",
                "data_classification",
                "retention_days",
                "support_access",
            },
        )
        current = _draft_or_redirect(context, project_id)
        if isinstance(current, RedirectResponse):
            return current
        blocked = _blocked_setup_redirect(
            current,
            ProjectSetupStep.GOVERNANCE,
        )
        if blocked is not None:
            return blocked
        try:
            project = context.projects.update_governance(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                data_manager=_text(form, "data_manager"),
                functional_owner=_text(form, "functional_owner"),
                business_unit=_text(form, "business_unit"),
                data_classification=_text(form, "data_classification"),
                retention_days=int(_text(form, "retention_days")),
                support_access="support_access" in form,
            )
            context.recipe_authoring.synchronize_setup(
                project,
                actor=context.actor,
            )
        except (ProjectError, RecipeError, ValueError) as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_governance.html",
                error,
            )
        if project_setup_requirements_for_step(
            project,
            ProjectSetupStep.GOVERNANCE,
        ):
            return _render(
                request,
                "project_governance.html",
                project=project,
                data_classification_for_form=project.data_classification.value,
                setup_attention_requested=True,
                status_code=422,
            )
        next_page = "files" if project.source_mode is SourceMode.FILE else "target"
        return RedirectResponse(
            f"/projects/{project.project_id}/{next_page}",
            status_code=303,
        )

    @router.get("/projects/{project_id}/files", response_class=HTMLResponse)
    async def project_files_form(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        blocked = _blocked_setup_redirect(
            project,
            ProjectSetupStep.FILES,
        )
        if blocked is not None:
            return blocked
        if project.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                f"/projects/{project.project_id}/target",
                status_code=303,
            )
        return _render(request, "project_files.html", project=project)

    @router.post("/projects/{project_id}/files")
    async def project_files(request: Request, project_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "source_file"},
        )
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        blocked = _blocked_setup_redirect(
            project,
            ProjectSetupStep.FILES,
        )
        if blocked is not None:
            return blocked
        if project.source_mode is SourceMode.ODOO:
            return RedirectResponse(
                f"/projects/{project.project_id}/target",
                status_code=303,
            )
        upload = form.get("source_file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _project_error(
                request,
                context,
                project_id,
                "project_files.html",
                SourceIntakeError("Choose a CSV or XLSX file"),
            )
        try:
            await run_in_threadpool(
                context.intake.accept,
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
                display_name=upload.filename,
                stream=upload.file,
            )
        except ProjectError as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_files.html",
                error,
            )
        finally:
            await upload.close()
        return RedirectResponse(
            f"/projects/{project_id}/files",
            status_code=303,
        )

    @router.get("/projects/{project_id}/review", response_class=HTMLResponse)
    async def project_review(request: Request, project_id: str):
        require_session(request)
        project = _draft_or_redirect(context, project_id)
        if isinstance(project, RedirectResponse):
            return project
        blocked = _blocked_setup_redirect(
            project,
            ProjectSetupStep.REVIEW,
        )
        if blocked is not None:
            return blocked
        return _render(
            request,
            "project_review.html",
            project=project,
            problems=registration_problems(project),
        )

    @router.post("/projects/{project_id}/register")
    async def register_project(request: Request, project_id: str):
        """Register a complete draft or render every remaining problem."""

        form = await request.form()
        _secure_form(request, form, {"csrf_token", "revision"})
        try:
            project = context.projects.register(
                project_id,
                actor=context.actor,
                expected_revision=_revision(form),
            )
        except ProjectRegistrationError as error:
            project = context.queries.get(project_id)
            return _render(
                request,
                "project_review.html",
                project=project,
                problems=error.problems,
                error="The project is not ready to register",
                status_code=422,
            )
        except ProjectError as error:
            return _project_error(
                request,
                context,
                project_id,
                "project_review.html",
                error,
                problems=registration_problems(context.queries.get(project_id)),
            )
        return RedirectResponse(
            f"/projects/{project.project_id}/overview",
            status_code=303,
        )

    return router


def _blocked_setup_redirect(
    project: MigrationProject,
    requested_step: ProjectSetupStep,
) -> RedirectResponse | None:
    destination = blocking_setup_url(project, requested_step)
    return (
        RedirectResponse(destination, status_code=303)
        if destination is not None
        else None
    )


def _render_recipe_overview(
    request: Request,
    context: WebContext,
    recipe_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    """Render the Recipe root with consistent lifecycle and authoring context."""

    recipe = context.recipes.get(recipe_id, actor=context.actor)
    versions = context.recipes.data_versions(recipe_id, actor=context.actor)
    revisions = context.recipes.revisions(recipe_id, actor=context.actor)
    draft = context.recipe_authoring.draft(recipe_id, actor=context.actor)
    project = (
        context.queries.get(draft.workspace_project_id)
        if draft.workspace_project_id
        else None
    )
    current_data_version = next(
        (
            item
            for item in versions
            if item.data_version_id == recipe.current_data_version_id
        ),
        None,
    )
    parameter_authoring_enabled = bool(
        current_data_version is not None
        and current_data_version.purpose is DataVersionPurpose.AUTHORING
        and current_data_version.state.value == "ACTIVE"
    )
    recipe_parameters = (
        context.recipe_authoring.parameter_definitions(
            recipe_id,
            actor=context.actor,
        )
        if parameter_authoring_enabled
        else ()
    )
    qualification_review = _recipe_qualification_review(context, recipe_id)
    cutover_candidate = context.recipes.cutover_candidate(
        recipe_id,
        actor=context.actor,
    )
    application_draft = (
        context.recipe_applications.current_draft(
            project.project_id,
            actor=context.actor,
        )
        if project is not None
        and current_data_version is not None
        and current_data_version.purpose is DataVersionPurpose.PRODUCTION
        else None
    )
    return _render(
        request,
        "recipe_overview.html",
        recipe=recipe,
        data_versions=versions,
        recipe_revisions=revisions,
        recipe_draft=draft,
        project=project,
        current_data_version=current_data_version,
        qualification_review=qualification_review,
        cutover_candidate=cutover_candidate,
        application_draft=application_draft,
        parameter_authoring_enabled=parameter_authoring_enabled,
        recipe_parameters=recipe_parameters,
        recovery_href=(
            _recipe_recovery_href(project, draft.issues[0].code)
            if project is not None and draft.issues
            else None
        ),
        error=error,
        status_code=status_code,
    )


def _recipe_application_review(context: WebContext, recipe_id: str):
    """Build one review using only safe credential-generation metadata."""

    recipe = context.recipes.get(recipe_id, actor=context.actor)
    versions = context.recipes.data_versions(recipe_id, actor=context.actor)
    current = next(
        item
        for item in versions
        if item.data_version_id == recipe.current_data_version_id
    )
    project = context.queries.get(current.workspace_project_id)
    try:
        credential = get_target_credential(
            context.secret_store,
            project,
            TargetCredentialRole.READ,
        )
    except SecretStoreError:
        credential = None
    return context.recipe_applications.review(
        recipe_id,
        credential_generation=(credential.binding_hash if credential else ""),
        credential_storage_class=(
            "OPERATING_SYSTEM_VAULT"
            if credential is not None and credential.persistent
            else "SESSION"
        ),
        actor=context.actor,
    )


def _recipe_read_credential(
    context: WebContext,
    recipe_id: str,
) -> tuple[str, str]:
    """Return only the current read-key generation and safe storage class."""

    recipe = context.recipes.get(recipe_id, actor=context.actor)
    versions = context.recipes.data_versions(recipe_id, actor=context.actor)
    current = next(
        (
            item
            for item in versions
            if item.data_version_id == recipe.current_data_version_id
        ),
        None,
    )
    if current is None:
        return "", "SESSION"
    project = context.queries.get(current.workspace_project_id)
    credential = get_target_credential(
        context.secret_store,
        project,
        TargetCredentialRole.READ,
    )
    return (
        credential.binding_hash if credential else "",
        (
            "OPERATING_SYSTEM_VAULT"
            if credential is not None and credential.persistent
            else "SESSION"
        ),
    )


def _recipe_qualification_review(context: WebContext, recipe_id: str):
    try:
        generation, storage_class = _recipe_read_credential(context, recipe_id)
    except SecretStoreError:
        generation, storage_class = "", "SESSION"
    return context.recipe_qualifications.review(
        recipe_id,
        credential_generation=generation,
        credential_storage_class=storage_class,
        actor=context.actor,
    )


def _render_recipe_qualification(
    request: Request,
    context: WebContext,
    recipe_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    review = _recipe_qualification_review(context, recipe_id)
    return _render(
        request,
        "recipe_qualification.html",
        recipe=review.recipe,
        project=review.project,
        qualification_review=review,
        error=error,
        status_code=status_code,
    )


def _render_recipe_application(
    request: Request,
    context: WebContext,
    recipe_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    review = _recipe_application_review(context, recipe_id)
    recipe = context.recipes.get(recipe_id, actor=context.actor)
    return _render(
        request,
        "recipe_application.html",
        recipe=recipe,
        project=review.project,
        application_review=review,
        blocker_count=sum(item.blocks for item in review.issues),
        information_count=sum(not item.blocks for item in review.issues),
        error=error,
        status_code=status_code,
    )


def _recipe_recovery_href(project, issue_code: str) -> str:
    """Map one publication issue to its existing authoring surface."""

    project_id = project.project_id
    if project.status is not ProjectStatus.REGISTERED:
        return f"/projects/{project_id}/details"
    if issue_code == "SOURCE_NOT_FROZEN":
        return f"/projects/{project_id}/datasets"
    if issue_code == "TARGET_GOVERNANCE_STALE":
        return f"/projects/{project_id}/schema"
    if issue_code == "QUALITY_RULES_NOT_READY":
        return f"/projects/{project_id}/prepare"
    return f"/projects/{project_id}/mapping"
