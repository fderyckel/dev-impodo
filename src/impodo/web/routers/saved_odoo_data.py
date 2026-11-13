"""Expose reusable Odoo capture settings, immutable versions, and restore actions.

Local page reads finish before source-company discovery or remote checks.
Every mutation checks the project owner and CSRF before accepting bounded
technical field choices. Captured versions remain usable without source access.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from impodo.application.destination_matching_service import (
    destination_match_scope_candidates,
)
from impodo.application.odoo_saved_data_jobs import SavedOdooActionError
from impodo.application.odoo_saved_data_service import SavedOdooDataService
from impodo.domain.odoo_relationship_scope import OdooRelationshipCaptureAction
from impodo.domain.odoo_restore import (
    RestoreFieldRule,
    RestoreLinkPolicy,
    RestoreValuePolicy,
    compile_odoo_restore,
)
from impodo.domain.odoo_saved_data import SavedOdooRestoreProfile
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_uuid,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.access import Capability
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.business_keys import recommend_business_key
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation, run_page_read
from impodo.web.composition.saved_odoo_data import (
    saved_capture_access,
    saved_source_companies,
)
from impodo.web.composition.saved_odoo_restore import restore_saved_odoo_data
from impodo.web.context import WebContext
from impodo.web.forms import _revision, _secure_form, _text
from impodo.web.presenters.common import _flash, _render
from impodo.web.request_identity import request_identity
from impodo.web.security import require_csrf, require_session

_UPLOAD_LIMIT = 100 * 1024 * 1024
_BASE_RESTORE_FIELDS = {
    "csrf_token",
    "operation_id",
    "version_number",
    "profile_id",
    "odoo_connection_mode",
    "odoo_base_url",
    "odoo_database",
    "destination_company_name",
    "api_key",
    "policy_default",
    "confirm_restore",
    "action",
}
_SAFE_ERRORS = (WorkspaceError, MigrationFoundationError, ValueError)


def _service(context: WebContext) -> SavedOdooDataService:
    service = context.saved_odoo_data
    if service is None:
        raise HTTPException(503, "Saved Odoo data is unavailable in this deployment")
    return service


def _location(project_id: str, saved_data_id: str | None = None, **query: str) -> str:
    parameters = {"selected": saved_data_id, **query} if saved_data_id else query
    suffix = urlencode({name: value for name, value in parameters.items() if value})
    return f"/projects/{project_id}/saved-odoo-data" + (f"?{suffix}" if suffix else "")


def _positive(value: str, label: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise HTTPException(422, f"Choose a valid {label}") from error
    if result < 1:
        raise HTTPException(422, f"Choose a valid {label}")
    return result


def _visible_project(request: Request, context: WebContext, project_id: str):
    identity = request_identity(request, context.identity_resolver)
    return context.project_queries.get_visible_project(
        project_id,
        scope=identity.organization_scope,
        authorization=identity.authorization,
    )


def _owned_source(context: WebContext, project_id: str, workspace_id: str, actor):
    owner = context.migration_workspaces.get(workspace_id, actor=actor)
    if owner.project_id != project_id:
        raise HTTPException(404, "Odoo source workspace not found")
    return context.queries.get(workspace_id)


def _singleton_form(request: Request, form: FormData, allowed: set[str], repeated=()):
    _secure_form(request, form, allowed)
    for name in allowed - set(repeated):
        if len(form.getlist(name)) > 1:
            raise HTTPException(422, "Repeated form fields are not accepted")


def _source_rows(context: WebContext, workspace_id: str, settings: dict):
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    selection = context.queries.get_source_selection(workspace_id)
    if schema is None:
        return (), (), {}, ()
    captured: dict[str, set[str]] = {}
    if selection is not None:
        for dataset in selection.datasets:
            if isinstance(dataset.source, OdooSourceBinding):
                captured[dataset.source.model] = {
                    column.source_name for column in dataset.columns
                }
    else:
        for plan in context.queries.get_current_odoo_capture_selections(workspace_id):
            captured[plan.model] = set(plan.field_names)
    scope = context.queries.get_current_odoo_relationship_scope(workspace_id)
    scope_candidates = (
        destination_match_scope_candidates(selection, schema, scope)
        if selection is not None
        else {}
    )
    scopes_by_model = (
        {
            dataset.source.model: scope_candidates.get(dataset.dataset_id, ())
            for dataset in selection.datasets
            if isinstance(dataset.source, OdooSourceBinding)
        }
        if selection is not None
        else {}
    )
    if scope is not None:
        for decision in scope.decisions:
            if (
                decision.source_model in captured
                and decision.relation_model in captured
                and decision.action
                in {
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                    OdooRelationshipCaptureAction.PRESERVE_LINKED,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                }
            ):
                captured[decision.source_model].add(decision.field_name)
    rows, key_rows, types, coverage = [], [], {}, []
    for model in schema.models:
        names = captured.get(model.name, set())
        capability = compile_odoo_restore(
            model,
            model,
            source_version=schema.odoo_version,
            destination_version=schema.odoo_version,
            creates_expected=False,
        )
        writable = {item.source.name for item in capability.fields}
        suggested = recommend_business_key(model)
        keys = tuple(settings.get("keys", {}).get(model.name, ())) or (
            suggested.key_fields if suggested else ()
        )
        matching_scopes = tuple(settings.get("scopes", {}).get(model.name, ()))
        names.update(
            name for name, _label in scopes_by_model.get(model.name, ())
            if name in matching_scopes
        )
        rules = {
            item["source_field"]: item
            for item in settings.get("rules", {}).get(model.name, [])
        }
        candidates = []
        for field in model.fields:
            if field.name not in names or (
                field.name not in writable and field.type != "one2many"
            ):
                if field.company_dependent is True:
                    reason = "Company-dependent values are outside the current automatic restore capability."
                elif field.type == "binary":
                    reason = (
                        "Binary content and attachments are outside this saved scope."
                    )
                elif field.readonly:
                    reason = "Odoo supplies this read-only field."
                elif field.computed is True and field.has_inverse is not True:
                    reason = "Odoo generates this field from other values."
                elif field.related is True:
                    reason = "This field is derived from related data."
                elif field.translated is True and field.name not in writable:
                    reason = "Translated values are outside the current automatic restore capability."
                elif field.name not in names:
                    reason = "This field was not selected in the capture configuration."
                else:
                    reason = "Captured for reference; the current field capability does not allow automatic restoration."
                coverage.append(
                    {
                        "model": model.name,
                        "name": field.name,
                        "label": field.label,
                        "reason": reason,
                    }
                )
            if field.name not in names:
                continue
            types[(model.name, field.name)] = field.type
            rule = rules.get(field.name, {})
            rows.append(
                {
                    "model": model.name,
                    "name": field.name,
                    "label": field.label,
                    "type": field.type,
                    "is_identity": field.name in (*keys, *matching_scopes),
                    "policy": rule.get("policy", "use_saved"),
                    "target_name": rule.get("target_field") or field.name,
                    "excluded": rule.get("excluded", False),
                    "link_policy": rule.get("link_policy", "replace"),
                }
            )
            if field.type in {"char", "text", "integer", "selection"}:
                candidates.append({"name": field.name, "label": field.label})
        key_rows.append(
            {
                "model": model.name,
                "label": model.label,
                "selected": keys,
                "fields": tuple(candidates),
                "scope_fields": tuple(
                    {"name": name, "label": label}
                    for name, label in scopes_by_model.get(model.name, ())
                ),
                "selected_scope": matching_scopes[0] if matching_scopes else "",
            }
        )
    return tuple(rows), tuple(key_rows), types, tuple(coverage)


def _page_data(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    selected=None,
    source_workspace_id=None,
    profile_id=None,
    version_number=None,
    job_id=None,
):
    project = _visible_project(request, context, project_id)
    actor = request_identity(request, context.identity_resolver).actor
    service = _service(context)
    definitions = service.repository.list_definitions(project.project_id)
    definition = (
        service.get(project.project_id, selected, actor=actor) if selected else None
    )
    versions = (
        service.repository.list_versions(project.project_id, definition.saved_data_id)
        if definition
        else ()
    )
    profiles = (
        service.repository.list_restore_profiles(
            project.project_id, definition.saved_data_id
        )
        if definition
        else ()
    )
    profile = (
        service.repository.get_restore_profile(
            project.project_id, definition.saved_data_id, profile_id
        )
        if definition and profile_id
        else None
    )
    settings = json.loads(profile.settings_json) if profile else {}
    source_id = source_workspace_id or (
        definition.source_workspace_id if definition else None
    )
    source = (
        _owned_source(context, project.project_id, source_id, actor)
        if source_id
        else None
    )
    version = None
    chosen_version = (
        version_number
        if version_number is not None
        else definition.latest_version
        if definition
        else None
    )
    if definition and chosen_version is not None:
        version = service.repository.get_version(
            project.project_id,
            definition.saved_data_id,
            _positive(str(chosen_version), "saved data version"),
        )
    evidence_workspace = (
        version.workspace_id if version else source.workspace_id if source else None
    )
    field_rows, key_rows, types, coverage = (
        _source_rows(context, evidence_workspace, settings)
        if evidence_workspace
        else ((), (), {}, ())
    )
    job = (
        context.saved_odoo_jobs.get(
            project.project_id, definition.saved_data_id, job_id
        )
        if job_id and definition and context.saved_odoo_jobs
        else None
    )
    values = (
        {
            "profile_id": profile.profile_id,
            "odoo_connection_mode": profile.connection_mode,
            "odoo_base_url": profile.base_url,
            "odoo_database": profile.database,
            "destination_company_name": profile.company_name,
        }
        if profile
        else {}
    )
    if version:
        values["version_number"] = str(version.version_number)
    return {
        "project": project,
        "definitions": definitions,
        "definition": definition,
        "versions": versions,
        "profiles": profiles,
        "source_workspace": source,
        "source_companies": (),
        "field_rows": field_rows,
        "key_rows": key_rows,
        "job": job,
        "error": None,
        "operation_id": getattr(job, "operation_id", "")
        if job
        and job.status == "FAILED"
        and job.kind == "UPDATE"
        and getattr(job, "operation_id", "")
        else str(uuid4()),
        "restore_values": values,
        "schema_issues": (),
        "default_rows": (),
        "coverage_rows": coverage[:500],
        "coverage_total": len(coverage),
        "configuration_values": {
            "all_fields": json.loads(definition.config_json).get("all_fields", False)
        }
        if definition
        else {},
        "_field_types": types,
        "_profile_hash": profile.content_hash if profile else "",
    }


def _render_page(request: Request, values: dict, *, status_code=200):
    public = {key: value for key, value in values.items() if not key.startswith("_")}
    return _render(
        request, "project_saved_odoo_data.html", status_code=status_code, **public
    )


def _safe_values(form: FormData) -> dict[str, str]:
    return {
        key: value
        for key, value in form.items()
        if isinstance(value, str)
        and key not in {"csrf_token", "api_key", "package_password", "confirm_restore"}
    }


def _restore_form_values(form: FormData, field_rows) -> dict[str, object]:
    values: dict[str, object] = dict(_safe_values(form))
    for row in field_rows:
        name = f"exclude__{row['model']}__{row['name']}"
        values[name] = _text(form, name) == "yes"
    return values


def _temporary():
    root = Path(tempfile.gettempdir()) / "impodo-trials"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="saved-odoo-", dir=root)


def _default_value(value: str, field_type: str, label: str):
    if field_type == "boolean":
        if value not in {"true", "false"}:
            raise WorkspaceError(f"Choose Yes or No for {label}")
        return value == "true"
    if field_type in {"integer", "float", "monetary"}:
        try:
            number = int(value) if field_type == "integer" else float(value)
        except ValueError as error:
            raise WorkspaceError(f"Enter a valid number for {label}") from error
        if isinstance(number, float) and not math.isfinite(number):
            raise WorkspaceError(f"Enter a finite number for {label}")
        return number
    if field_type in {"many2one", "many2many", "one2many"}:
        raise WorkspaceError(
            f"Choose the required related record for {label} in destination setup, then check again"
        )
    return value


def _restore_fields(values: dict) -> tuple[set[str], set[str]]:
    allowed, repeated = set(_BASE_RESTORE_FIELDS), set()
    for row in values["field_rows"]:
        suffix = f"{row['model']}__{row['name']}"
        allowed.update(
            {
                f"policy__{suffix}",
                f"links__{suffix}",
                f"target__{suffix}",
                f"exclude__{suffix}",
            }
        )
    for row in values["key_rows"]:
        name = f"keys__{row['model']}"
        allowed.add(name)
        allowed.add(f"scope__{row['model']}")
        repeated.add(name)
    for row in values["default_rows"]:
        suffix = f"{row['model']}__{row['name']}"
        allowed.update({f"default_provider__{suffix}", f"default_value__{suffix}"})
    return allowed, repeated


def _profile_from_form(
    context: WebContext,
    project_id: str,
    saved_data_id: str,
    form: FormData,
    values: dict,
) -> SavedOdooRestoreProfile:
    repository = _service(context).repository
    profile_id = _text(form, "profile_id")
    previous = (
        repository.get_restore_profile(project_id, saved_data_id, profile_id)
        if profile_id
        else None
    )
    settings = json.loads(previous.settings_json) if previous else {}
    policy = RestoreValuePolicy(_text(form, "policy_default") or "use_saved")
    keys = dict(settings.get("keys", {}))
    scopes = dict(settings.get("scopes", {}))
    for row in values["key_rows"]:
        name = f"keys__{row['model']}"
        submitted = tuple(str(value) for value in form.getlist(name))
        if submitted:
            candidates = {field["name"] for field in row["fields"]}
            if (
                len(submitted) > 3
                or len(set(submitted)) != len(submitted)
                or not set(submitted) <= candidates
            ):
                raise WorkspaceError(
                    "Choose up to three distinct captured matching fields for each model"
                )
            keys[row["model"]] = submitted
        elif row["selected"]:
            keys[row["model"]] = row["selected"]
        scope_name = f"scope__{row['model']}"
        if scope_name in form:
            selected_scope = _text(form, scope_name)
            if selected_scope and selected_scope not in {
                field["name"] for field in row.get("scope_fields", ())
            }:
                raise WorkspaceError("Choose an available parent or company for matching")
            scopes[row["model"]] = (selected_scope,) if selected_scope else ()
    rules: dict[str, list[dict[str, object]]] = {}
    for row in values["field_rows"]:
        model, field = row["model"], row["name"]
        suffix = f"{model}__{field}"
        target = (
            _text(form, f"target__{suffix}").strip() or row.get("target_name") or field
        )
        is_identity = field in (*keys.get(model, ()), *scopes.get(model, ()))
        rule = RestoreFieldRule(
            field,
            target_field=target if target != field else None,
            policy=RestoreValuePolicy.USE_SAVED
            if is_identity
            else RestoreValuePolicy(_text(form, f"policy__{suffix}") or policy.value),
            link_policy=RestoreLinkPolicy.REPLACE
            if is_identity
            else RestoreLinkPolicy(
                _text(form, f"links__{suffix}") or row.get("link_policy", "replace")
            ),
            excluded=_text(form, f"exclude__{suffix}") == "yes",
        )
        if is_identity and (rule.excluded or rule.target_field is not None):
            raise WorkspaceError(
                "Matching fields cannot be excluded or renamed by an ordinary field policy"
            )
        rules.setdefault(model, []).append(rule.to_dict())
    defaults = dict(settings.get("defaults", {}))
    for row in values["default_rows"]:
        model, field = row["model"], row["name"]
        suffix = f"{model}__{field}"
        provider = _text(form, f"default_provider__{suffix}")
        if not provider:
            continue
        if provider not in {"fixed_value", "odoo_default"}:
            raise WorkspaceError("Choose an available provider for each required field")
        if provider == "odoo_default" and not row.get("has_odoo_default"):
            raise WorkspaceError("This required field has no checked Odoo default")
        default = (
            None
            if provider == "odoo_default"
            else _default_value(
                _text(form, f"default_value__{suffix}"),
                row.get("type", "char"),
                row["label"],
            )
        )
        defaults.setdefault(model, {})[field] = {"provider": provider, "value": default}
    now = datetime.now(UTC)
    database = _text(form, "odoo_database") or (previous.database if previous else "")
    company = _text(form, "destination_company_name") or (
        previous.company_name if previous else ""
    )
    return SavedOdooRestoreProfile(
        profile_id=previous.profile_id if previous else str(uuid4()),
        project_id=project_id,
        saved_data_id=saved_data_id,
        label=previous.label if previous else f"{database} · {company}"[:300],
        revision=previous.revision if previous else 1,
        connection_mode=_text(form, "odoo_connection_mode")
        or (previous.connection_mode if previous else "REMOTE"),
        base_url=_text(form, "odoo_base_url")
        or (previous.base_url if previous else ""),
        database=database,
        company_name=company,
        credential_workspace_id=previous.credential_workspace_id if previous else None,
        settings_json=canonical_json(
            {"rules": rules, "defaults": defaults, "keys": keys, "scopes": scopes}
        ),
        created_at=previous.created_at if previous else now,
        updated_at=now,
    )


def build_saved_odoo_data_router(context: WebContext) -> APIRouter:
    """Build project-scoped capture, refresh, restore, and package endpoints."""

    router = APIRouter()
    # This bounded cache contains checked field metadata, never credentials or
    # connections. A new runtime check still validates every requested write.
    checked_reviews: OrderedDict[tuple[str, str, str, int], dict] = OrderedDict()

    def page(request, project_id, **options):
        values = _page_data(request, context, project_id, **options)
        definition = values["definition"]
        profile_id = values["restore_values"].get("profile_id", "")
        number = int(values["restore_values"].get("version_number", "0"))
        if definition and profile_id:
            review = checked_reviews.get(
                (project_id, definition.saved_data_id, profile_id, number)
            )
            if review and review.get("_profile_hash") == values["_profile_hash"]:
                values.update(review)
        return values

    @router.get("/projects/{project_id}/saved-odoo-data")
    async def saved_data_page(
        request: Request,
        project_id: str,
        selected: str | None = None,
        profile: str | None = None,
        source_workspace_id: str | None = None,
        version: int | None = None,
        job: str | None = None,
        unchanged: str | None = None,
    ):
        require_session(request)
        values = await run_page_read(
            page,
            request,
            project_id,
            selected=selected,
            profile_id=profile,
            source_workspace_id=source_workspace_id,
            version_number=version,
            job_id=job,
        )
        if source_workspace_id:
            actor = request_identity(request, context.identity_resolver).actor
            try:
                values["source_companies"] = await run_in_threadpool(
                    saved_source_companies, context, source_workspace_id, actor=actor
                )
            except _SAFE_ERRORS as error:
                values["error"] = str(error)
        if unchanged == "yes":
            _flash(
                request,
                "The destination already matches this saved data version. No records needed changing.",
            )
        return await run_page_read(_render_page, request, values)

    @router.post("/projects/{project_id}/saved-odoo-data/configure")
    async def configure_saved_data(request: Request, project_id: str):
        form = await request.form()
        _singleton_form(
            request,
            form,
            {
                "csrf_token",
                "workspace_id",
                "label",
                "source_company_id",
                "all_fields",
                "operation_id",
                "saved_data_id",
                "revision",
            },
        )
        actor = request_identity(request, context.identity_resolver).actor
        workspace_id = _text(form, "workspace_id")
        await run_local_operation(_visible_project, request, context, project_id)
        await run_local_operation(
            _owned_source, context, project_id, workspace_id, actor
        )
        selected = _text(form, "saved_data_id") or None
        companies = ()
        try:
            companies = await run_in_threadpool(
                saved_source_companies, context, workspace_id, actor=actor
            )
            matches = tuple(
                item
                for item in companies
                if str(item.company_id) == _text(form, "source_company_id")
            )
            if len(matches) != 1:
                raise WorkspaceError(
                    "Choose one company fetched from the source Odoo database"
                )
            service = _service(context)
            if selected:
                state = await run_page_read(
                    _owned_source, context, project_id, workspace_id, actor
                )
                if state.odoo_company != matches[0]:
                    raise WorkspaceError(
                        "Save a separate configuration for another source company"
                    )
                definition = await run_local_operation(
                    service.revise,
                    project_id,
                    selected,
                    workspace_id,
                    actor=actor,
                    expected_revision=_revision(form),
                    label=_text(form, "label"),
                    all_fields="all_fields" in form,
                )
            else:
                definition = await run_in_threadpool(
                    service.configure,
                    workspace_id,
                    actor=actor,
                    label=_text(form, "label"),
                    source_company=matches[0],
                    all_fields="all_fields" in form,
                    operation_id=require_uuid(
                        _text(form, "operation_id"), "operation_id"
                    ),
                    access_factory=lambda fresh, base: saved_capture_access(
                        context, fresh, base, actor=actor
                    ),
                )
        except _SAFE_ERRORS as error:
            values = await run_page_read(
                page,
                request,
                project_id,
                selected=selected,
                source_workspace_id=workspace_id,
            )
            submitted = _safe_values(form)
            submitted["all_fields"] = "yes" if "all_fields" in form else ""
            values.update(
                error=str(error),
                configuration_values=submitted,
                source_companies=companies,
            )
            return await run_page_read(_render_page, request, values, status_code=422)
        _flash(
            request,
            "Capture configuration saved. Use Update saved data to download its records.",
        )
        return RedirectResponse(_location(project_id, definition.saved_data_id), 303)

    @router.post("/projects/{project_id}/saved-odoo-data/{saved_data_id}/update")
    async def update_saved_data(request: Request, project_id: str, saved_data_id: str):
        form = await request.form()
        _singleton_form(request, form, {"csrf_token", "revision", "operation_id"})
        actor = request_identity(request, context.identity_resolver).actor

        def prepare():
            _visible_project(request, context, project_id)
            service = _service(context)
            definition = service.get(project_id, saved_data_id, actor=actor)
            context.workspace_access.require(
                actor,
                Capability.SOURCE_CAPTURE,
                workspace_id=definition.source_workspace_id,
            )
            if definition.revision != _revision(form):
                raise WorkspaceError(
                    "The configuration changed. Reload before updating saved data."
                )
            return service

        try:
            service = await run_local_operation(prepare)
            operation_id = require_uuid(_text(form, "operation_id"), "operation_id")
        except _SAFE_ERRORS as error:
            values = await run_page_read(
                page, request, project_id, selected=saved_data_id
            )
            values["error"] = str(error)
            return await run_page_read(_render_page, request, values, status_code=422)
        jobs = context.saved_odoo_jobs
        if jobs is None:
            raise HTTPException(503, "Saved Odoo data actions are unavailable")

        def work(progress):
            service.refresh(
                project_id,
                saved_data_id,
                actor=actor,
                expected_revision=_revision(form),
                operation_id=operation_id,
                access_factory=lambda fresh, base: saved_capture_access(
                    context, fresh, base, actor=actor
                ),
                progress=progress,
            )
            return _location(project_id, saved_data_id)

        job = jobs.enqueue(
            project_id,
            saved_data_id,
            kind="UPDATE",
            work=work,
            operation_id=operation_id,
        )
        return RedirectResponse(
            _location(project_id, saved_data_id, job=job.job_id), 303
        )

    def remember_check(project_id, saved_data_id, version_number, result):
        service = _service(context)
        profile = service.repository.get_restore_profile(
            project_id, saved_data_id, result.profile_id
        )
        review = {
            "_profile_hash": profile.content_hash,
            "schema_issues": tuple({"message": message} for message in result.issues),
        }
        for name in ("field_rows", "default_rows"):
            rows = getattr(result, name, ())
            if rows:
                review[name] = rows
        checked_reviews[
            (project_id, saved_data_id, profile.profile_id, version_number)
        ] = review
        while len(checked_reviews) > 64:
            checked_reviews.popitem(last=False)

    @router.post("/projects/{project_id}/saved-odoo-data/{saved_data_id}/restore")
    async def restore_saved_data(request: Request, project_id: str, saved_data_id: str):
        form = await request.form()
        require_csrf(request, _text(form, "csrf_token"))
        actor = request_identity(request, context.identity_resolver).actor
        version_number = _positive(_text(form, "version_number"), "saved data version")
        profile_id = _text(form, "profile_id") or None
        values = await run_page_read(
            page,
            request,
            project_id,
            selected=saved_data_id,
            profile_id=profile_id,
            version_number=version_number,
        )
        allowed, repeated = _restore_fields(values)
        _singleton_form(request, form, allowed, repeated)
        action = _text(form, "action")
        if action not in {"check", "restore"}:
            raise HTTPException(422, "Choose Check destination or Restore all")
        context.authorization.require(
            actor,
            Capability.PREFLIGHT_RUN
            if action == "check"
            else Capability.EXPORT_PLAN_EXECUTE,
            project_id=project_id,
        )
        if action == "restore" and _text(form, "confirm_restore") != "yes":
            raise HTTPException(
                422, "Confirm the destination and authorize Restore all"
            )
        try:
            operation_id = require_uuid(_text(form, "operation_id"), "operation_id")
            profile = await run_local_operation(
                _profile_from_form, context, project_id, saved_data_id, form, values
            )
            api_key = _text(form, "api_key")
            if action == "check":
                result = await run_in_threadpool(
                    restore_saved_odoo_data,
                    context,
                    project_id,
                    saved_data_id,
                    version_number,
                    actor=actor,
                    operation_id=operation_id,
                    profile=profile,
                    api_key=api_key,
                    progress=lambda _percent, _message: None,
                    check_only=True,
                )
                await run_local_operation(
                    remember_check, project_id, saved_data_id, version_number, result
                )
                values = await run_page_read(
                    page,
                    request,
                    project_id,
                    selected=saved_data_id,
                    profile_id=result.profile_id,
                    version_number=version_number,
                )
                if not result.issues:
                    _flash(
                        request,
                        "Destination checked. Review the choices, then confirm Restore all.",
                    )
                return await run_page_read(
                    _render_page,
                    request,
                    values,
                    status_code=422 if result.issues else 200,
                )
            jobs = context.saved_odoo_jobs
            if jobs is None:
                raise HTTPException(503, "Saved Odoo data actions are unavailable")

            def work(progress):
                result = restore_saved_odoo_data(
                    context,
                    project_id,
                    saved_data_id,
                    version_number,
                    actor=actor,
                    operation_id=operation_id,
                    profile=profile,
                    api_key=api_key,
                    progress=progress,
                )
                if not result.verified:
                    from impodo.web.composition.page_reads import (
                        run_local_operation_sync,
                    )

                    run_local_operation_sync(
                        remember_check,
                        project_id,
                        saved_data_id,
                        version_number,
                        result,
                    )
                    raise SavedOdooActionError(
                        "The restore needs destination decisions. Review the saved check before retrying.",
                        redirect_url=result.redirect_url,
                    )
                return result.redirect_url

            job = jobs.enqueue(project_id, saved_data_id, kind="RESTORE", work=work)
            return RedirectResponse(
                _location(project_id, saved_data_id, job=job.job_id), 303
            )
        except _SAFE_ERRORS as error:
            message = str(error)
            if _text(form, "api_key") and _text(form, "api_key") in message:
                message = "The destination check could not complete. Review its connection and access, then try again."
            values.update(
                error=message,
                restore_values=_restore_form_values(form, values["field_rows"]),
            )
            values["key_rows"] = tuple(
                {
                    **row,
                    "selected": tuple(form.getlist(f"keys__{row['model']}"))
                    or row["selected"],
                }
                for row in values["key_rows"]
            )
            return await run_page_read(_render_page, request, values, status_code=422)

    @router.post(
        "/projects/{project_id}/saved-odoo-data/{saved_data_id}/versions/{version_number}/download"
    )
    async def download_saved_data(
        request: Request, project_id: str, saved_data_id: str, version_number: int
    ):
        form = await request.form()
        _singleton_form(request, form, {"csrf_token", "package_password"})
        await run_page_read(_visible_project, request, context, project_id)
        archive = context.saved_odoo_archive
        if archive is None:
            raise HTTPException(503, "Saved data package download is unavailable")
        actor = request_identity(request, context.identity_resolver).actor
        temporary = _temporary()
        transferred = False
        try:
            destination = Path(temporary.name) / "saved.impodo"
            path = await run_in_threadpool(
                archive.export_version,
                project_id,
                saved_data_id,
                version_number,
                destination=destination,
                password=_text(form, "package_password"),
                actor=actor,
            )
            response = FileResponse(
                path,
                filename=f"saved-odoo-v{version_number}.impodo",
                media_type="application/octet-stream",
                background=BackgroundTask(temporary.cleanup),
            )
            transferred = True
            return response
        except _SAFE_ERRORS:
            values = await run_page_read(
                page, request, project_id, selected=saved_data_id
            )
            values["error"] = (
                "The saved package could not be created. Check its password and retained data, then try again."
            )
            return await run_page_read(_render_page, request, values, status_code=422)
        finally:
            if not transferred:
                temporary.cleanup()

    @router.post("/projects/{project_id}/saved-odoo-data/import")
    async def import_saved_data(request: Request, project_id: str):
        form = await request.form(max_files=1, max_fields=4)
        _singleton_form(
            request, form, {"csrf_token", "package_file", "package_password"}
        )
        await run_page_read(_visible_project, request, context, project_id)
        archive = context.saved_odoo_archive
        if archive is None:
            raise HTTPException(503, "Saved data package import is unavailable")
        actor = request_identity(request, context.identity_resolver).actor
        upload = form.get("package_file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(422, "Choose a saved Odoo data package")
        for capability in (Capability.SOURCE_CONFIGURE, Capability.DATA_VERSION_CREATE):
            context.authorization.require(actor, capability, project_id=project_id)
        try:
            with _temporary() as directory:
                path = Path(directory) / "upload.impodo"
                size = 0
                with path.open("xb") as target:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > _UPLOAD_LIMIT:
                            raise HTTPException(
                                413,
                                "The saved data package exceeds the upload size limit",
                            )
                        await run_in_threadpool(target.write, chunk)
                if not size:
                    raise WorkspaceError("Choose a non-empty saved data package")
                definition = await run_in_threadpool(
                    archive.import_version,
                    project_id,
                    path,
                    password=_text(form, "package_password"),
                    actor=actor,
                )
        except _SAFE_ERRORS:
            values = await run_page_read(page, request, project_id)
            values["error"] = (
                "The saved package could not be opened. Check its password and use a complete package downloaded from Impodo."
            )
            return await run_page_read(_render_page, request, values, status_code=422)
        finally:
            await upload.close()
        _flash(
            request,
            "Saved data package imported. Choose its destination to restore it.",
        )
        return RedirectResponse(_location(project_id, definition.saved_data_id), 303)

    return router
