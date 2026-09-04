from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode
from uuid import uuid4

from starlette.requests import Request

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.application.transfer_order_service import TransferOrderService
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from impodo.domain.workspace.workbench import WorkspaceStateService
from impodo.web.routers.transfer_load import build_transfer_load_router
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from tests.application.workspace.execution.test_service import _snapshot
from tests.application.workspace.test_destination_matching import (
    CONTEXT_HASH,
    PERMISSION_HASH,
    PRINCIPAL_HASH,
    _SourceValues,
    _destination_reader,
    _identity,
    _selection,
    _source_schema,
    _workspace,
)
from tests.application.workspace.test_transfer_review import _WorkspaceRepository


class TransferLoadRouteTests(unittest.TestCase):
    def test_prepare_rechecks_and_stages_without_constructing_writer(self) -> None:
        now = datetime.now(UTC)
        workspace = _workspace(now)
        selection = _selection(now)
        schema = _source_schema(workspace, now)
        product, uom = selection.datasets
        source_values = _SourceValues(
            {
                (product.dataset_id, "product-code"): (
                    {"value": "P001", "count": 1},
                    {"value": "P002", "count": 1},
                ),
                (uom.dataset_id, "uom-name"): (
                    {"value": "Kilogram", "count": 1},
                    {"value": "Unit", "count": 1},
                ),
            }
        )
        secrets = MemorySecretStore()
        credential = store_target_credential(
            secrets,
            workspace,
            TargetCredentialRole.DESTINATION_TRANSFER,
            "destination-secret",
            persistent=False,
        )
        workspace = replace(
            workspace,
            destination_verified_credential_binding_hash=credential.binding_hash,
        )
        identity = replace(
            _identity(workspace),
            principal_hash=PRINCIPAL_HASH,
            permission_hash=PERMISSION_HASH,
            context_hash=CONTEXT_HASH,
        )
        reader = _destination_reader(workspace)
        matching = DestinationMatchingService(source_values)
        choices = (
            DestinationMatchKeyChoice(product.dataset_id, "product-code"),
            DestinationMatchKeyChoice(uom.dataset_id, "uom-name"),
        )
        match = matching.check(
            workspace,
            selection,
            schema,
            choices,
            api_key=credential.secret,
            credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            reader=reader,
            recorded_by="Data manager",
        )
        workspace = replace(workspace, destination_match_plan=match)
        order = TransferOrderService().build(
            workspace,
            match,
            recorded_by="Data manager",
        )
        workspace = replace(workspace, transfer_order_plan=order)
        package = TransferReviewService().build(
            workspace,
            match,
            order,
            run_id=str(uuid4()),
            data_version_id=str(uuid4()),
            built_by=LOCAL_ACTOR.identity,
        )
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=now,
        )
        workspace = replace(
            workspace,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        workspace = replace(workspace, transfer_preflight_report=report)
        repository = _WorkspaceRepository(workspace)
        writer_calls: list[bool] = []
        transfer_execution = _TransferExecution()
        context = SimpleNamespace(
            actor=LOCAL_ACTOR,
            queries=_Queries(repository, selection, schema),
            workspace_states=WorkspaceStateService(
                repository,
                CapabilityAuthorizationPolicy(),
            ),
            categorical_coverage=source_values,
            secret_store=secrets,
            read_identity_probe=lambda destination, secret, models: identity,
            destination_match_reader=reader,
            odoo_provenance=_NoOrigins(),
            execution=_NoExecution(),
            transfer_execution=transfer_execution,
            write_executor_factory=lambda *_args, **_kwargs: writer_calls.append(True),
        )
        router = build_transfer_load_router(context)

        response = asyncio.run(
            _endpoint(router, "prepare_transfer_load")(
                _request(
                    "/transfer-load/prepare",
                    {
                        "csrf_token": "csrf",
                        "revision": str(workspace.revision),
                        "preflight_hash": report.content_hash,
                    },
                ),
                workspace.workspace_id,
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].endswith("/transfer-load/confirm"))
        self.assertTrue(repository.workspace.transfer_preflight_report.ready)
        self.assertEqual(repository.events, ["WORKSPACE_TRANSFER_PREFLIGHT_CHECKED"])
        self.assertEqual(transfer_execution.compiled, 1)
        self.assertEqual(transfer_execution.staged, 1)
        self.assertEqual(writer_calls, [])

    def test_recovery_assesses_readback_before_constructing_writer(self) -> None:
        now = datetime.now(UTC)
        preflight_hash = "sha256:" + "9" * 64
        workspace = replace(
            _workspace(now),
            transfer_preflight_report=SimpleNamespace(
                ready=True,
                content_hash=preflight_hash,
            ),
        )
        selection = _selection(now)
        schema = _source_schema(workspace, now)
        snapshot = replace(
            _snapshot(),
            workspace_id=workspace.workspace_id,
            preflight_result_hash=preflight_hash,
            target_hash=workspace.destination_verified_target_hash,
            target_database=workspace.destination_odoo_database,
        )
        secrets = MemorySecretStore()
        credential = store_target_credential(
            secrets,
            workspace,
            TargetCredentialRole.DESTINATION_TRANSFER,
            "same-destination-secret",
            persistent=False,
        )
        run = ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=workspace.workspace_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            batch_rows=10,
            status=ExecutionRunStatus.RUNNING,
            started_at=now,
            started_by=LOCAL_ACTOR.identity.display_name,
            completed_at=None,
            rows=tuple(
                ExecutionRowAttempt(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    target_model=row.target_model,
                    operation=row.disposition,
                    field_names=tuple(field.field for field in row.fields),
                    proposed_external_id=row.proposed_external_id,
                    status=(
                        ExecutionRowStatus.IN_FLIGHT
                        if index == 0
                        else ExecutionRowStatus.PLANNED
                    ),
                    attempt=1 if index == 0 else 0,
                )
                for index, row in enumerate(snapshot.rows)
            ),
            write_credential_binding_hash=credential.binding_hash,
            write_principal_hash=PRINCIPAL_HASH,
            write_permission_hash=PERMISSION_HASH,
            write_context_hash=CONTEXT_HASH,
        )
        calls: list[str] = []
        jobs = _SynchronousJobs()
        transfer_execution = _RecoveryTransferExecution(snapshot, run, calls)
        reconciliation = _RecoveryReconciliation(calls)
        context = SimpleNamespace(
            actor=LOCAL_ACTOR,
            queries=_Queries(SimpleNamespace(workspace=workspace), selection, schema),
            execution=_CurrentExecution(run),
            transfer_execution=transfer_execution,
            reconciliation=reconciliation,
            categorical_coverage=object(),
            secret_store=secrets,
            load_jobs=jobs,
            workspace_access=SimpleNamespace(
                resolve=lambda *_args, **_kwargs: object()
            ),
            workspace_views=SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(
                    migration_project=SimpleNamespace(display_name="Transfer test")
                )
            ),
            read_identity_probe=lambda *_args, **_kwargs: _called(
                calls,
                "read identity",
            ),
            write_identity_probe=lambda *_args, **_kwargs: _called(
                calls,
                "write identity",
            ),
            readback_reader_factory=lambda *_args, **_kwargs: _called(
                calls,
                "readback reader",
            ),
            write_executor_factory=lambda *_args, **_kwargs: _called(
                calls,
                "writer",
            ),
        )
        router = build_transfer_load_router(context)

        response = asyncio.run(
            _endpoint(router, "recover_transfer_load")(
                _request(
                    "/transfer-load/recover",
                    {
                        "csrf_token": "csrf",
                        "revision": str(workspace.revision),
                        "execution_run_id": run.run_id,
                        "snapshot_hash": snapshot.semantic_hash,
                        "preflight_hash": preflight_hash,
                    },
                ),
                workspace.workspace_id,
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].endswith("/recovery-job"))
        self.assertEqual(jobs.result.execution_run_id, run.run_id)
        self.assertTrue(jobs.result.verification_complete)
        self.assertLess(calls.index("assess recovery"), calls.index("writer"))
        self.assertEqual(calls.count("resume"), 1)


class _Queries:
    def __init__(self, repository, selection, schema) -> None:
        self.repository = repository
        self.selection = selection
        self.schema = schema

    def get(self, _workspace_id):
        return self.repository.workspace

    def get_source_selection(self, _workspace_id):
        return self.selection

    def get_odoo_schema_catalog(self, _workspace_id):
        return self.schema


class _CurrentExecution:
    def __init__(self, run) -> None:
        self.run = run

    def current_transfer_run(self, _workspace_id):
        return self.run


class _RecoveryTransferExecution:
    def __init__(self, snapshot, run, calls) -> None:
        self.snapshot = snapshot
        self.run = run
        self.calls = calls

    def current_snapshot(self, *_args, **_kwargs):
        return self.snapshot

    def resume(self, *_args, **_kwargs):
        self.calls.append("resume")
        return replace(
            self.run,
            status=ExecutionRunStatus.COMPLETED,
            completed_at=datetime.now(UTC),
        )


class _RecoveryReconciliation:
    def __init__(self, calls) -> None:
        self.calls = calls

    def assess_recovery(self, *_args, **_kwargs):
        self.calls.append("assess recovery")
        return object()

    def reconcile(self, *_args, **_kwargs):
        self.calls.append("reconcile")
        return SimpleNamespace(unknown_count=0, fallout_count=0)


class _SynchronousJobs:
    def __init__(self) -> None:
        self.result = None

    def active(self, _workspace_id):
        return None

    def enqueue(self, *_args, access_context, work, **_kwargs):
        self.result = work(access_context, lambda _run: None, lambda _run: None)
        return SimpleNamespace(job_id="recovery-job")


def _called(calls, label):
    calls.append(label)
    return object()


class _NoOrigins:
    def read_current_origins(self, *_args, **_kwargs):
        return None


class _NoExecution:
    def current_transfer_run(self, _workspace_id):
        return None


class _TransferExecution:
    def __init__(self) -> None:
        self.compiled = 0
        self.staged = 0

    def compile(self, *_args, **_kwargs):
        self.compiled += 1
        return object()

    def stage(self, *_args, **_kwargs):
        self.staged += 1


def _endpoint(router, name):
    return next(route.endpoint for route in router.routes if route.name == name)


def _request(path: str, values: dict[str, str]) -> Request:
    body = urlencode(values).encode("ascii")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "session": {"authenticated": True, "csrf_token": "csrf"},
        },
        receive,
    )


if __name__ == "__main__":
    unittest.main()
