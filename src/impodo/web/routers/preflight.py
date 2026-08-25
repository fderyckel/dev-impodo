"""Expose read-only Odoo comparison and its review artifacts.

Migration stage: H — read-only target preflight. Layer: web route.

The compare action supplies
:class:`impodo.application.preflight_service.PreflightService` with a
project-bound read-only snapshot reader. Other routes download the already-
published technical manifest or build/download its human review workbook
projection. No route exposes an Odoo write operation.

See ``docs/architecture/python-code-map.md`` and
``tests/test_preflight_service.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ...access import Capability
from ...application.odoo_read_failures import (
    OdooReadFailureCode,
    OdooReadWorkflowError,
    classify_odoo_read_failure,
)
from ...application.preflight_service import MANIFEST_NAME
from ...artifacts import ArtifactStoreError
from ...connectors import ConnectorError
from ...domain.errors import ReadinessError
from ...migration_foundation import MigrationFoundationError
from ...migration_runs import MigrationRunPurpose
from ...reporting import (
    WORKBOOK_NAME,
    ReportGenerationError,
    write_review_workbook,
)
from ...secrets import SecretStoreError
from ...workspace_access import bind_workspace_access_context
from ...workspace_errors import WorkspaceDatabaseBusyError, WorkspaceError
from ...workspace_state import OdooConnectionMode, WorkspaceStateError
from ..context import WebContext
from ..forms import _secure_form, _text
from ..presenters.common import _flash
from ..presenters.summary import _render_summary
from ..run_review import (
    publish_compared_application,
    publish_reconciled_application,
)
from ..security import require_session
from ..target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    get_target_credential,
    store_target_credential,
)
from ..target_readers import (
    LocalOdooRecoveryRequired,
    _read_readiness_snapshots,
)


def _read_key_persistence(form) -> bool:
    """Translate the explicit retention choice into the secret-store boundary."""

    storage = _text(form, "read_api_key_storage")
    if storage == "vault":
        return True
    if storage == "session":
        return False
    raise OdooReadWorkflowError(
        OdooReadFailureCode.READ_KEY_MISSING,
        "Choose whether Impodo should keep the read-only key on this computer "
        "or only until Impodo closes.",
    )


def _rebind_remote_read_access(context: WebContext, workspace_state, credential):
    """Verify and rebind a replacement key without recapturing semantic state."""

    if workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE:
        return None
    schema = context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
    if (
        schema is None
        or credential.binding_hash == schema.read_credential_binding_hash
    ):
        return None
    probe_models = tuple(sorted(model.name for model in schema.models))
    identity = context.read_identity_probe(
        workspace_state,
        credential.secret,
        probe_models,
    )
    snapshot = context.schema_reader(workspace_state, credential.secret)
    try:
        context.schema_workspace.rebind_current_access(
            workspace_state.workspace_id,
            snapshot,
            read_credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            actor=context.actor,
        )
    except WorkspaceDatabaseBusyError:
        raise
    except WorkspaceError as error:
        raise OdooReadWorkflowError(
            OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
            "The Odoo fields or read access changed; refresh Odoo data before "
            "comparing again.",
        ) from error
    context.remote_connections.mark_checked(
        workspace_state,
        snapshot.fingerprint,
        identity,
    )
    return identity


def _report_chunks(
    context: WebContext,
    workspace_id: str,
    run_id: str,
    filename: str,
) -> Iterator[bytes]:
    """Stream a protected report artifact without loading it all in memory."""

    with context.artifacts.materialize_report(
        workspace_id, run_id, filename
    ) as path, path.open("rb") as report:
        while chunk := report.read(64 * 1024):
            yield chunk


def build_preflight_router(context: WebContext) -> APIRouter:
    """Build compare, manifest, and workbook routes for current Stage H evidence."""

    router = APIRouter()

    @router.post("/workspaces/{workspace_id}/summary/compare")
    async def compare_workspace_data(request: Request, workspace_id: str):
        """Compare the exact approved rows through a bounded read-only reader."""

        form = await request.form()
        _secure_form(
            request,
            form,
            {"csrf_token", "read_api_key", "read_api_key_storage"},
        )
        workspace_state = context.queries.get(workspace_id)
        credential_owner = context.target_credential_workspace(workspace_id)
        verified_read_identity = None
        completed_without_load = False

        def reader(requirements):
            return _read_readiness_snapshots(
                context,
                workspace_state,
                requirements,
                verified_read_identity=verified_read_identity,
            )

        try:
            access_context = context.workspace_access.resolve(
                workspace_id,
                actor=context.actor,
                capability=Capability.PREFLIGHT_RUN,
            )
            submitted_key = _text(form, "read_api_key")
            if submitted_key:
                if workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE:
                    raise OdooReadWorkflowError(
                        OdooReadFailureCode.CONNECTION_DETAILS_INVALID,
                        "A read-only API key can be entered here only for Remote Odoo.",
                    )
                credential = store_target_credential(
                    context.secret_store,
                    credential_owner,
                    TargetCredentialRole.READ,
                    submitted_key,
                    persistent=_read_key_persistence(form),
                )
                audit_stored_target_credential(
                    context.workspace_states,
                    credential_owner,
                    TargetCredentialRole.READ,
                    credential,
                    actor=context.actor,
                )
                context.remote_connections.clear(workspace_id)
            else:
                credential = get_target_credential(
                    context.secret_store,
                    credential_owner,
                    TargetCredentialRole.READ,
                )
            if credential is not None:
                verified_read_identity = await run_in_threadpool(
                    _rebind_remote_read_access,
                    context,
                    workspace_state,
                    credential,
                )
            await run_in_threadpool(
                context.preflight.compare,
                workspace_id,
                reader=reader,
                actor=context.actor,
            )
            preview = context.execution.current_preview(workspace_id)
            run = context.migration_runs.get(
                access_context.migration_run_id,
                actor=context.actor,
            )
            if access_context.recipe_application_id is not None:
                await run_in_threadpool(
                    publish_compared_application,
                    context,
                    access_context.recipe_application_id,
                    access_context.migration_run_id,
                )
            if (
                access_context.recipe_application_id is not None
                and run.purpose is MigrationRunPurpose.TEST
                and preview is not None
                and preview.can_complete_without_load
            ):
                completed = await run_in_threadpool(
                    context.execution.complete_no_changes,
                    workspace_id,
                    expected_snapshot_hash=preview.snapshot.semantic_hash,
                    actor=context.actor,
                )
                readback = context.readback_reader_factory(
                    workspace_state,
                    credential.secret if credential is not None else "",
                    preview.api_scope,
                )
                verification = await run_in_threadpool(
                    context.reconciliation.reconcile,
                    workspace_id,
                    expected_execution_run_id=completed.run_id,
                    reader=readback,
                    actor=context.actor,
                )
                if not verification.unknown_count and not verification.fallout_count:
                    await run_in_threadpool(
                        publish_reconciled_application,
                        context,
                        access_context.recipe_application_id,
                        access_context.migration_run_id,
                    )
                    completed_without_load = True
        except LocalOdooRecoveryRequired as error:
            return _render_summary(
                request,
                context,
                workspace_id,
                local_stack_error=(
                    "Reconnect local Odoo for this session before comparing "
                    "data."
                ),
                local_stack_support_error=str(error),
                open_local_stack=True,
                status_code=422,
            )
        except (
            ArtifactStoreError,
            ConnectorError,
            MigrationFoundationError,
            WorkspaceStateError,
            ReadinessError,
            SecretStoreError,
            WorkspaceError,
            OSError,
        ) as error:
            return _render_summary(
                request,
                context,
                workspace_id,
                comparison_failure=classify_odoo_read_failure(error),
                status_code=422,
            )
        if completed_without_load:
            _flash(
                request,
                "Odoo already matches this Recipe. No load confirmation was needed.",
            )
            return RedirectResponse(
                f"/projects/{access_context.project_id}/runs/"
                f"{access_context.migration_run_id}",
                status_code=303,
            )
        _flash(request, "Prepared data compared with Odoo. Nothing was changed.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/summary",
            status_code=303,
        )

    @router.get("/workspaces/{workspace_id}/summary/manifest")
    async def download_readiness_manifest(request: Request, workspace_id: str):
        require_session(request)
        context.workspace_access.resolve(
            workspace_id,
            actor=context.actor,
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )
        report = context.preflight.current_report(workspace_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        try:
            exists = context.artifacts.report_exists(
                workspace_id, report.run_id, MANIFEST_NAME
            )
        except ArtifactStoreError as error:
            raise HTTPException(
                status_code=404, detail="Readiness manifest not found"
            ) from error
        if not exists:
            raise HTTPException(status_code=404, detail="Readiness manifest not found")
        filename = f"impodo-{workspace_id[:8]}-preflight.json"
        return StreamingResponse(
            _report_chunks(
                context,
                workspace_id,
                report.run_id,
                MANIFEST_NAME,
            ),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/workspaces/{workspace_id}/summary/package")
    async def generate_readiness_package(request: Request, workspace_id: str):
        form = await request.form()
        _secure_form(request, form, {"csrf_token"})
        access_context = context.workspace_access.resolve(
            workspace_id,
            actor=context.actor,
            capability=Capability.PROTECTED_EVIDENCE_MANAGE,
        )
        report = context.preflight.current_report(workspace_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        staging = context.preflight.current_staging(workspace_id)
        if staging is None or not staging.control_totals_passed:
            return _render_summary(
                request,
                context,
                workspace_id,
                error=(
                    "Resolve the named totals that need attention before "
                    "creating the package."
                ),
                status_code=422,
            )
        quality = context.quality.current_summary(workspace_id)
        if quality is None or quality.run_id != report.quality_run_id:
            return _render_summary(
                request,
                context,
                workspace_id,
                error="Check all rows again before creating the package.",
                status_code=422,
            )
        if not quality.ready_for_package:
            return _render_summary(
                request,
                context,
                workspace_id,
                error=(
                    "Resolve the data checks that need review or setup before "
                    "creating the package. Records already set aside may remain "
                    "outside the package."
                ),
                status_code=422,
            )
        if report.status != "READY":
            return _render_summary(
                request,
                context,
                workspace_id,
                error="Resolve the rows that need attention before creating the package.",
                status_code=422,
            )

        def write_package() -> None:
            with (
                bind_workspace_access_context(access_context),
                context.artifacts.materialize_report(
                    workspace_id, report.run_id, MANIFEST_NAME
                ) as manifest_path,
                context.artifacts.prepare_report(
                    workspace_id, report.run_id, WORKBOOK_NAME
                ) as workbook_path,
            ):
                write_review_workbook(manifest_path, workbook_path)

        try:
            await run_in_threadpool(write_package)
        except (ArtifactStoreError, OSError, ReportGenerationError) as error:
            return _render_summary(
                request,
                context,
                workspace_id,
                error=str(error),
                status_code=422,
            )
        _flash(request, "Review package created.")
        return RedirectResponse(
            f"/workspaces/{workspace_id}/summary",
            status_code=303,
        )

    @router.get("/workspaces/{workspace_id}/summary/workbook")
    async def download_readiness_workbook(request: Request, workspace_id: str):
        require_session(request)
        context.workspace_access.resolve(
            workspace_id,
            actor=context.actor,
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )
        report = context.preflight.current_report(workspace_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Readiness report not found")
        try:
            exists = context.artifacts.report_exists(
                workspace_id, report.run_id, WORKBOOK_NAME
            )
        except ArtifactStoreError as error:
            raise HTTPException(
                status_code=404, detail="Review package not found"
            ) from error
        if not exists:
            raise HTTPException(status_code=404, detail="Review package not found")
        filename = f"impodo-{workspace_id[:8]}-review.xlsx"
        return StreamingResponse(
            _report_chunks(
                context,
                workspace_id,
                report.run_id,
                WORKBOOK_NAME,
            ),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router
