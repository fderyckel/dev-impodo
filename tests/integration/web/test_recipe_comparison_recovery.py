"""Keep Recipe comparison pages within their authorized workspace."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.application.odoo_read_failures import OdooReadWorkflowError
from impodo.application.preflight_attempts import PreflightComparisonRequest
from impodo.application.preflight_jobs import PreflightJobManager
from impodo.application.preflight_service import EXECUTION_SNAPSHOT_NAME, MANIFEST_NAME
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import target_read_credential_id
from tests.integration.web import test_fresh_data_controls as fixtures
from tests.support.identity import resolve_local_actor
from tests.support.recipe_lifecycle import metadata_for_schema, prepare_test_application


class RecipeComparisonRecoveryBrowserTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root_patch = patch.object(fixtures.fixtures, "ROOT", Path(temporary.name))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.fixture = fixtures.FreshDataControlBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.context, self.client = self.fixture.context, self.fixture.client

    def _approved_review(self):
        fresh_url = self.fixture.registered_delivery()
        fresh = self.client.get(fresh_url)
        accepted = self.client.post(fresh_url + "/accept", data={
            "csrf_token": fixtures.field(fresh, "csrf_token"), "parameter_revision": "",
            "warnings_acknowledged": "1", "control_0": "125.50",
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        application = self.fixture._activate_with_real_compiler()
        workspace_id = application.workspace_id
        context, actor = self.context, resolve_local_actor(self.context)
        summary, evaluation, _ = context.normalization.current_review(workspace_id)
        for group in evaluation.groups:
            if group.requires_decision:
                summary = context.normalization.decide_group(workspace_id,
                    summary.run_id, group.group_id, approve=True,
                    expected_version=summary.lifecycle_version, actor=actor)
        context.normalization.approve(workspace_id, summary.run_id,
            expected_version=summary.lifecycle_version, actor=actor)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)

        def reader(requirements):
            metadata = metadata_for_schema(schema, {
                item.model: item.fields for item in requirements.metadata_requests
            })
            fields = {item.model: item.fields for item in requirements.record_requests}
            return metadata, RecordSnapshot(metadata.fingerprint, {model: () for model in fields}, fields)

        return application, reader

    def _request(self, application):
        workspace = self.context.migration_workspaces.get(
            application.workspace_id, actor=resolve_local_actor(self.context))
        schema = self.context.queries.get_odoo_schema_catalog(workspace.workspace_id)
        return PreflightComparisonRequest(
            operation_id=str(uuid4()), project_id=workspace.project_id,
            workspace_id=workspace.workspace_id, data_version_id=workspace.data_version_id,
            migration_run_id=workspace.migration_run_id,
            recipe_application_id=workspace.recipe_application_id,
            workspace_revision=workspace.optimistic_revision, requested_at=datetime.now(UTC),
            read_credential_binding_hash=schema.read_credential_binding_hash,
            semantic_input_hash=self.context.preflight.comparison_input_hash(
                workspace.workspace_id, read_credential_binding_hash=schema.read_credential_binding_hash),
        )

    def _change_workspace_revision(self, workspace_id):
        state = self.context.workspace_states.repository.get(workspace_id)
        self.context.workspace_states.repository.save(
            replace(state, revision=state.revision + 1, name="Updated local review"),
            expected_revision=state.revision, event_type="WORKSPACE_DETAILS_UPDATED",
            event_detail="Fictional revision update", actor=resolve_local_actor(self.context))

    def test_committed_artifacts_survive_registration_sync_failure_and_restart(self):
        application, reader = self._approved_review()
        context, actor = self.context, resolve_local_actor(self.context)
        request = self._request(application)
        store = context.migration_projects.repository
        store.reserve_preflight_comparison(request, actor=actor)
        with (
            patch.object(context.preflight.preflight._workspaces,
                "synchronize_registration_artifacts", side_effect=OSError("private sync detail")),
            self.assertLogs("impodo.application.preflight_service", level="WARNING") as logged,
        ):
            report = context.preflight.compare(application.workspace_id, reader=reader,
                actor=actor, operation_id=request.operation_id, comparison_request=request,
                local_operation=run_local_operation_sync)
        self.assertNotIn("private sync detail", " ".join(logged.output))
        self.assertEqual(context.preflight.current_report(application.workspace_id).run_id, report.run_id)
        manager = PreflightJobManager(store)
        manager.reconcile_pending(context.preflight.recover_comparison_receipt)
        recovered = manager.get(application.workspace_id, request.operation_id)
        self.assertEqual(recovered.status.value, "SUCCEEDED")
        self.assertEqual(recovered.preflight_run_id, report.run_id)
        for filename in (MANIFEST_NAME, EXECUTION_SNAPSHOT_NAME):
            with context.artifacts.materialize_report(application.workspace_id, report.run_id, filename) as path:
                self.assertTrue(path.is_file())

    def test_transaction_failure_preserves_previous_report_and_removes_uncommitted_artifacts(self):
        application, reader = self._approved_review()
        context, actor = self.context, resolve_local_actor(self.context)
        previous = context.preflight.compare(application.workspace_id, reader=reader,
            actor=actor, operation_id=str(uuid4()), local_operation=run_local_operation_sync)
        with (
            patch.object(context.preflight.preflight, "_insert_workspace_audit", side_effect=OSError("before commit")),
            patch.object(context.artifacts, "write_report", wraps=context.artifacts.write_report) as writes,
            self.assertRaisesRegex(OSError, "before commit"),
        ):
            context.preflight.compare(application.workspace_id, reader=reader,
                actor=actor, operation_id=str(uuid4()), local_operation=run_local_operation_sync)
        self.assertEqual(context.preflight.current_report(application.workspace_id).run_id, previous.run_id)
        self.assertEqual(context.preflight.comparison_receipt(previous).preflight_run_id, previous.run_id)
        for call in writes.call_args_list:
            path = context.artifacts._report_path(*call.args[:3], create=False)
            self.assertFalse(path.exists())

    def test_background_recipe_comparison_publishes_its_verified_receipt(self):
        application, reader = self._approved_review()
        context = self.context
        manager = PreflightJobManager(context.migration_projects.repository)
        context.preflight_jobs = manager
        operation_id = str(uuid4())
        summary_url = f"/workspaces/{application.workspace_id}/summary"
        try:
            page = self.client.get(summary_url)
            with patch(
                "impodo.web.routers.preflight._read_readiness_snapshots",
                side_effect=lambda _context, _state, requirements, **kwargs: reader(requirements),
            ):
                started = self.client.post(
                    summary_url + "/compare",
                    data={
                        "csrf_token": fixtures.field(page, "csrf_token"),
                        "operation_id": operation_id,
                    },
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False,
                )
                self.assertEqual(started.status_code, 303, started.text)
                self.assertIn("/preflight/", started.headers["location"])
                deadline = monotonic() + 30
                while monotonic() < deadline:
                    job = manager.get(application.workspace_id, operation_id)
                    if job.terminal:
                        break
                    sleep(0.01)
                else:
                    self.fail("The local comparison worker did not finish")
            self.assertEqual(job.status.value, "SUCCEEDED", job.failure)
            report = context.preflight.current_report(application.workspace_id)
            self.assertEqual(report.operation_id, operation_id)
            self.assertEqual(job.preflight_run_id, report.run_id)
            self.assertEqual(context.run_planning.repository.get_application(
                application.application_id).status, RecipeApplicationStatus.COMPARED)
        finally:
            manager.shutdown()
            context.preflight_jobs = None

    def test_reserved_inputs_are_checked_before_read_and_before_publication(self):
        application, reader = self._approved_review()
        context, actor = self.context, resolve_local_actor(self.context)
        previous = context.preflight.compare(application.workspace_id, reader=reader,
            actor=actor, operation_id=str(uuid4()), local_operation=run_local_operation_sync)
        for boundary in (
            "queued", "reading", "classifying", "queued_credential", "reading_credential"
        ):
            with self.subTest(boundary=boundary):
                request = self._request(application)
                calls = []
                current_binding = [request.read_credential_binding_hash]

                def read(requirements):
                    calls.append("read")
                    if boundary == "reading":
                        self._change_workspace_revision(application.workspace_id)
                    if boundary == "reading_credential":
                        current_binding[0] = "sha256:" + "a" * 64
                    return reader(requirements)

                original_engine = context.preflight.engine.run
                def classify(*args, **kwargs):
                    result = original_engine(*args, **kwargs)
                    if boundary == "classifying":
                        self._change_workspace_revision(application.workspace_id)
                    return result

                if boundary == "queued":
                    self._change_workspace_revision(application.workspace_id)
                if boundary == "queued_credential":
                    current_binding[0] = "sha256:" + "a" * 64
                with (
                    patch.object(context.preflight.engine, "run", side_effect=classify),
                    self.assertRaises((OdooReadWorkflowError, WorkspaceError)) as raised,
                ):
                    context.preflight.compare(application.workspace_id, reader=read,
                        actor=actor, operation_id=request.operation_id, comparison_request=request,
                        current_read_credential_binding_hash=lambda: current_binding[0],
                        local_operation=run_local_operation_sync)
                self.assertIn("changed", str(raised.exception))
                self.assertEqual(calls, [] if boundary.startswith("queued") else ["read"])
                self.assertEqual(context.preflight.current_report(application.workspace_id).run_id, previous.run_id)

    def test_saved_comparison_renders_and_reconnects_shared_run_credentials(self):
        fresh_url = self.fixture.registered_delivery()
        fresh = self.client.get(fresh_url)
        accepted = self.client.post(fresh_url + "/accept", data={
            "csrf_token": fixtures.field(fresh, "csrf_token"), "parameter_revision": "",
            "warnings_acknowledged": "1", "control_0": "125.50",
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        application = self.fixture._activate_with_real_compiler(prepare=False)
        workspace_id = application.workspace_id
        context, actor = self.context, resolve_local_actor(self.context)
        prepare_test_application(self, context, application, self.fixture.fixture.root)
        run_url = f"/projects/{application.project_id}/runs/{application.migration_run_id}"
        run_page = self.client.get(run_url)
        self.assertEqual(run_page.status_code, 200, run_page.text)
        self.assertIn('class="sidebar-workflow"', run_page.text)
        self.assertIn('class="steps steps-3"', run_page.text)
        self.assertIn('<dt>Odoo database</dt><dd>fictional_test</dd>', run_page.text)
        self.assertIn('<dt>Odoo version reported by target</dt><dd>19.0</dd>', run_page.text)
        self.assertLess(run_page.text.index('data-run-next-action'),
                        run_page.text.index('id="run-progress-title"'))
        review = run_page.context["review"]
        self.assertEqual(review.current_card.action_label, "Review prepared data")
        prepared_review = self.client.get(review.current_card.action_url)
        self.assertEqual(prepared_review.status_code, 200, prepared_review.text)
        self.assertEqual(prepared_review.url.path, f"/workspaces/{workspace_id}/normalization")
        summary, evaluation, _ = context.normalization.current_review(workspace_id)
        for group in evaluation.groups:
            if group.requires_decision:
                summary = context.normalization.decide_group(
                    workspace_id, summary.run_id, group.group_id, approve=True,
                    expected_version=summary.lifecycle_version, actor=actor,
                )
        context.normalization.approve(workspace_id, summary.run_id,
            expected_version=summary.lifecycle_version, actor=actor)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)

        def empty_target(requirements):
            self.assertTrue(all(item.domain for item in requirements.record_requests))
            metadata = metadata_for_schema(schema, {
                item.model: item.fields for item in requirements.metadata_requests
            })
            fields = {item.model: item.fields for item in requirements.record_requests}
            return metadata, RecordSnapshot(metadata.fingerprint, {model: () for model in fields}, fields)

        # Reproduce a persisted comparison whose run-progress publication was
        # interrupted by the original preview failure.
        report = context.preflight.compare(workspace_id, reader=empty_target, actor=actor)
        self.assertEqual(report.create_count, 2)
        summary_url = f"/workspaces/{workspace_id}/summary"
        page = self.client.get(summary_url + "#comparison-recovery")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn('id="comparison-recovery"', page.text)

        owner = context.target_credential_workspace(
            workspace_id,
            actor=resolve_local_actor(context),
        )
        self.assertNotEqual(owner.workspace_id, workspace_id)
        context.secret_store.delete(target_read_credential_id(owner))
        recovery = self.client.get(summary_url)
        self.assertEqual(recovery.status_code, 200, recovery.text)
        self.assertIn('id="comparison-recovery"', recovery.text)
        self.assertIn('name="read_api_key"', recovery.text)
        identity = OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash,
            readable_models=tuple(model.name for model in schema.models),
            observed_at=datetime.now(UTC).isoformat(),
        )
        with (
            patch.object(context, "schema_reader", return_value=metadata_for_schema(schema)),
            patch.object(context, "read_identity_probe", return_value=identity),
            patch("impodo.web.routers.preflight._read_readiness_snapshots",
                  side_effect=lambda _context, _state, requirements, **kwargs: empty_target(requirements)),
        ):
            compared = self.client.post(summary_url + "/compare", data={
                "csrf_token": fixtures.field(recovery, "csrf_token"),
                "read_api_key": "fictional-reconnected-read",
                "read_api_key_storage": "session",
            }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(compared.status_code, 303, compared.text)
        final = self.client.get(compared.headers["location"])
        self.assertEqual(final.status_code, 200, final.text)
        self.assertNotIn('id="comparison-recovery"', final.text)
        self.assertNotIn("fictional-reconnected-read", final.text)
        self.assertEqual(context.run_planning.repository.get_application(
            application.application_id).status, RecipeApplicationStatus.COMPARED)
        rebound = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertEqual(rebound.content_hash, schema.content_hash)
        self.assertNotEqual(rebound.read_credential_binding_hash, schema.read_credential_binding_hash)
