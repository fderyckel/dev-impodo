"""Integration evidence for agent-readable load failures and privacy."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.application.workspace.execution.load_jobs import (
    LoadJobManager,
    LoadJobResult,
)
from impodo.application.workspace.execution.load_observation import (
    bind_load_observation,
    current_load_observation,
    emit_load_evidence,
    observe_load_stage,
    record_load_failure,
)
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.diagnostics import (
    LocalDiagnosticRecorder,
    RequestDiagnosticsMiddleware,
    create_diagnostic_bundle,
)
from impodo.web.load_diagnostics import (
    LOAD_EVENT_LOG,
    LOAD_SUMMARY_LOG,
    sanitize_load_record,
)
from tests.application.workspace.execution import test_service as execution_fixtures


def _bundle(recorder, *, attempt_id=None, max_records=5_000):
    recorder.flush()
    with ZipFile(
        BytesIO(
            create_diagnostic_bundle(
                recorder.path.parent,
                build_contract=PROCESS_BUILD_CONTRACT,
                attempt_id=attempt_id,
                max_records=max_records,
            )
        )
    ) as archive:
        members = {name: archive.read(name).decode() for name in archive.namelist()}
    return members, json.loads(members["load-attempts.json"])


def _access(workspace_id, *, recipe=False):
    return WorkspaceAccessContext(
        project_id=str(uuid4()),
        workspace_id=workspace_id,
        data_version_id=str(uuid4()),
        migration_run_id=str(uuid4()),
        recipe_application_id=str(uuid4()) if recipe else None,
        run_purpose="TEST" if recipe else "AUTHORING",
    )


def _run_job(recorder, workspace_id, work, *, recipe=False, action="LOAD"):
    manager = LoadJobManager()
    observer = recorder.load_observation(action=action, request_id=str(uuid4()))
    with bind_load_observation(observer):
        job = manager.enqueue(
            workspace_id,
            "Private project",
            target_database="private-database",
            target_server="private-host",
            target_environment="Test",
            total_rows=3,
            access_context=_access(workspace_id, recipe=recipe),
            actor=LOCAL_ACTOR,
            work=work,
        )
    deadline = time.monotonic() + 5
    while (
        not manager.get(workspace_id, job.job_id).terminal
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    manager.shutdown()
    finished = manager.get(workspace_id, job.job_id)
    if not observer.terminal:
        raise AssertionError("Worker did not publish terminal diagnostics")
    return finished, observer


class LoadDiagnosticIntegrationTests(unittest.TestCase):
    def test_transfer_loads_use_the_verified_major_and_share_the_durable_diagnostics(
        self,
    ):
        for major in (19, 20):
            for recipe in (False, True):
                with (
                    self.subTest(major=major, recipe=recipe),
                    tempfile.TemporaryDirectory() as directory,
                    ExitStack() as resources,
                ):
                    original = replace(
                        execution_fixtures._snapshot(), target_odoo_version=f"{major}.0"
                    )
                    service, journal, snapshot = (
                        execution_fixtures.ExecutionServiceTests()._transfer_service(
                            original
                        )
                    )
                    workspace = replace(
                        service.workspaces.get(snapshot.workspace_id),
                        destination_verified_odoo_version=f"{major}.0",
                    )
                    service.workspaces = SimpleNamespace(get=lambda _id: workspace)
                    scope = execution_api_scope(snapshot)
                    executor = execution_fixtures._Executor(
                        scope.semantic_hash,
                        lookup_ids=(),
                        lookup_results={("res.partner", (("ref", "=", "C1"),)): (50,)},
                    )
                    executor.target_hash = snapshot.target_hash
                    identity = dict(
                        target_hash=snapshot.target_hash,
                        principal_hash=execution_fixtures.HASH,
                        permission_hash=execution_fixtures.HASH,
                        context_hash=execution_fixtures.HASH,
                        observed_at=datetime.now(UTC).isoformat(),
                    )
                    read = OdooReadIdentity(
                        **identity, readable_models=snapshot.readable_models
                    )
                    write = OdooWriteIdentity(
                        **identity,
                        readable_models=tuple(item.model for item in scope.models),
                        writable_models=tuple(
                            item.model for item in scope.models if item.write_fields
                        ),
                    )
                    recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

                    def work(_access, writing, _verifying):
                        run = service.execute_transfer(
                            snapshot.workspace_id,
                            expected_snapshot_hash=snapshot.semantic_hash,
                            expected_preflight_hash=execution_fixtures.HASH,
                            snapshot=snapshot,
                            executor=executor,
                            actor=LOCAL_ACTOR,
                            read_identity=read,
                            credential_binding_hash=execution_fixtures.HASH,
                            write_identity=write,
                            progress=writing,
                        )
                        return LoadJobResult(run.run_id, False)

                    _run_job(
                        recorder,
                        snapshot.workspace_id,
                        work,
                        recipe=recipe,
                        action="TRANSFER_LOAD",
                    )
                    _members, attempts = _bundle(recorder)
                    self.assertEqual(attempts[0]["context"]["odoo_major"], major)
                    self.assertEqual(attempts[0]["context"]["source_mode"], "ODOO")
                    self.assertEqual(
                        attempts[0]["journal"]["row_states"]["COMMITTED"],
                        len(journal.run.rows),
                    )
                    self.assertEqual(attempts[0]["journal"]["projection"], "FINAL")
                    self.assertTrue(executor.loads)

    def test_failed_verification_keeps_commits_and_does_not_recommend_another_load(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            snapshot = execution_fixtures._snapshot()
            service, _journal = execution_fixtures.ExecutionServiceTests()._service(
                snapshot
            )
            executor = execution_fixtures._Executor(
                execution_api_scope(snapshot).semantic_hash
            )
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

            def work(_access, writing, _verifying):
                run = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                    progress=writing,
                )
                try:
                    with observe_load_stage("verification"):
                        raise WorkspaceError(
                            "secret verification detail",
                            load_failure_code="LOAD_PREVIEW_CHANGED",
                        )
                except WorkspaceError:
                    return LoadJobResult(run.run_id, False)

            _run_job(recorder, snapshot.workspace_id, work)
            members, attempts = _bundle(recorder)
            self.assertEqual(attempts[0]["failure"]["stage"], "verification")
            self.assertEqual(attempts[0]["journal"]["row_states"]["COMMITTED"], 3)
            self.assertIn(
                "Run verification from the saved outcome", members["diagnosis.md"]
            )

    def test_broken_diagnostic_sink_does_not_change_execution_and_adds_only_one_journal_read(
        self,
    ):
        from impodo.application.workspace.execution.load_observation import (
            LoadObservation,
        )

        snapshot = execution_fixtures._snapshot()
        service, journal = execution_fixtures.ExecutionServiceTests()._service(snapshot)
        executor = execution_fixtures._Executor(
            execution_api_scope(snapshot).semantic_hash
        )

        def broken(_event, _facts):
            raise OSError("Diagnostic storage unavailable")

        with (
            bind_load_observation(LoadObservation(str(uuid4()), broken)),
            patch.object(journal, "get_run", wraps=journal.get_run) as reads,
        ):
            run = service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
            )
        self.assertEqual(run.committed_count, 3)
        self.assertEqual(reads.call_count, 1)

    def test_guard_failure_is_explained_before_any_journal_or_write_for_both_majors(
        self,
    ):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for recipe in (False, True):
                    with (
                        self.subTest(major=major, mode=mode, recipe=recipe),
                        tempfile.TemporaryDirectory() as directory,
                        ExitStack() as resources,
                    ):
                        snapshot = replace(
                            execution_fixtures._snapshot(),
                            target_odoo_version=f"{major}.0",
                        )
                        service, journal = (
                            execution_fixtures.ExecutionServiceTests()._service(
                                snapshot, mode=mode
                            )
                        )
                        executor = execution_fixtures._Executor(
                            execution_api_scope(snapshot).semantic_hash
                        )
                        recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

                        def work(_access, _writing, _verifying):
                            service.execute(
                                snapshot.workspace_id,
                                expected_snapshot_hash="changed",
                                executor=executor,
                                actor=LOCAL_ACTOR,
                            )
                            self.fail("Stale confirmation was accepted")

                        job, observer = _run_job(
                            recorder, snapshot.workspace_id, work, recipe=recipe
                        )
                        members, attempts = _bundle(recorder)
                        attempt = attempts[0]
                        self.assertEqual(job.status.value, "FAILED")
                        self.assertEqual(job.support_reference, observer.attempt_id)
                        self.assertEqual(
                            attempt["failure"]["causes"][0]["code"],
                            "LOAD_PREVIEW_CHANGED",
                        )
                        self.assertTrue(attempt["failure"]["causes"][0]["frames"])
                        self.assertEqual(
                            attempt["transport_observation"], "NOT_STARTED"
                        )
                        self.assertEqual(attempt["context"]["odoo_major"], major)
                        self.assertEqual(
                            attempt["entry_point"], "RECIPE" if recipe else "AUTHORING"
                        )
                        self.assertIsNone(journal.run)
                        self.assertEqual(executor.creates, [])
                        self.assertIn(
                            "The load preview changed", members["diagnosis.md"]
                        )
                        self.assertIn(
                            "did not start a write call", members["diagnosis.md"]
                        )
                        self.assertNotIn("private-host", str(members))

    def test_lost_reply_reports_unknown_and_preserves_durable_rows_for_both_majors(
        self,
    ):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with (
                    self.subTest(major=major, mode=mode),
                    tempfile.TemporaryDirectory() as directory,
                    ExitStack() as resources,
                ):
                    snapshot = replace(
                        execution_fixtures._snapshot(), target_odoo_version=f"{major}.0"
                    )
                    service, journal = (
                        execution_fixtures.ExecutionServiceTests()._service(
                            snapshot, mode=mode
                        )
                    )
                    executor = execution_fixtures._Executor(
                        execution_api_scope(snapshot).semantic_hash, unknown=True
                    )
                    recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

                    def work(_access, writing, verifying):
                        run = service.execute(
                            snapshot.workspace_id,
                            expected_snapshot_hash=snapshot.semantic_hash,
                            executor=executor,
                            actor=LOCAL_ACTOR,
                            progress=writing,
                        )
                        verifying(run)
                        return LoadJobResult(run.run_id, False)

                    _run_job(recorder, snapshot.workspace_id, work)
                    members, attempts = _bundle(recorder)
                    attempt = attempts[0]
                    self.assertEqual(attempt["transport_observation"], "UNKNOWN")
                    self.assertGreater(
                        attempt["journal"]["row_states"]["OUTCOME_UNKNOWN"], 0
                    )
                    self.assertEqual(
                        attempt["journal"]["execution_run_id"], journal.run.run_id
                    )
                    self.assertIn(
                        "Do not resend an uncertain write", members["diagnosis.md"]
                    )
                    self.assertEqual(current_load_observation(), None)

    def test_rejection_after_commits_keeps_specific_odoo_reason_and_totals(self):
        for major in (19, 20):
            with self.subTest(major=major), tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
                snapshot = replace(
                    execution_fixtures._snapshot(), target_odoo_version=f"{major}.0"
                )
                service, _journal = execution_fixtures.ExecutionServiceTests()._service(
                    snapshot
                )
                diagnostic = OdooWriteDiagnostic(
                    WriteFailureCode.REQUIRED_VALUE,
                    "res.partner",
                    "write",
                    fields=("name",),
                )
                executor = execution_fixtures._Executor(
                    execution_api_scope(snapshot).semantic_hash,
                    update_error=OdooWriteRejected(
                        "private server payload", diagnostic=diagnostic
                    ),
                )
                recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

                def work(_access, writing, _verifying):
                    run = service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                        progress=writing,
                    )
                    return LoadJobResult(run.run_id, False)

                _run_job(recorder, snapshot.workspace_id, work)
                members, attempts = _bundle(recorder)
                self.assertGreater(attempts[0]["journal"]["row_states"]["COMMITTED"], 0)
                self.assertEqual(attempts[0]["journal"]["row_states"]["FAILED"], 1)
                self.assertIn("ODOO_REQUIRED_VALUE_MISSING", members["diagnosis.md"])
                self.assertIn("name", members["diagnosis.md"])
                self.assertNotIn("private server payload", str(members))

    def test_fatal_exception_after_send_retains_original_failure_if_journal_capture_fails(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            snapshot = execution_fixtures._snapshot()
            service, journal = execution_fixtures.ExecutionServiceTests()._service(
                snapshot
            )
            executor = execution_fixtures._Executor(
                execution_api_scope(snapshot).semantic_hash
            )
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

            def crash(model, values):
                journal.get_run = lambda *_args: (_ for _ in ()).throw(
                    OSError("private path")
                )
                raise RuntimeError("private source cell and API key")

            executor.create_rows = crash

            def work(_access, _writing, _verifying):
                service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=executor,
                    actor=LOCAL_ACTOR,
                )

            job, _observer = _run_job(recorder, snapshot.workspace_id, work)
            members, attempts = _bundle(recorder)
            self.assertEqual(job.status.value, "FAILED")
            self.assertEqual(
                attempts[0]["failure"]["causes"][0]["exception_class"], "RuntimeError"
            )
            self.assertEqual(attempts[0]["transport_observation"], "UNKNOWN")
            self.assertIn('"event":"capture_failed"', members["load-events.jsonl"])
            self.assertNotIn("private source", str(members))
            self.assertNotIn("private path", str(members))

    def test_terminal_failure_survives_restart_request_flood_and_missing_event_log(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))

            def work(*_args):
                raise WorkspaceError(
                    "sensitive message", load_failure_code="LOAD_PREVIEW_CHANGED"
                )

            _job, observer = _run_job(recorder, str(uuid4()), work)
            for _ in range(5_005):
                recorder.record_request(
                    request_id=str(uuid4()),
                    method="GET",
                    route_template="/health",
                    status_code=200,
                    duration_ms=1,
                )
            recorder.close()
            (Path(directory) / LOAD_EVENT_LOG).unlink()
            reopened = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))
            members, attempts = _bundle(reopened, max_records=5)
            self.assertEqual(attempts[0]["attempt_id"], observer.attempt_id)
            self.assertIn("The load preview changed", members["diagnosis.md"])
            self.assertGreater(attempts[0]["omitted_recorded_event_count"], 0)
            self.assertTrue(
                any("omitted" in item for item in attempts[0]["evidence_gaps"])
            )
            manifest = json.loads(members["manifest.json"])
            self.assertGreater(
                manifest["coverage"]["general"]["omitted_record_count"], 0
            )

    def test_limits_incomplete_attempts_selection_and_unknown_custom_reasons_are_explicit(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))
            first = recorder.load_observation(action="LOAD", request_id=str(uuid4()))
            with bind_load_observation(first):
                with patch("impodo.web.load_diagnostics.MAX_ATTEMPT_EVENTS", 3):
                    for _ in range(12):
                        emit_load_evidence("stage_started", stage="write")
                first.emit("terminal", outcome="FAILED")
            incomplete = recorder.load_observation(
                action="RECOVER", request_id=str(uuid4())
            )
            members, attempts = _bundle(recorder, attempt_id=first.attempt_id)
            self.assertEqual(len(attempts), 1)
            self.assertGreater(attempts[0]["omitted_event_count"], 0)
            self.assertIn("cause was not recorded", members["diagnosis.md"])
            _, attempts = _bundle(recorder, attempt_id=incomplete.attempt_id)
            self.assertTrue(attempts[0]["terminal_missing"])
            self.assertEqual(attempts[0]["transport_observation"], "UNKNOWN")

    def test_exception_causes_and_tampered_logs_cannot_export_messages_or_locals(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))
            observer = recorder.load_observation(action="LOAD", request_id=str(uuid4()))
            with bind_load_observation(observer):
                try:
                    try:
                        raise ValueError("SECRET_SOURCE")
                    except ValueError as cause:
                        raise WorkspaceError(
                            "SECRET_TOKEN", load_failure_code="SECRET_CODE"
                        ) from cause
                except WorkspaceError as error:
                    record_load_failure(error)
                observer.emit("terminal", outcome="FAILED")
            with (Path(directory) / LOAD_SUMMARY_LOG).open("a") as stream:
                stream.write(json.dumps({"event": ["SECRET_EVENT"]}) + "\n")
                stream.write("not JSON SECRET_BODY\n")
            members, attempts = _bundle(recorder)
            self.assertEqual(len(attempts[0]["failure"]["causes"]), 2)
            self.assertNotIn("SECRET_", str(members))
            self.assertGreater(
                json.loads(members["manifest.json"])["load_evidence"][
                    "malformed_record_count"
                ],
                0,
            )
            self.assertIsNone(sanitize_load_record({"event": []}))


class LoadRequestDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_submission_is_correlated_and_thread_context_is_restored(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))
            captured = {}

            async def app(scope, _receive, send):
                captured["reference"] = current_load_observation().attempt_id
                with observe_load_stage("submission_preview"):
                    try:
                        raise WorkspaceError(
                            "private request value",
                            load_failure_code="LOAD_PREVIEW_CHANGED",
                        )
                    except WorkspaceError as error:
                        record_load_failure(error)
                await send(
                    {"type": "http.response.start", "status": 422, "headers": []}
                )
                await send({"type": "http.response.body", "body": b""})

            messages = []

            async def send(message):
                messages.append(message)

            await RequestDiagnosticsMiddleware(app, recorder=recorder)(
                {
                    "type": "http",
                    "method": "POST",
                    "path": f"/workspaces/{uuid4()}/load",
                },
                None,
                send,
            )
            members, attempts = _bundle(recorder)
            self.assertEqual(attempts[0]["attempt_id"], captured["reference"])
            self.assertEqual(attempts[0]["outcome"], "FAILED")
            self.assertIn(
                (b"x-impodo-support-reference", captured["reference"].encode()),
                messages[0]["headers"],
            )
            self.assertIsNone(current_load_observation())
            self.assertNotIn("private request value", str(members))

    async def test_redirect_to_background_worker_keeps_the_failure_correlated(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            recorder = resources.enter_context(closing(LocalDiagnosticRecorder(directory)))
            manager = LoadJobManager()
            resources.callback(manager.shutdown)
            workspace_id = str(uuid4())
            messages = []

            def work(*_args):
                raise WorkspaceError(
                    "secret message", load_failure_code="LOAD_PREVIEW_CHANGED"
                )

            async def app(_scope, _receive, send):
                manager.enqueue(
                    workspace_id,
                    "Project",
                    target_database="private",
                    target_server="private",
                    target_environment="Test",
                    total_rows=1,
                    access_context=_access(workspace_id),
                    actor=LOCAL_ACTOR,
                    work=work,
                )
                await send(
                    {"type": "http.response.start", "status": 303, "headers": []}
                )
                await send({"type": "http.response.body", "body": b""})

            async def send(message):
                messages.append(message)

            await RequestDiagnosticsMiddleware(app, recorder=recorder)(
                {
                    "type": "http",
                    "method": "POST",
                    "path": f"/workspaces/{workspace_id}/load",
                },
                None,
                send,
            )
            deadline = time.monotonic() + 5
            while (
                not manager.latest(workspace_id).terminal
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.005)
            manager.shutdown()
            members, attempts = _bundle(recorder)
            self.assertEqual(messages[0]["status"], 303)
            self.assertEqual(attempts[0]["outcome"], "FAILED")
            self.assertEqual(attempts[0]["job_id"], manager.latest(workspace_id).job_id)
            self.assertIn("The load preview changed", members["diagnosis.md"])
