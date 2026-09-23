"""Stage 5 destination matching for frozen Odoo-source transfers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from math import isfinite

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
    destination_match_key_candidates,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.mapping.create_field_policy import CREATE_FIXED_VALUE_TYPES
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.destination_matching import (
    carry_destination_create_field_reviews,
    choose_destination_create_field_provider,
    confirm_destination_create_field_defaults,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateError,
    transfer_destination_workspace,
)

from ..context import WebContext
from ..forms import _revision, _secure_form
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    get_target_credential_status,
)


def _matching_evidence(context: WebContext, workspace_id: str):
    selection = context.queries.get_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if selection is None or schema is None:
        raise WorkspaceError("Freeze the Odoo source before matching the destination")
    return selection, schema


def _choice_value(dataset_id: str, source_column_key: str) -> str:
    return f"{dataset_id}::{source_column_key}"


def _parse_choices(form) -> tuple[DestinationMatchKeyChoice, ...]:
    choices: list[DestinationMatchKeyChoice] = []
    extras: dict[str, list[str]] = {}
    for raw in form.getlist("match_key_extra"):
        if not raw:
            continue
        dataset_id, separator, source_column_key = str(raw).partition("::")
        if not separator or not dataset_id or not source_column_key:
            raise WorkspaceStateError("Choose current matching fields")
        extras.setdefault(dataset_id, []).append(source_column_key)
    for raw in form.getlist("match_key"):
        dataset_id, separator, source_column_key = str(raw).partition("::")
        if not separator or not dataset_id or not source_column_key:
            raise WorkspaceStateError("Choose one matching field for each source table")
        choices.append(
            DestinationMatchKeyChoice(
                dataset_id=dataset_id,
                source_column_key=source_column_key,
                additional_source_column_keys=tuple(extras.pop(dataset_id, ())),
            )
        )
    if extras:
        raise WorkspaceStateError("Matching fields do not belong to a selected table")
    return tuple(choices)


def _matching_rows(workspace_state, selection, schema):
    candidates = destination_match_key_candidates(selection, schema)
    plan = workspace_state.destination_match_plan
    selected_by_dataset = {
        item.dataset_id: item.source_column_keys
        for item in plan.model_matches
    } if plan is not None else {}
    result_by_dataset = {
        item.dataset_id: item for item in plan.model_matches
    } if plan is not None else {}
    rows = []
    for dataset in selection.datasets:
        model = (
            dataset.source.model
            if isinstance(dataset.source, OdooSourceBinding)
            else ""
        )
        available = tuple(
            {
                "stable_key": stable_key,
                "field_name": field_name,
                "label": label,
                "value": _choice_value(dataset.dataset_id, stable_key),
            }
            for stable_key, field_name, label in candidates.get(dataset.dataset_id, ())
        )
        text_fields = {
            field.name
            for schema_model in schema.models if schema_model.name == model
            for field in schema_model.fields if field.type in {"char", "text", "selection"}
        }
        primary_candidates = tuple(
            item for item in available if item["field_name"] in text_fields
        )
        selected_keys = selected_by_dataset.get(dataset.dataset_id, ())
        if not selected_keys or selected_keys[0] not in {
            item["stable_key"] for item in primary_candidates
        }:
            selected_keys = (
                (primary_candidates[0]["stable_key"],)
                if primary_candidates
                else ()
            )
        rows.append(
            {
                "dataset": dataset,
                "model": model,
                "candidates": available,
                "primary_candidates": primary_candidates,
                "selected_keys": selected_keys,
                "result": result_by_dataset.get(dataset.dataset_id),
            }
        )
    return tuple(rows)


def _render_matching(
    request: Request,
    context: WebContext,
    workspace_state,
    selection,
    schema,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    current = workspace_state.destination_match_current(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    ready = bool(
        current
        and workspace_state.destination_match_plan is not None
        and workspace_state.destination_match_plan.ready
    )
    rows = _matching_rows(workspace_state, selection, schema)
    create_field_rows: dict[str, list[dict[str, object]]] = {}
    create_field_evidence_error = None
    plan = workspace_state.destination_match_plan
    if plan is not None and plan.create_field_evidence_id is not None:
        try:
            access = context.workspace_access.resolve(
                workspace_state.workspace_id,
                actor=context.actor,
                capability=Capability.PROTECTED_EVIDENCE_READ,
            )
            protected = context.destination_create_fields.read(
                access.project_id,
                plan,
            )
            protected_by_key = {
                item.key: item for item in (protected.values if protected else ())
            }
            decisions_by_key = {
                item.key: item for item in plan.create_field_decisions
            }
            labels_by_model = {
                item.model: item.model_label for item in plan.model_matches
            }
            for protected_value in protected_by_key.values():
                decision = decisions_by_key.get(protected_value.key)
                create_field_rows.setdefault(protected_value.dataset_id, []).append(
                    {
                        "decision": decision,
                        "evidence": protected_value,
                        "model_label": labels_by_model.get(
                            protected_value.model, protected_value.model
                        ),
                        "value": protected_value.display_value or "Needs decision",
                        "form_value": (
                            f"{protected_value.model}::{protected_value.field_name}"
                        ),
                        "fixed_supported": (
                            protected_value.field_type in CREATE_FIXED_VALUE_TYPES
                        ),
                        "reference_supported": bool(
                            protected_value.reference_candidates
                        ),
                        "incoming_reference_supported": bool(
                            protected_value.incoming_reference_candidates
                        ),
                    }
                )
        except (PermissionError, WorkspaceError) as evidence_error:
            create_field_evidence_error = str(evidence_error)
    return _render(
        request,
        "workspace_destination_matching.html",
        workspace_state=workspace_state,
        source_selection=selection,
        source_schema=schema,
        matching_rows=rows,
        matching_can_check=bool(rows) and all(row["primary_candidates"] for row in rows),
        match_plan=workspace_state.destination_match_plan,
        match_plan_current=current,
        match_plan_ready=ready,
        match_plan_create_defaults_pending=bool(
            plan is not None and not plan.create_field_defaults_complete
        ),
        match_plan_stale=(
            workspace_state.destination_match_plan is not None and not current
        ),
        destination_credential_status=get_target_credential_status(
            context.secret_store,
            workspace_state,
            TargetCredentialRole.DESTINATION_TRANSFER,
        ),
        disable_default_read_credential_prompt=True,
        create_field_rows=create_field_rows,
        default_create_field_rows={
            dataset_id: [
                item for item in items
                if item["decision"] is not None
                and item["decision"].provider_kind == "odoo_default"
            ]
            for dataset_id, items in create_field_rows.items()
            if any(
                item["decision"] is not None
                and item["decision"].provider_kind == "odoo_default"
                for item in items
            )
        },
        create_default_evidence_error=create_field_evidence_error,
        error=error,
        status_code=status_code,
    )


def build_destination_matching_router(context: WebContext) -> APIRouter:
    router = APIRouter()
    service = DestinationMatchingService(context.categorical_coverage)

    @router.get(
        "/workspaces/{workspace_id}/destination-matching",
        response_class=HTMLResponse,
    )
    async def destination_matching_form(request: Request, workspace_id: str):
        require_session(request)
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.source_mode is not SourceMode.ODOO
            or not workspace_state.destination_verified
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-destination",
                status_code=303,
            )
        try:
            selection, schema = _matching_evidence(context, workspace_id)
        except WorkspaceError:
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        return _render_matching(
            request,
            context,
            workspace_state,
            selection,
            schema,
        )

    @router.post("/workspaces/{workspace_id}/destination-matching")
    async def check_destination_matching(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "match_key", "match_key_extra"},
        )
        workspace_state = context.queries.get(workspace_id)
        if (
            workspace_state.source_mode is not SourceMode.ODOO
            or not workspace_state.destination_verified
        ):
            return RedirectResponse(
                f"/workspaces/{workspace_id}/transfer-destination",
                status_code=303,
            )
        try:
            selection, schema = _matching_evidence(context, workspace_id)
        except WorkspaceError as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        try:
            expected_revision = _revision(form)
            if expected_revision != workspace_state.revision:
                raise WorkspaceStateError(
                    "The workspace changed in another request; reload before continuing"
                )
            choices = _parse_choices(form)
            credential = get_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.DESTINATION_TRANSFER,
            )
            if credential is None:
                raise SecretStoreError(
                    "Return to the destination connection and enter its transfer key"
                )
            models = tuple(
                sorted(
                    item.source.model
                    for item in selection.datasets
                    if isinstance(item.source, OdooSourceBinding)
                )
            )
            destination = replace(
                transfer_destination_workspace(workspace_state),
                intended_models=models,
            )
            identity = await run_in_threadpool(
                context.read_identity_probe,
                destination,
                credential.secret,
                models,
            )
            source_origins = {}
            for dataset in selection.datasets:
                protected = await run_in_threadpool(
                    context.odoo_provenance.read_current_origins,
                    workspace_id,
                    actor=context.actor,
                    dataset_id=dataset.dataset_id,
                )
                if protected is not None:
                    source_origins[dataset.dataset_id] = protected[1]
            plan = await run_in_threadpool(
                service.check,
                workspace_state,
                selection,
                schema,
                choices,
                api_key=credential.secret,
                credential_binding_hash=credential.binding_hash,
                read_identity=identity,
                reader=context.destination_match_reader,
                recorded_by=context.actor.identity.display_name,
                source_origins=source_origins,
            )
            previous = workspace_state.destination_match_plan
            access = None
            if previous is not None:
                previous_evidence = None
                if previous.create_field_evidence_id is not None:
                    access = context.workspace_access.resolve(
                        workspace_id,
                        actor=context.actor,
                        capability=Capability.PROTECTED_EVIDENCE_MANAGE,
                    )
                    previous_evidence = await run_in_threadpool(
                        context.destination_create_fields.read,
                        access.project_id,
                        previous,
                    )
                plan = carry_destination_create_field_reviews(
                    plan,
                    previous,
                    previous_evidence,
                )
            if plan.create_field_evidence is not None and access is None:
                access = context.workspace_access.resolve(
                    workspace_id,
                    actor=context.actor,
                    capability=Capability.PROTECTED_EVIDENCE_MANAGE,
                )
            plan = await run_in_threadpool(
                context.destination_create_fields.put,
                access.project_id if access is not None else "",
                plan,
            )
            workspace_state = context.workspace_states.save_destination_match_plan(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                plan=plan,
            )
        except (
            ConnectorError,
            SecretStoreError,
            PermissionError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            return _render_matching(
                request,
                context,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            (
                "Destination matching is ready. Nothing was changed in Odoo."
                if workspace_state.destination_match_plan.ready
                else "Destination matching was checked. Review the blockers below."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/destination-matching#matching-results",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/destination-matching/defaults")
    async def confirm_destination_defaults(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "revision", "match_plan_hash", "default_field"},
        )
        workspace_state = context.queries.get(workspace_id)
        try:
            selection, schema = _matching_evidence(context, workspace_id)
        except WorkspaceError as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        try:
            expected_revision = _revision(form)
            plan = workspace_state.destination_match_plan
            if (
                expected_revision != workspace_state.revision
                or plan is None
                or str(form.get("match_plan_hash", "")) != plan.content_hash
                or not workspace_state.destination_match_current(
                    source_selection_hash=selection.content_hash,
                    source_schema_hash=schema.content_hash,
                )
            ):
                raise WorkspaceStateError(
                    "The destination matching check changed. Reload before confirming defaults."
                )
            selected: set[tuple[str, str]] = set()
            for raw in form.getlist("default_field"):
                model, separator, field_name = str(raw).partition("::")
                if not separator or not model or not field_name:
                    raise WorkspaceStateError("Choose the current Odoo defaults")
                selected.add((model, field_name))
            access = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PROTECTED_EVIDENCE_MANAGE,
            )
            evidence = await run_in_threadpool(
                context.destination_create_fields.read,
                access.project_id,
                plan,
            )
            if evidence is None:
                raise WorkspaceStateError("No Odoo defaults are waiting for review")
            confirmed = confirm_destination_create_field_defaults(
                plan,
                evidence,
                selected,
            )
            workspace_state = context.workspace_states.save_destination_match_plan(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                plan=confirmed,
            )
        except (PermissionError, WorkspaceError, WorkspaceStateError, ValueError) as error:
            return _render_matching(
                request,
                context,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "The current destination defaults are confirmed for new records only.",
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/destination-matching#matching-results",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/destination-matching/create-fields")
    async def choose_destination_create_field(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token", "revision", "match_plan_hash", "field_key",
                "provider", "source_field", "fixed_value",
                "reference_choice", "incoming_reference_choice",
            },
        )
        workspace_state = context.queries.get(workspace_id)
        try:
            selection, schema = _matching_evidence(context, workspace_id)
        except WorkspaceError as error:
            _flash(request, str(error))
            return RedirectResponse(
                f"/workspaces/{workspace_id}/sources",
                status_code=303,
            )
        try:
            expected_revision = _revision(form)
            plan = workspace_state.destination_match_plan
            if (
                expected_revision != workspace_state.revision
                or plan is None
                or str(form.get("match_plan_hash", "")) != plan.content_hash
                or not workspace_state.destination_match_current(
                    source_selection_hash=selection.content_hash,
                    source_schema_hash=schema.content_hash,
                )
            ):
                raise WorkspaceStateError(
                    "The destination matching check changed. Reload before choosing a value."
                )
            model, separator, field_name = str(form.get("field_key", "")).partition("::")
            if not separator or not model or not field_name:
                raise WorkspaceStateError("Choose a current required field")
            provider = str(form.get("provider", ""))
            access = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PROTECTED_EVIDENCE_MANAGE,
            )
            evidence = await run_in_threadpool(
                context.destination_create_fields.read,
                access.project_id,
                plan,
            )
            if evidence is None:
                raise WorkspaceStateError("No create-only field is waiting for review")
            protected = next(
                (item for item in evidence.values if item.key == (model, field_name)),
                None,
            )
            if protected is None:
                raise WorkspaceStateError("The required field is no longer current")
            chosen = choose_destination_create_field_provider(
                plan,
                evidence,
                model=model,
                field_name=field_name,
                provider_kind=provider,
                fixed_value=(
                    _parse_fixed_create_value(
                        protected.field_type,
                        str(form.get("fixed_value", "")),
                    )
                    if provider == "fixed_value"
                    else None
                ),
                source_field_name=(
                    str(form.get("source_field", ""))
                    if provider == "source_field"
                    else None
                ),
                reference_choice_hash=(
                    str(form.get("reference_choice", ""))
                    if provider == "existing_reference"
                    else None
                ),
                incoming_reference_choice_hash=(
                    str(form.get("incoming_reference_choice", ""))
                    if provider == "incoming_reference"
                    else None
                ),
            )
            chosen = await run_in_threadpool(
                context.destination_create_fields.put,
                access.project_id,
                chosen,
            )
            context.workspace_states.save_destination_match_plan(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                plan=chosen,
            )
        except (PermissionError, WorkspaceError, WorkspaceStateError, ValueError) as error:
            return _render_matching(
                request,
                context,
                context.queries.get(workspace_id),
                selection,
                schema,
                error=str(error),
                status_code=422,
            )
        _flash(request, "The create-only field choice is saved for new records.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/destination-matching#matching-results",
            status_code=303,
        )

    return router


def _parse_fixed_create_value(field_type: str, raw: str):
    if field_type == "boolean":
        if raw not in {"true", "false"}:
            raise ValueError("Choose Yes or No")
        return raw == "true"
    if field_type == "integer":
        try:
            return int(raw)
        except ValueError as error:
            raise ValueError("Enter a whole number") from error
    if field_type in {"float", "monetary"}:
        try:
            value = float(raw)
        except ValueError as error:
            raise ValueError("Enter a number") from error
        if not isfinite(value):
            raise ValueError("Enter a finite number")
        return value
    if field_type == "date":
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as error:
            raise ValueError("Enter a valid date") from error
    if field_type == "datetime":
        try:
            return datetime.fromisoformat(raw).isoformat(sep=" ")
        except ValueError as error:
            raise ValueError("Enter a valid date and time") from error
    if field_type in {"char", "text", "html", "selection"} and raw.strip():
        return raw
    raise ValueError("Enter a value supported by this destination field")
