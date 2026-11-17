"""Release local owners before transport, request admission, and progress sinks."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from itertools import product
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4

from impodo.application.workspace.execution.service import ExecutionService
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_scope import execution_api_scope
from impodo.domain.execution.odoo_write import OdooRequestDeferred
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    OdooReadIdentity,
    OdooWriteIdentity,
    target_identity_hash,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as fixtures


class _LocalPhases:
    """Model operation-owned resources without opening a database."""

    def __init__(self):
        self.owner = 0
        self.next_owner = 0
        self.events = []

    def __call__[T](self, function: Callable[[], T]) -> T:
        if self.owner:
            return function()
        self.next_owner += 1
        self.owner = self.next_owner
        try:
            return function()
        finally:
            self.owner = 0

    def local(self, name, function):
        def checked(*args, **kwargs):
            if not self.owner:
                raise AssertionError(f"Local {name} has no operation owner")
            self.events.append((name, self.owner))
            return function(*args, **kwargs)

        return checked

    def external(self, name, function):
        def checked(*args, **kwargs):
            if self.owner:
                raise AssertionError(f"External {name} retained a local owner")
            self.events.append((name, 0))
            return function(*args, **kwargs)

        return checked


class _CheckedExecutor(fixtures._Executor):
    def __init__(self, phases, scope, *, defer_after=None, **kwargs):
        super().__init__(scope, **kwargs)
        self.admissions = 0
        self.defer_after = defer_after
        for name in (
            "admit_write",
            "find_ids",
            "find_ids_many",
            "create_rows",
            "load_create_rows",
            "update_row",
            "read_projected_ids",
        ):
            setattr(self, name, phases.external(name, getattr(self, name)))

    def admit_write(self):
        self.admissions += 1
        if self.defer_after is not None and self.admissions > self.defer_after:
            raise OdooRequestDeferred("Synthetic request admission deferred")


class _TransferExecutor(_CheckedExecutor):
    def __init__(self, phases, scope, *, target_hash, interrupted=False):
        self.phases = phases
        self.interrupted = interrupted
        super().__init__(
            phases,
            scope,
            lookup_ids=(),
            lookup_results={("res.partner", (("ref", "=", "C1"),)): (50,)},
        )
        self.target_hash = target_hash

    def find_ids_many(self, model, domains):
        self.phases.events.append((f"lookup:{model}", self.phases.owner))
        return super().find_ids_many(model, domains)

    def load_create_rows(self, model, values, external_ids):
        if self.interrupted:
            raise RuntimeError("Synthetic transfer interruption")
        return super().load_create_rows(model, values, external_ids)


def _checked_service(original, journal, phases, *, receipt_error=None):
    for name in (
        "get_current_run",
        "get_run",
        "start_run",
        "record_batch_started",
        "record_outcomes",
        "record_recovery",
        "finish_run",
    ):
        function = getattr(journal, name)
        if name == "record_outcomes" and receipt_error is not None:

            def fail_receipt(*_args, **_kwargs):
                raise receipt_error

            function = fail_receipt
        setattr(journal, name, phases.local(name, function))
    original.workspaces.get = phases.local("workspace", original.workspaces.get)
    for name in (
        "current_execution_snapshot",
        "current_business_intent_assessment",
        "business_intent_review",
        "require_business_intent_approval",
    ):
        if hasattr(original.preflight, name):
            setattr(
                original.preflight,
                name,
                phases.local(name, getattr(original.preflight, name)),
            )
    service = ExecutionService(
        original.workspaces,
        original.preflight,
        journal,
        SimpleNamespace(
            require=phases.local("authorization", original.authorization.require)
        ),
        local_operation=phases,
    )
    return service, journal


def _service(snapshot, mode, phases, *, receipt_error=None):
    original, journal = fixtures.ExecutionServiceTests()._service(snapshot, mode=mode)
    return _checked_service(original, journal, phases, receipt_error=receipt_error)


def _transfer_service(major, mode, phases):
    template = replace(
        fixtures._snapshot(),
        target_odoo_version=f"{major}.0",
        target_module_versions={"base": f"{major}.0.1.0"},
    )
    original, journal, snapshot = fixtures.ExecutionServiceTests()._transfer_service(
        template
    )
    workspace = original.workspaces.get(snapshot.workspace_id)
    target_hash = target_identity_hash(
        connection_mode=mode.value,
        base_url=workspace.destination_odoo_base_url,
        database=workspace.destination_odoo_database,
    )
    snapshot = replace(snapshot, target_hash=target_hash)
    workspace = replace(
        workspace,
        destination_odoo_connection_mode=mode,
        destination_verified_target_hash=target_hash,
        destination_verified_odoo_version=f"{major}.0",
    )
    original.workspaces.get = lambda _workspace_id: workspace
    service, journal = _checked_service(original, journal, phases)
    return service, journal, snapshot


def _recovery(snapshot, run):
    return ReconciliationRun(
        reconciliation_id=str(uuid4()),
        workspace_id=snapshot.workspace_id,
        execution_run_id=run.run_id,
        snapshot_hash=snapshot.semantic_hash,
        target_hash=snapshot.target_hash,
        target_database=snapshot.target_database,
        status=ReconciliationRunStatus.FALLOUT,
        verified_at=datetime.now(UTC),
        verified_by="Synthetic operator",
        unchanged_count=0,
        rows=tuple(
            ReconciliationRow(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.operation,
                execution_status=row.status.value,
                status=(
                    ReconciliationRowStatus.VERIFIED
                    if row.status is ExecutionRowStatus.COMMITTED
                    else ReconciliationRowStatus.NOT_APPLIED
                    if row.status is ExecutionRowStatus.IN_FLIGHT
                    else ReconciliationRowStatus.NOT_WRITTEN
                ),
                odoo_id=row.odoo_id,
                message="Synthetic exact read-back",
                retry_safe=row.status is ExecutionRowStatus.IN_FLIGHT,
            )
            for row in run.rows
        ),
    )


class LocalPhaseBoundaryTests(TestCase):
    def test_execute_and_resume_release_owners_for_both_majors_and_modes(self):
        for major, mode in product((19, 20), tuple(OdooConnectionMode)):
            with self.subTest(major=major, mode=mode):
                snapshot = replace(
                    fixtures._snapshot(),
                    target_odoo_version=f"{major}.0",
                    target_module_versions={"base": f"{major}.0.1.0"},
                )
                phases = _LocalPhases()
                service, journal = _service(snapshot, mode, phases)
                scope = execution_api_scope(snapshot).semantic_hash
                writer = _CheckedExecutor(phases, scope, defer_after=1)
                progress = []
                publish = phases.external("progress", progress.append)

                interrupted = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=writer,
                    actor=LOCAL_ACTOR,
                    progress=publish,
                )
                self.assertEqual(interrupted.status, ExecutionRunStatus.RUNNING)
                self.assertEqual(interrupted.committed_count, 1)
                self.assertEqual(interrupted.in_flight_count, 0)
                self.assertEqual(phases.owner, 0)
                admission = phases.events[
                    : next(
                        index
                        for index, (name, _owner) in enumerate(phases.events)
                        if name == "start_run"
                    )
                ]
                admission_owners = {owner for _name, owner in admission if owner}
                self.assertEqual(len(admission_owners), 1)
                phases.events.clear()

                resumed_writer = _CheckedExecutor(phases, scope)
                completed = service.resume(
                    snapshot.workspace_id,
                    expected_execution_run_id=interrupted.run_id,
                    recovery=_recovery(snapshot, interrupted),
                    executor=resumed_writer,
                    actor=LOCAL_ACTOR,
                    progress=publish,
                )

                self.assertEqual(phases.owner, 0)
                self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED)
                self.assertEqual(completed.committed_count, 3)
                self.assertEqual(completed, journal.run)
                self.assertTrue(all(row.attempt == 1 for row in completed.rows))
                self.assertEqual(progress[-1].committed_count, 3)
                first_remote = next(
                    index
                    for index, (_name, owner) in enumerate(phases.events)
                    if owner == 0
                )
                recovery_events = phases.events[:first_remote]
                self.assertIn(
                    "record_recovery", [name for name, _owner in recovery_events]
                )
                self.assertEqual(len({owner for _name, owner in recovery_events}), 1)

    def test_transfer_recovery_splits_local_work_between_remote_lookup_phases(self):
        for major, mode in product((19, 20), tuple(OdooConnectionMode)):
            with self.subTest(major=major, destination_mode=mode):
                phases = _LocalPhases()
                service, journal, snapshot = _transfer_service(major, mode, phases)
                scope = execution_api_scope(snapshot)
                observed_at = datetime.now(UTC).isoformat()
                read_identity = OdooReadIdentity(
                    target_hash=snapshot.target_hash,
                    principal_hash=fixtures.HASH,
                    permission_hash=fixtures.HASH,
                    context_hash=fixtures.HASH,
                    readable_models=snapshot.readable_models,
                    observed_at=observed_at,
                )
                write_identity = OdooWriteIdentity(
                    target_hash=snapshot.target_hash,
                    principal_hash=fixtures.HASH,
                    permission_hash=fixtures.HASH,
                    context_hash=fixtures.HASH,
                    readable_models=tuple(item.model for item in scope.models),
                    writable_models=tuple(
                        item.model for item in scope.models if item.write_fields
                    ),
                    observed_at=observed_at,
                )
                interrupted_writer = _TransferExecutor(
                    phases,
                    scope.semantic_hash,
                    target_hash=snapshot.target_hash,
                    interrupted=True,
                )
                progress = []
                publish = phases.external("progress", progress.append)

                with self.assertRaisesRegex(RuntimeError, "transfer interruption"):
                    service.execute_transfer(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        expected_preflight_hash=fixtures.HASH,
                        snapshot=snapshot,
                        executor=interrupted_writer,
                        actor=LOCAL_ACTOR,
                        read_identity=read_identity,
                        credential_binding_hash=fixtures.HASH,
                        write_identity=write_identity,
                        progress=publish,
                    )

                interrupted = journal.run
                self.assertEqual(phases.owner, 0)
                self.assertEqual(interrupted.status, ExecutionRunStatus.RUNNING)
                self.assertEqual(interrupted.in_flight_count, 1)
                start_index = next(
                    index
                    for index, (name, _owner) in enumerate(phases.events)
                    if name == "start_run"
                )
                self.assertEqual(
                    [
                        name
                        for name, owner in phases.events[:start_index]
                        if name.startswith("lookup:") and owner == 0
                    ],
                    [
                        "lookup:product.category",
                        "lookup:product.template",
                        "lookup:res.partner",
                    ],
                )
                recovery = replace(
                    _recovery(snapshot, interrupted),
                    verification_credential_binding_hash=fixtures.HASH,
                    verification_principal_hash=fixtures.HASH,
                    verification_permission_hash=fixtures.HASH,
                    verification_context_hash=fixtures.HASH,
                )
                phases.events.clear()
                writer = _TransferExecutor(
                    phases, scope.semantic_hash, target_hash=snapshot.target_hash
                )

                completed = service.resume_transfer(
                    snapshot.workspace_id,
                    expected_execution_run_id=interrupted.run_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    expected_preflight_hash=fixtures.HASH,
                    snapshot=snapshot,
                    recovery=recovery,
                    executor=writer,
                    actor=LOCAL_ACTOR,
                    read_identity=read_identity,
                    credential_binding_hash=fixtures.HASH,
                    write_identity=write_identity,
                    progress=publish,
                )

                self.assertEqual(phases.owner, 0)
                self.assertEqual(completed.status, ExecutionRunStatus.COMPLETED)
                self.assertEqual(completed, journal.run)
                self.assertEqual(completed.committed_count, 3)
                self.assertEqual(completed.rows[0].attempt, 2)
                self.assertTrue(all(row.recovery_hash for row in completed.rows))
                self.assertEqual(len(writer.loads), 2)
                self.assertEqual(writer.creates, [])
                self.assertEqual(len(writer.updates), 1)
                self.assertEqual(progress[-1].committed_count, 3)
                recovery_index = next(
                    index
                    for index, (name, _owner) in enumerate(phases.events)
                    if name == "record_recovery"
                )
                recovery_owner = phases.events[recovery_index][1]
                self.assertGreater(recovery_owner, 0)
                self.assertNotEqual(recovery_owner, phases.events[0][1])
                self.assertEqual(
                    phases.events[recovery_index + 1], ("get_run", recovery_owner)
                )
                self.assertEqual(
                    [
                        name
                        for name, _owner in phases.events[:recovery_index]
                        if name.startswith("lookup:")
                    ],
                    ["lookup:product.category", "lookup:product.template"],
                )
                self.assertEqual(
                    next(
                        (name, owner)
                        for name, owner in phases.events[recovery_index + 2 :]
                        if name.startswith("lookup:")
                    ),
                    ("lookup:res.partner", 0),
                )

    def test_receipt_failure_releases_owner_and_retains_uncertain_boundary(self):
        snapshot = fixtures._snapshot()
        phases = _LocalPhases()
        failure = ValueError("Synthetic durable receipt failure")
        service, journal = _service(
            snapshot, OdooConnectionMode.LOCAL, phases, receipt_error=failure
        )
        writer = _CheckedExecutor(phases, execution_api_scope(snapshot).semantic_hash)

        with self.assertRaises(ValueError) as raised:
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=writer,
                actor=LOCAL_ACTOR,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(phases.owner, 0)
        self.assertEqual(journal.run.in_flight_count, 1)
        self.assertEqual(journal.run.committed_count, 0)
        self.assertEqual(len(writer.creates), 1)
        self.assertEqual(writer.updates, [])

    def test_stale_admission_releases_owner_before_any_remote_work(self):
        snapshot = fixtures._snapshot()
        phases = _LocalPhases()
        service, journal = _service(snapshot, OdooConnectionMode.LOCAL, phases)
        writer = _CheckedExecutor(phases, execution_api_scope(snapshot).semantic_hash)

        with self.assertRaises(WorkspaceError) as raised:
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash="sha256:" + "9" * 64,
                executor=writer,
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(raised.exception.load_failure_code, "LOAD_PREVIEW_CHANGED")
        self.assertEqual(phases.owner, 0)
        self.assertTrue(all(owner for _name, owner in phases.events))
        self.assertIsNone(journal.run)
