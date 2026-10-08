"""Destination matching for frozen Odoo-source transfers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from math import isfinite

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from impodo.application.destination_matching_service import (
    DESTINATION_GOVERNED_IDENTITY_MODELS,
    DestinationMatchKeyChoice,
    DestinationMatchingService,
    destination_governed_key_choices,
    destination_match_key_candidates,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.mapping.create_field_policy import CREATE_FIXED_VALUE_TYPES
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.odoo_source_scope import (
    RelatedDataHandling,
    propose_related_odoo_data,
)
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.shared.access import Capability
from impodo.domain.workspace.destination_matching import (
    DESTINATION_MATCH_CONTRACT_VERSION,
    DESTINATION_HANDLINGS,
    carry_destination_create_field_reviews,
    choose_destination_create_field_provider,
    confirm_destination_create_field_defaults,
    set_destination_field_exclusion,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateError,
    transfer_destination_workspace,
)

from ..composition.page_reads import run_page_read
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
    handlings: dict[str, str] = {}
    for raw in form.getlist("destination_handling"):
        dataset_id, separator, handling = str(raw).partition("::")
        if (
            not separator
            or not dataset_id
            or handling not in DESTINATION_HANDLINGS
            or dataset_id in handlings
        ):
            raise WorkspaceStateError("Choose one destination handling for each source table")
        handlings[dataset_id] = handling
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
        destination_handling = handlings.pop(dataset_id, None)
        if destination_handling is None:
            raise WorkspaceStateError(
                "Choose one destination handling for each source table"
            )
        choices.append(
            DestinationMatchKeyChoice(
                dataset_id=dataset_id,
                source_column_key=source_column_key,
                additional_source_column_keys=tuple(extras.pop(dataset_id, ())),
                destination_handling=destination_handling,
            )
        )
    if extras or handlings:
        raise WorkspaceStateError("Matching fields do not belong to a selected table")
    return tuple(choices)


def _matching_rows(workspace_state, selection, schema):
    candidates = destination_match_key_candidates(selection, schema)
    governed_by_dataset = destination_governed_key_choices(selection, schema)
    plan = workspace_state.destination_match_plan
    selected_by_dataset = {
        item.dataset_id: item.source_column_keys
        for item in plan.model_matches
    } if plan is not None else {}
    result_by_dataset = {
        item.dataset_id: item for item in plan.model_matches
    } if plan is not None else {}
    reuse_destination_models = {
        item.relation_model
        for item in propose_related_odoo_data(
            schema.models,
            include_selected=True,
        )
        if item.handling is RelatedDataHandling.REUSE_DESTINATION
    }
    relationship_fields_by_model: dict[str, set[str]] = {}
    if plan is not None:
        for relation in plan.relationship_matches:
            relationship_fields_by_model.setdefault(relation.model, set()).add(
                relation.field_name
            )
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
        governed_identity = governed_by_dataset.get(dataset.dataset_id)
        selected_keys = selected_by_dataset.get(dataset.dataset_id, ())
        governed_keys = (
            governed_identity.source_column_keys
            if governed_identity is not None
            else ()
        )
        if governed_keys and selected_keys[: len(governed_keys)] != governed_keys:
            selected_keys = governed_keys
        elif not governed_keys and (
            not selected_keys or selected_keys[0] not in {
                item["stable_key"] for item in primary_candidates
            }
        ):
            selected_keys = (
                (primary_candidates[0]["stable_key"],)
                if primary_candidates
                else ()
            )
        additional_candidates = tuple(
            item for item in available
            if item["stable_key"] not in governed_keys
        )
        optional_start = len(governed_keys) if governed_keys else 1
        optional_slots = (
            0
            if governed_identity is not None
            else max(0, 3 - optional_start)
        )
        rows.append(
            {
                "dataset": dataset,
                "model": model,
                "candidates": available,
                "primary_candidates": primary_candidates,
                "selected_keys": selected_keys,
                "governed_identity": governed_identity,
                "governed_identity_required": (
                    model in DESTINATION_GOVERNED_IDENTITY_MODELS
                ),
                "governed_form_values": tuple(
                    _choice_value(dataset.dataset_id, key)
                    for key in governed_keys
                ),
                "additional_candidates": additional_candidates,
                "optional_identifiers": tuple(
                    {
                        "number": optional_start + index + 1,
                        "selected_key": (
                            selected_keys[optional_start + index]
                            if len(selected_keys) > optional_start + index
                            else ""
                        ),
                    }
                    for index in range(optional_slots)
                ),
                "result": result_by_dataset.get(dataset.dataset_id),
                "identity_issue_rows": (),
                "excluded_identity_rows": (),
                "identity_issue_error": None,
                "relationship_fields": relationship_fields_by_model.get(model, set()),
                "destination_handling": (
                    result_by_dataset[dataset.dataset_id].destination_handling
                    if dataset.dataset_id in result_by_dataset
                    and (
                        model not in reuse_destination_models
                        or plan is not None
                        and plan.contract_version
                        >= DESTINATION_MATCH_CONTRACT_VERSION
                    )
                    else (
                        "reference_only"
                        if model in reuse_destination_models
                        else "transfer"
                    )
                ),
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
    ready = workspace_state.destination_match_ready(
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    rows = _matching_rows(workspace_state, selection, schema)
    for row in rows:
        result = row["result"]
        if result is None or not (
            result.source_blank_row_count
            or result.source_duplicate_key_count
            or result.excluded_source_row_numbers
        ):
            continue
        try:
            row["identity_issue_rows"] = (
                context.categorical_coverage.source_identity_issue_rows(
                    workspace_state.workspace_id,
                    result.dataset_id,
                    result.source_column_keys,
                    excluded_row_numbers=result.excluded_source_row_numbers,
                )
            )
            if result.excluded_source_row_numbers:
                excluded_numbers = set(result.excluded_source_row_numbers)
                row["excluded_identity_rows"] = tuple(
                    issue
                    for issue in context.categorical_coverage.source_identity_issue_rows(
                        workspace_state.workspace_id,
                        result.dataset_id,
                        result.source_column_keys,
                        maximum_rows=result.frozen_source_row_count,
                    )
                    if issue.row_number in excluded_numbers
                )
        except WorkspaceError as identity_error:
            row["identity_issue_error"] = str(identity_error)
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
        matching_can_check=bool(rows) and all(
            row["governed_identity"] is not None
            or (
                not row["governed_identity_required"]
                and row["primary_candidates"]
            )
            for row in rows
        ),
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
        return await run_page_read(
            render_destination_matching_form,
            request,
            workspace_id,
        )

    def render_destination_matching_form(
        request: Request,
        workspace_id: str,
    ):
        """Read and render Destination matching in one bounded worker scope."""

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
            {
                "csrf_token",
                "revision",
                "match_key",
                "match_key_extra",
                "destination_handling",
            },
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
            previous = workspace_state.destination_match_plan
            choice_keys = {
                item.dataset_id: item.source_column_keys for item in choices
            }
            exclusions = {
                item.dataset_id: item.excluded_source_row_numbers
                for item in (previous.model_matches if previous is not None else ())
                if item.excluded_source_row_numbers
                and choice_keys.get(item.dataset_id) == item.source_column_keys
            }
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
                excluded_source_rows=exclusions,
            )
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
                if workspace_state.destination_match_ready(
                    source_selection_hash=selection.content_hash,
                    source_schema_hash=schema.content_hash,
                )
                else (
                    "Destination matching was checked. Complete the remaining "
                    "decisions below."
                    if workspace_state.destination_match_plan.ready
                    else "Destination matching was checked. Review the blockers below."
                )
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

    @router.post("/workspaces/{workspace_id}/destination-matching/field-scope")
    async def choose_destination_field_scope(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "match_plan_hash",
                "field_key",
                "field_action",
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
                    "The destination matching check changed. Reload before changing field scope."
                )
            model, separator, field_name = str(form.get("field_key", "")).partition(
                "::"
            )
            if not separator or not model or not field_name:
                raise WorkspaceStateError("Choose a current destination field issue")
            action = str(form.get("field_action", ""))
            if action not in {"exclude", "restore"}:
                raise WorkspaceStateError("Choose whether to put the field aside")
            updated = set_destination_field_exclusion(
                plan,
                model=model,
                field_name=field_name,
                excluded=action == "exclude",
            )
            context.workspace_states.save_destination_match_plan(
                workspace_id,
                actor=context.actor,
                expected_revision=expected_revision,
                plan=updated,
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
            (
                f"{field_name} is put aside and will not be written to the destination."
                if action == "exclude"
                else f"{field_name} is included again and must be resolved before writing."
            ),
        )
        return RedirectResponse(
            f"/workspaces/{workspace_id}/destination-matching#matching-results",
            status_code=303,
        )

    @router.post("/workspaces/{workspace_id}/destination-matching/record-scope")
    async def choose_destination_record_scope(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "revision",
                "match_plan_hash",
                "dataset_id",
                "row_number",
                "record_action",
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
            previous = workspace_state.destination_match_plan
            if (
                expected_revision != workspace_state.revision
                or previous is None
                or str(form.get("match_plan_hash", "")) != previous.content_hash
                or not workspace_state.destination_match_current(
                    source_selection_hash=selection.content_hash,
                    source_schema_hash=schema.content_hash,
                )
            ):
                raise WorkspaceStateError(
                    "The destination matching check changed. Reload before changing record scope."
                )
            dataset_id = str(form.get("dataset_id", ""))
            try:
                row_number = int(str(form.get("row_number", "")))
            except ValueError as error:
                raise WorkspaceStateError("Choose a current affected source record") from error
            action = str(form.get("record_action", ""))
            if action not in {"exclude", "restore"}:
                raise WorkspaceStateError("Choose whether to put the source record aside")
            current = next(
                (item for item in previous.model_matches if item.dataset_id == dataset_id),
                None,
            )
            if current is None or not 1 <= row_number <= current.frozen_source_row_count:
                raise WorkspaceStateError("Choose a current affected source record")
            exclusions = {
                item.dataset_id: set(item.excluded_source_row_numbers)
                for item in previous.model_matches
                if item.excluded_source_row_numbers
            }
            selected_rows = exclusions.setdefault(dataset_id, set())
            if action == "exclude":
                selected_rows.add(row_number)
            elif row_number not in selected_rows:
                raise WorkspaceStateError("Choose a source record that is currently put aside")
            else:
                selected_rows.remove(row_number)
            exclusions = {
                key: tuple(sorted(numbers))
                for key, numbers in exclusions.items()
                if numbers
            }
            choices = tuple(
                DestinationMatchKeyChoice(
                    dataset_id=item.dataset_id,
                    source_column_key=item.source_column_key,
                    additional_source_column_keys=item.source_column_keys[1:],
                    destination_handling=item.destination_handling,
                )
                for item in previous.model_matches
            )
            credential = get_target_credential(
                context.secret_store,
                workspace_state,
                TargetCredentialRole.DESTINATION_TRANSFER,
            )
            if credential is None:
                raise SecretStoreError(
                    "Return to the destination connection and enter its transfer key"
                )
            models = tuple(sorted(item.model for item in previous.model_matches))
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
                excluded_source_rows=exclusions,
            )
            access = None
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
            context.workspace_states.save_destination_match_plan(
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
            ValueError,
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
                "The source record is put aside for this transfer."
                if action == "exclude"
                else "The source record is included again and its identity must be resolved."
            ),
        )
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
