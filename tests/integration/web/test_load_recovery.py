"""Exercise explicit prepared-data recovery without repeating a saved load."""

from contextlib import contextmanager, ExitStack
from dataclasses import replace
from threading import Event

from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.target_credentials import target_write_credential_id
from tests.support.browser_scenarios import (
    ExecutionRowStatus, ExecutionRunStatus, OdooReadIdentity, OdooWriteIdentity,
    POST_HEADERS, ProjectSetupBrowserTestCase, SimpleNamespace,
    TargetCredentialRole, _wait_for_load, patch, store_target_credential,
)


class LoadRecoveryBrowserTests(ProjectSetupBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.context = context = self.app.state.context
        workspace = self.workspaces.create(name="Interrupted product load", source_system="Other")
        self.workspace = context.workspace_states.update_target(
            workspace.workspace_id, actor=context.actor,
            expected_revision=workspace.revision, odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.test", odoo_database="migration",
            intended_applications=("Contacts",), intended_models=(),
        )
        self.read_key = store_target_credential(
            context.secret_store, self.workspace, TargetCredentialRole.READ,
            "read-secret", persistent=False,
        )
        self.write_key = store_target_credential(
            context.secret_store, self.workspace, TargetCredentialRole.WRITE,
            "write-secret", persistent=False,
        )
        self.run = SimpleNamespace(
            run_id="11111111-1111-4111-8111-111111111111",
            workspace_id=self.workspace.workspace_id,
            status=ExecutionRunStatus.RUNNING, rows=(), total_count=5,
            committed_count=2, planned_count=2, in_flight_count=1,
            retry_ready_count=0, unknown_count=0, partially_applied_count=0,
            failed_count=0, blocked_count=0,
            write_credential_binding_hash=self.write_key.binding_hash,
        )
        self.snapshot = SimpleNamespace(
            semantic_hash="sha256:" + "a" * 64, target_hash="sha256:" + "b" * 64,
            target_database="migration", target_odoo_version="19.0", write_count=5,
            counts={"CREATE": 5, "UPDATE": 0, "UNCHANGED": 0},
            readable_models=("res.partner",), read_context_hash="sha256:" + "c" * 64,
            read_credential_binding_hash=self.read_key.binding_hash,
        )
        self.preview = SimpleNamespace(
            snapshot=self.snapshot, api_scope=SimpleNamespace(models=()),
            scope_error="", current_run=self.run, can_load=False, datasets=(),
        )
        self.read_identity = OdooReadIdentity(
            target_hash=self.snapshot.target_hash, principal_hash="sha256:" + "d" * 64,
            permission_hash="sha256:" + "e" * 64,
            context_hash=self.snapshot.read_context_hash, readable_models=("res.partner",),
            observed_at="2026-08-23T00:00:00Z",
        )
        self.write_identity = OdooWriteIdentity(
            target_hash=self.snapshot.target_hash, principal_hash="sha256:" + "1" * 64,
            permission_hash="sha256:" + "2" * 64,
            context_hash=self.snapshot.read_context_hash, readable_models=("res.partner",),
            writable_models=("res.partner",), observed_at="2026-08-23T00:00:00Z",
        )
        self.completed = SimpleNamespace(
            run_id=self.run.run_id, total_count=5, planned_count=0,
            status=ExecutionRunStatus.COMPLETED,
            rows=tuple(SimpleNamespace(operation="CREATE", status=ExecutionRowStatus.COMMITTED)
                       for _ in range(5)),
        )
        self.events = []
        self.recovery = object()

    @contextmanager
    def recovery_context(self, *, assessment_error=None, preview_reader=None, assess=None):
        context = self.context

        def assessment(*args, **kwargs):
            self.events.append("assess")
            if assessment_error:
                raise assessment_error
            if assess:
                assess()
            return self.recovery

        def writer(*args):
            self.events.append("writer")
            return object()

        def resume(*args, **kwargs):
            self.events.append("resume")
            kwargs["progress"](self.completed)
            return self.completed

        def verify(*args, **kwargs):
            self.events.append("verify")
            return SimpleNamespace(unknown_count=0, fallout_count=0)

        with ExitStack() as stack:
            mocks = {}
            def patched(name, owner, method, **kwargs):
                mocks[name] = stack.enter_context(patch.object(owner, method, **kwargs))
            patched("preview", type(context.execution), "current_preview",
                    **({"side_effect": preview_reader} if preview_reader else {"return_value": self.preview}))
            patched("current", type(context.reconciliation), "current", return_value=None)
            patched("assess", type(context.reconciliation), "assess_recovery", side_effect=assessment)
            patched("resume", type(context.execution), "resume", side_effect=resume)
            patched("execute", type(context.execution), "execute")
            patched("verify", type(context.reconciliation), "reconcile", side_effect=verify)
            patched("cutover", type(context.cutover_plans), "assert_application_can_execute", return_value=None)
            patched("authority", type(context.production_runs), "assert_execution_authority", return_value=None)
            patched("owner", type(context.production_runs), "credential_workspace", return_value=self.workspace)
            patched("schema", type(context.queries), "get_odoo_schema_catalog",
                    return_value=SimpleNamespace(read_context_hash=self.snapshot.read_context_hash))
            patched("read_identity", context, "read_identity_probe", return_value=self.read_identity)
            patched("write_identity", context, "write_identity_probe", return_value=self.write_identity)
            patched("writer", context, "write_executor_factory", side_effect=writer)
            patched("reader", context, "readback_reader_factory", return_value=object())
            yield mocks

    def submit(self, **overrides):
        data = {"csrf_token": self.csrf, "snapshot_hash": self.snapshot.semantic_hash,
                "execution_run_id": self.run.run_id}
        data.update(overrides)
        return self.client.post(
            f"/workspaces/{self.workspace.workspace_id}/load/recover",
            data=data, headers=POST_HEADERS, follow_redirects=False,
        )

    def test_interrupted_pages_explain_counts_and_offer_explicit_recovery(self):
        with self.recovery_context() as mocks:
            review = self.client.get(f"/workspaces/{self.workspace.workspace_id}/load/review")
            outcome = self.client.get(f"/workspaces/{self.workspace.workspace_id}/load/outcome")
            self.assertEqual(outcome.status_code, 200, outcome.text)
            self.assertIn("Review interrupted load", review.text)
            self.assertIn("2 accepted", outcome.text)
            self.assertIn("2 waiting to load", outcome.text)
            self.assertIn("1 uncertain", outcome.text)
            self.assertIn("Assess and resume interrupted load", outcome.text)
            self.assertNotIn("Verify what happened in Odoo", outcome.text)
            self.assertNotIn("write-secret", outcome.text)
            mocks["assess"].assert_not_called()
            mocks["writer"].assert_not_called()

    def test_fresh_confirmation_binds_the_prior_interrupted_run(self):
        self.preview.current_run = None
        self.preview.can_load = True
        self.preview.prior_interrupted_run = self.run
        with self.recovery_context():
            page = self.client.get(f"/workspaces/{self.workspace.workspace_id}/load/confirm")
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIn('name="prior_execution_run_id"', page.text)
            self.assertIn(self.run.run_id, page.text)
            self.assertIn("closes the earlier attempt", page.text)

    def test_fresh_load_closes_prior_journal_before_writer_and_verifies(self):
        self.preview.current_run = None
        self.preview.can_load = True
        self.preview.prior_interrupted_run = self.run
        def execute(*args, **kwargs):
            self.events.append("execute")
            kwargs["progress"](self.completed)
            return self.completed
        with self.recovery_context() as mocks, patch.object(
            type(self.context.execution), "close_interrupted_run_for_recomparison",
            side_effect=lambda *args, **kwargs: self.events.append("close"),
        ) as close:
            mocks["execute"].side_effect = execute
            response = self.client.post(f"/workspaces/{self.workspace.workspace_id}/load",
                data={"csrf_token": self.csrf, "snapshot_hash": self.snapshot.semantic_hash,
                      "batch_rows": "10", "prior_execution_run_id": self.run.run_id},
                headers=POST_HEADERS, follow_redirects=False)
            self.assertEqual(response.status_code, 303, response.text)
            job = _wait_for_load(self.client, response.headers["location"])
            self.assertEqual(job["status"], "SUCCEEDED", job)
            self.assertEqual(self.events, ["close", "writer", "execute", "verify"])
            self.assertEqual(close.call_args.kwargs["expected_execution_run_id"], self.run.run_id)
            self.assertEqual(close.call_args.kwargs["expected_snapshot_hash"], self.snapshot.semantic_hash)
            mocks["resume"].assert_not_called()

    def test_fresh_load_requires_prior_binding_and_safe_closure(self):
        self.preview.current_run = None
        self.preview.can_load = True
        self.preview.prior_interrupted_run = self.run
        with self.recovery_context() as mocks, patch.object(
            type(self.context.execution), "close_interrupted_run_for_recomparison",
            side_effect=WorkspaceError("An original receipt changed"),
        ) as close:
            for prior_id in ("", "stale-run"):
                response = self.client.post(f"/workspaces/{self.workspace.workspace_id}/load",
                    data={"csrf_token": self.csrf, "snapshot_hash": self.snapshot.semantic_hash,
                          "batch_rows": "10", "prior_execution_run_id": prior_id},
                    headers=POST_HEADERS, follow_redirects=False)
                self.assertEqual(response.status_code, 422, response.text)
            close.assert_not_called()
            response = self.client.post(f"/workspaces/{self.workspace.workspace_id}/load",
                data={"csrf_token": self.csrf, "snapshot_hash": self.snapshot.semantic_hash,
                      "batch_rows": "10", "prior_execution_run_id": self.run.run_id},
                headers=POST_HEADERS, follow_redirects=False)
            self.assertEqual(response.status_code, 303, response.text)
            job = _wait_for_load(self.client, response.headers["location"])
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("receipt changed", job["failure_message"])
            mocks["writer"].assert_not_called()
            mocks["execute"].assert_not_called()

    def test_resume_assesses_before_writer_and_verifies_same_run(self):
        with self.recovery_context() as mocks:
            started = self.submit()
            self.assertEqual(started.status_code, 303, started.text)
            finished = _wait_for_load(self.client, started.headers["location"])
            self.assertEqual(finished["status"], "SUCCEEDED", finished)
            self.assertTrue(finished["verification_complete"])
            self.assertEqual(self.events, ["assess", "writer", "resume", "verify"])
            mocks["execute"].assert_not_called()
            for method in ("assess", "resume", "verify"):
                kwargs = mocks[method].call_args.kwargs
                self.assertEqual(kwargs["expected_execution_run_id"], self.run.run_id)
                self.assertEqual(kwargs["write_credential_binding_hash"], self.write_key.binding_hash)
                self.assertEqual(kwargs["write_identity"], self.write_identity)
            self.assertIs(mocks["resume"].call_args.kwargs["recovery"], self.recovery)
            self.assertNotIn("batch_rows", mocks["resume"].call_args.kwargs)

    def test_failed_assessment_never_constructs_writer(self):
        with self.recovery_context(assessment_error=WorkspaceError("Earlier Odoo record changed")) as mocks:
            started = self.submit()
            finished = _wait_for_load(self.client, started.headers["location"])
            self.assertEqual(finished["status"], "FAILED", finished)
            self.assertIn("Earlier Odoo record changed", finished["failure_message"])
            mocks["writer"].assert_not_called()
            mocks["resume"].assert_not_called()

    def test_changed_company_context_stops_before_assessment_or_writer(self):
        with self.recovery_context() as mocks:
            mocks["write_identity"].return_value = replace(
                self.write_identity, context_hash="sha256:" + "f" * 64,
            )
            started = self.submit()
            finished = _wait_for_load(self.client, started.headers["location"])
            self.assertEqual(finished["status"], "FAILED", finished)
            self.assertIn("reviewed Odoo context", finished["failure_message"])
            mocks["assess"].assert_not_called()
            mocks["writer"].assert_not_called()
            mocks["resume"].assert_not_called()

    def test_stale_run_snapshot_terminal_run_and_replaced_key_cannot_start(self):
        with self.recovery_context() as mocks:
            for overrides in ({"execution_run_id": "another-run"}, {"snapshot_hash": "stale"}):
                with self.subTest(overrides=overrides):
                    self.assertEqual(self.submit(**overrides).status_code, 422)
            self.preview.current_run = None
            self.assertEqual(self.submit().status_code, 422)
            self.preview.current_run = self.run
            self.run.status = ExecutionRunStatus.COMPLETED
            self.assertEqual(self.submit().status_code, 422)
            self.run.status = ExecutionRunStatus.RUNNING
            store_target_credential(self.context.secret_store, self.workspace,
                                    TargetCredentialRole.WRITE, "replacement", persistent=False)
            response = self.submit()
            self.assertEqual(response.status_code, 422)
            self.assertIn("original credential binding", response.text)
            mocks["assess"].assert_not_called()
            mocks["writer"].assert_not_called()

    def test_rechecks_preview_when_background_worker_starts(self):
        reads = 0
        def preview_reader(workspace_id):
            nonlocal reads
            reads += 1
            return self.preview if reads == 1 else None
        with self.recovery_context(preview_reader=preview_reader) as mocks:
            started = self.submit()
            finished = _wait_for_load(self.client, started.headers["location"])
            self.assertEqual(finished["status"], "FAILED", finished)
            self.assertIn("changed before recovery", finished["failure_message"])
            mocks["assess"].assert_not_called()
            mocks["writer"].assert_not_called()

    def test_duplicate_submission_returns_active_recovery(self):
        entered, release = Event(), Event()
        def assess():
            entered.set()
            if not release.wait(10):
                raise AssertionError("Recovery test did not release the reader")
        with self.recovery_context(assess=assess) as mocks:
            started = self.submit()
            try:
                self.assertTrue(entered.wait(5))
                repeated = self.submit()
                self.assertEqual(repeated.headers["location"], started.headers["location"])
            finally:
                release.set()
                finished = _wait_for_load(self.client, started.headers["location"])
            self.assertEqual(finished["status"], "SUCCEEDED", finished)
            mocks["resume"].assert_called_once()

    def test_csrf_and_new_write_parameters_are_rejected(self):
        with self.recovery_context() as mocks:
            self.assertEqual(self.submit(csrf_token="invalid").status_code, 403)
            for extra in ({"batch_rows": "100"}, {"write_api_key": "replacement"}):
                with self.subTest(extra=extra):
                    self.assertEqual(self.submit(**extra).status_code, 422)
            mocks["assess"].assert_not_called()
            mocks["writer"].assert_not_called()

    def test_missing_loading_key_does_not_fall_back_to_read_key(self):
        self.context.secret_store.delete(target_write_credential_id(self.workspace))
        with self.recovery_context() as mocks:
            page = self.client.get(f"/workspaces/{self.workspace.workspace_id}/load/outcome")
            self.assertIn("original saved loading key is unavailable", page.text)
            self.assertNotIn("/load/recover", page.text)
            response = self.submit()
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("Recovery requires the original saved loading key", response.text)
            mocks["assess"].assert_not_called()
            mocks["writer"].assert_not_called()
