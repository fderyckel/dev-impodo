"""Expose the focused completed-load correction browser journey."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from impodo.application.correction_execution import correction_api_scope
from impodo.application.correction_jobs import (
    CorrectionJobKind,
    CorrectionJobResult,
)
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.correction import CorrectionPlanError
from impodo.domain.correction_execution import CorrectionExecutionSnapshot
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.reconciliation import ReconciliationRunStatus
from impodo.domain.shared.access import AuthorizationError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateError

from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash, _render
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    store_target_credential,
)


def build_corrections_router(context: WebContext) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/workspaces/{completed_workspace_id}/correction",
        response_class=HTMLResponse,
    )
    async def correction_page(request: Request, completed_workspace_id: str):
        require_session(request)
        return _render_correction(request, context, completed_workspace_id)

    @router.post("/workspaces/{completed_workspace_id}/correction/start")
    async def start_correction(request: Request, completed_workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token", "request_id"})
        try:
            context.corrections.start(
                completed_workspace_id,
                actor=context.actor,
                request_id=_text(form, "request_id"),
            )
        except (AuthorizationError, CorrectionOriginError, ValueError) as error:
            return _render_correction(
                request,
                context,
                completed_workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(
            request,
            "Correction workspace ready. Change only the rules that were wrong.",
        )
        return RedirectResponse(
            f"/workspaces/{completed_workspace_id}/correction",
            status_code=303,
        )

    @router.post("/workspaces/{completed_workspace_id}/correction/review")
    async def review_correction(request: Request, completed_workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "review_request_id",
                "read_api_key",
                "remember_read_api_key",
            },
        )
        active = context.correction_jobs.active(completed_workspace_id)
        if active is not None:
            return RedirectResponse(
                _progress_url(completed_workspace_id, active.job_id),
                status_code=303,
            )
        try:
            view = context.corrections.get(
                completed_workspace_id,
                actor=context.actor,
            )
            if view is None or view.successor_workspace_id is None:
                raise CorrectionOriginError("Start the correction first")
            successor_state = context.queries.get(view.successor_workspace_id)
            submitted_key = _text(form, "read_api_key")
            credential = get_target_credential(
                context.secret_store,
                successor_state,
                TargetCredentialRole.READ,
            )
            if credential is None:
                credential = get_target_credential(
                    context.secret_store,
                    context.queries.get(completed_workspace_id),
                    TargetCredentialRole.READ,
                )
            if submitted_key:
                credential = store_target_credential(
                    context.secret_store,
                    successor_state,
                    TargetCredentialRole.READ,
                    submitted_key,
                    persistent="remember_read_api_key" in form,
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    successor_state,
                    TargetCredentialRole.READ,
                    credential,
                    actor=context.actor,
                )
            review_request_id = _text(form, "review_request_id")

            def work(progress):
                progress(15, "Checking the corrected rules")
                review, plan, _binding = context.corrections.review(
                    completed_workspace_id,
                    actor=context.actor,
                    review_request_id=review_request_id,
                )
                progress(90, "Preparing the compact correction review")
                summary = plan.public_summary() if plan is not None else None
                return CorrectionJobResult(
                    field_count=summary.field_count if summary else 0,
                    record_count=summary.record_count if summary else 0,
                    already_corrected_count=review.already_corrected_count,
                    blocker_messages=tuple(
                        dict.fromkeys(
                            item.message or "A correction target needs attention"
                            for item in review.blockers
                        )
                    ),
                )

            job = context.correction_jobs.enqueue(
                completed_workspace_id,
                view.successor_workspace_id,
                kind=CorrectionJobKind.REVIEW,
                work=work,
            )
        except (
            AuthorizationError,
            CorrectionOriginError,
            SecretStoreError,
            ValueError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            return _render_correction(
                request,
                context,
                completed_workspace_id,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            _progress_url(completed_workspace_id, job.job_id),
            status_code=303,
        )

    @router.post("/workspaces/{completed_workspace_id}/correction/apply")
    async def apply_correction(request: Request, completed_workspace_id: str):
        form = await request.form()
        _secure_form(
            request,
            form,
            {
                "csrf_token",
                "confirmation_id",
                "confirm_apply",
                "write_api_key",
                "remember_write_api_key",
            },
        )
        active = context.correction_jobs.active(completed_workspace_id)
        if active is not None:
            return RedirectResponse(
                _progress_url(completed_workspace_id, active.job_id),
                status_code=303,
            )
        try:
            if _text(form, "confirm_apply") != "yes":
                raise CorrectionPlanError(
                    "Confirm that Impodo may apply this reviewed correction"
                )
            view = context.corrections.get(
                completed_workspace_id,
                actor=context.actor,
            )
            plan = context.corrections.current_plan(completed_workspace_id)
            if view is None or view.successor_workspace_id is None or plan is None:
                raise CorrectionOriginError("Review the correction first")
            successor_state = context.queries.get(view.successor_workspace_id)
            submitted_key = _text(form, "write_api_key")
            credential = get_target_credential(
                context.secret_store,
                successor_state,
                TargetCredentialRole.WRITE,
            )
            if credential is None:
                credential = get_target_credential(
                    context.secret_store,
                    context.queries.get(completed_workspace_id),
                    TargetCredentialRole.WRITE,
                )
            if submitted_key:
                credential = store_target_credential(
                    context.secret_store,
                    successor_state,
                    TargetCredentialRole.WRITE,
                    submitted_key,
                    persistent="remember_write_api_key" in form,
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    successor_state,
                    TargetCredentialRole.WRITE,
                    credential,
                    actor=context.actor,
                )
            if credential is None:
                raise SecretStoreError(
                    "Enter an Odoo API key approved for correcting this target"
                )
            confirmation_id = _text(form, "confirmation_id")

            def work(progress):
                provisional_scope = _plan_scope(plan)
                progress(10, "Checking correction write access")
                identity = context.write_identity_probe(
                    successor_state,
                    credential.secret,
                    provisional_scope,
                )
                context.corrections.confirm(
                    completed_workspace_id,
                    confirmation_id=confirmation_id,
                    write_credential_binding_hash=credential.binding_hash,
                    write_identity=identity,
                    actor=context.actor,
                )
                progress(30, "Rechecking Odoo before the first change")
                confirmation = context.corrections.current_confirmation(
                    completed_workspace_id
                )
                if confirmation is None:
                    raise CorrectionPlanError("Correction confirmation is missing")
                snapshot = CorrectionExecutionSnapshot.create(
                    plan,
                    confirmation,
                    target_database=successor_state.odoo_database,
                )
                scope = correction_api_scope(snapshot)
                current_identity = context.write_identity_probe(
                    successor_state,
                    credential.secret,
                    scope,
                )
                reader = context.readback_reader_factory(
                    successor_state,
                    credential.secret,
                    scope,
                )
                writer = context.write_executor_factory(
                    successor_state,
                    credential.secret,
                    scope,
                )
                progress(55, "Applying the reviewed corrections")
                result = context.corrections.execute(
                    completed_workspace_id,
                    target_database=successor_state.odoo_database,
                    write_credential_binding_hash=credential.binding_hash,
                    write_identity=current_identity,
                    reader=reader,
                    writer=writer,
                    actor=context.actor,
                )
                progress(95, "Checking the correction outcome")
                summary = plan.public_summary()
                return CorrectionJobResult(
                    field_count=summary.field_count,
                    record_count=summary.record_count,
                    verified=(
                        result.reconciliation.status
                        is ReconciliationRunStatus.VERIFIED
                    ),
                    blocker_messages=(
                        ()
                        if result.reconciliation.status
                        is ReconciliationRunStatus.VERIFIED
                        else ("The correction outcome needs review.",)
                    ),
                )

            job = context.correction_jobs.enqueue(
                completed_workspace_id,
                view.successor_workspace_id,
                kind=CorrectionJobKind.APPLY,
                work=work,
            )
        except (
            AuthorizationError,
            CorrectionOriginError,
            CorrectionPlanError,
            SecretStoreError,
            ValueError,
            WorkspaceError,
            WorkspaceStateError,
        ) as error:
            return _render_correction(
                request,
                context,
                completed_workspace_id,
                error=str(error),
                status_code=422,
            )
        return RedirectResponse(
            _progress_url(completed_workspace_id, job.job_id),
            status_code=303,
        )

    @router.get(
        "/workspaces/{completed_workspace_id}/correction/progress/{job_id}",
        response_class=HTMLResponse,
    )
    async def correction_progress(
        request: Request,
        completed_workspace_id: str,
        job_id: str,
    ):
        require_session(request)
        try:
            job = context.correction_jobs.get(completed_workspace_id, job_id)
        except LookupError:
            return HTMLResponse("Correction progress not found", status_code=404)
        return _render(
            request,
            "workspace_correction_progress.html",
            job=job,
            completed_workspace_id=completed_workspace_id,
        )

    return router


def _render_correction(
    request: Request,
    context: WebContext,
    completed_workspace_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    view = context.corrections.get(
        completed_workspace_id,
        actor=context.actor,
    )
    if view is None:
        return HTMLResponse("Completed load is not eligible for correction", status_code=404)
    active = context.correction_jobs.active(completed_workspace_id)
    if active is not None:
        return RedirectResponse(
            _progress_url(completed_workspace_id, active.job_id),
            status_code=303,
        )
    latest = context.correction_jobs.latest(completed_workspace_id)
    successor_state = (
        context.queries.get(view.successor_workspace_id)
        if view.successor_workspace_id is not None
        else None
    )
    return _render(
        request,
        "workspace_correction.html",
        correction=view,
        latest_job=latest,
        successor_state=successor_state,
        request_id=str(uuid4()),
        review_request_id=str(uuid4()),
        confirmation_id=str(uuid4()),
        error=error,
        status_code=status_code,
    )


def _plan_scope(plan) -> OdooApiScope:
    fields: dict[str, set[str]] = {}
    for item in plan.fields:
        fields.setdefault(item.target_model, set()).add(item.target_field)
    return OdooApiScope(
        preview_hash=plan.plan_hash,
        models=tuple(
            OdooModelScope(
                model=model,
                write_fields=tuple(sorted(names)),
                read_fields=tuple(sorted(names)),
            )
            for model, names in sorted(fields.items())
        ),
    )


def _progress_url(completed_workspace_id: str, job_id: str) -> str:
    return (
        f"/workspaces/{completed_workspace_id}/correction/progress/{job_id}"
    )


__all__ = ["build_corrections_router"]
