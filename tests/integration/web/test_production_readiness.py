"""Exercise Production compilation and restart recovery with fictional Odoo evidence."""

from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.domain.run.contracts import MigrationRunPlanningError
from impodo.domain.run.production import ProductionRunError
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooWriteIdentity
from impodo.web.app import create_local_app
from impodo.web.run_commands import _assert_recipe_application_can_prepare, _preparation_workspace
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.recipe_lifecycle import complete_application, metadata_for_schema
from impodo.domain.shared.models import OdooReadIdentity, target_identity_hash
from tests.integration.web import test_fresh_data_controls as fresh_data

field = fresh_data.field


class ProductionReadinessBrowserTests(TestCase):
    def setUp(self):
        self.fixture = fresh_data.FreshDataControlBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.context, self.client = self.fixture.context, self.fixture.client
        self.headers = {"Origin": "http://testserver"}

    def ready_production(self, *, before_accept=None):
        """Qualify actual Test outcomes, then guide a separate Production delivery."""

        url = self.fixture.registered_delivery()
        page = self.client.get(url)
        accepted = self.client.post(url + "/accept", data={
            "csrf_token": field(page, "csrf_token"), "parameter_revision": "",
            "warnings_acknowledged": "1", "control_0": "125.50",
        }, headers=self.headers, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        test_application = self.fixture._activate_with_real_compiler()
        context, actor = self.context, self.context.actor
        project_id, test_run_id = test_application.project_id, test_application.migration_run_id
        complete_application(self, context, test_application, expected_total="125.50")
        review = context.cutover_plans.review(project_id, test_run_id, actor=actor)
        self.assertTrue(review.can_qualify, review.issues)
        qualification = context.cutover_plans.qualify(project_id, test_run_id,
            expected_workspace_revision=context.migration_projects.get(project_id, actor=actor).optimistic_revision,
            expected_evidence_hash=review.integrated_evidence_hash, operation_id=str(uuid4()), actor=actor)
        context.cutover_plans.select(project_id, qualification.qualification_id,
            expected_workspace_revision=context.migration_projects.get(project_id, actor=actor).optimistic_revision,
            operation_id=str(uuid4()), actor=actor)
        new_url = f"/projects/{project_id}/production-runs/new"
        page = self.client.get(new_url)
        created = self.client.post(new_url, data={
            **{name: field(page, name) for name in ("csrf_token", "cutover_selection_id", "expected_workspace_revision", "operation_id")},
            "label": "September Production balances", "export_as_of": "2026-09-09",
        }, headers=self.headers, follow_redirects=False)
        self.assertEqual(created.status_code, 303, created.text)
        self.fresh_url = created.headers["location"]
        self.run_id = self.fresh_url.split("/")[4]
        self.binding = context.production_runs.production_runs.get(self.run_id)
        workspace_id = self.binding.setup_workspace_id
        page = self.client.get(self.fresh_url)
        self.assertIn("this Production run needs", page.text)
        uploaded = self.client.post(self.fresh_url + "/files", data={
            "csrf_token": field(page, "csrf_token"), "revision": field(page, "revision"),
        }, files={"source_file": ("production-balances.csv", b"Name,Amount\nAcme,150.00\nBeta,50.00\n", "text/csv")},
            headers=self.headers, follow_redirects=False)
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        page = self.client.get(self.fresh_url)
        registered = self.client.post(self.fresh_url + "/register", data={
            "csrf_token": field(page, "csrf_token"), "revision": field(page, "revision"),
        }, headers=self.headers, follow_redirects=False)
        self.assertEqual(registered.status_code, 303, registered.text)
        if before_accept:
            before_accept()
        page = self.client.get(self.fresh_url)
        self.assertIn("Recipe table matches", page.text)
        accepted = self.client.post(self.fresh_url + "/accept", data={
            "csrf_token": field(page, "csrf_token"), "parameter_revision": field(page, "parameter_revision"),
            "warnings_acknowledged": "1", "control_0": "200.00",
        }, headers=self.headers, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        saved = context.production_runs.production_runs.get_run_values(self.run_id)
        self.assertEqual(saved.controls[test_application.recipe_id], {"control:balances.amount": "200.00"})
        state = context.workspace_states.repository.get(workspace_id)
        context.workspace_states.update_target(workspace_id, actor=actor, expected_revision=state.revision,
            odoo_connection_mode="REMOTE", odoo_base_url="https://production.example.test", odoo_database="fictional_production",
            intended_models=("res.partner",), intended_applications=())
        state = context.workspace_states.repository.get(workspace_id)
        read = store_target_credential(context.secret_store, state, TargetCredentialRole.READ,
            "fictional-production-read", persistent=False)
        test_schema = context.run_planning.repository.get_run_target_schema(test_run_id).source_schema
        self.schema = replace(test_schema, workspace_id=workspace_id, database="fictional_production",
            connection_target_hash=target_identity_hash(connection_mode="REMOTE", base_url="https://production.example.test", database="fictional_production"), read_credential_binding_hash=read.binding_hash,
            read_principal_hash=content_hash("production reader"), read_permission_hash=content_hash("production read permissions"),
            read_context_hash=content_hash("production context"), content_hash=content_hash("production schema"))
        self.identity = OdooWriteIdentity(target_hash=self.schema.connection_target_hash,
            principal_hash=content_hash("production writer"), permission_hash=content_hash("production write permissions"),
            context_hash=self.schema.read_context_hash, readable_models=("res.partner",), writable_models=("res.partner",),
            observed_at="2026-09-09T08:00:00+00:00")
        self.binding = context.production_runs.production_runs.for_workspace(workspace_id)
        self.project_id, self.run_id = project_id, self.binding.migration_run_id
        self.url = f"/projects/{project_id}/production-runs/{self.run_id}/activate"
        self.run_url = f"/projects/{project_id}/runs/{self.run_id}"
        page = self.client.get(self.run_url + "/odoo")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Review the Odoo requirements for this Production run", page.text)
        read_identity = OdooReadIdentity(target_hash=self.schema.connection_target_hash,
            principal_hash=self.schema.read_principal_hash, permission_hash=self.schema.read_permission_hash,
            context_hash=self.schema.read_context_hash, readable_models=("res.partner",), observed_at=self.identity.observed_at)
        with patch.object(context, "read_identity_probe", return_value=read_identity), \
                patch.object(context, "schema_reader", return_value=metadata_for_schema(self.schema)):
            checked = self.client.post(f"/projects/{project_id}/production-runs/{self.run_id}/odoo/check", data={
                name: field(page, name) for name in ("csrf_token", "expected_workspace_revision", "operation_id")
            }, headers=self.headers, follow_redirects=False)
        self.assertEqual(checked.status_code, 303, checked.text)
        self.assertEqual(checked.headers["location"], self.url)
        self.assertEqual(context.production_runs.production_runs.get(self.run_id).state.value, "SETUP")
        self.assertIsNone(context.production_runs.activation_operation(self.run_id, actor=actor))
        self.schema, _ = context.run_planning.target_evidence_from_workspace(project_id, workspace_id, actor=actor)
        return test_application

    def _check_saved_answers(self):
        page = self.client.get(self.fresh_url)
        form = {"csrf_token": field(page, "csrf_token"), "parameter_revision": "", "intent": "save"}
        for value, message in (("", "Opening balance total"), ("NaN", "finite number")):
            response = self.client.post(self.fresh_url + "/accept", data={**form, "control_0": value}, headers=self.headers)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn(message, response.text)
        saved = self.client.post(self.fresh_url + "/accept", data={**form, "control_0": "200.00"},
                                 headers=self.headers, follow_redirects=False)
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(self.context.data_versions.get(self.binding.data_version_id, actor=self.context.actor).state.value, "DRAFT")
        page = self.client.get(self.fresh_url)
        self.assertIn('value="200.00"', page.text)
        self.assertEqual(field(page, "parameter_revision"), "1")
        stale = self.client.post(self.fresh_url + "/accept", data={**form, "control_0": "999"}, headers=self.headers)
        self.assertEqual(stale.status_code, 422, stale.text)
        self.assertIn("Run values changed", stale.text)
        wrong_kind = self.client.get(self.fresh_url.replace("production-runs", "test-runs"))
        self.assertEqual(wrong_kind.status_code, 404)

    def activation_form(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Opening balance total", page.text)
        self.assertNotIn('name="control_0"', page.text)
        self.assertIn('200.00', page.text)
        self.assertNotIn("You are viewing Review and load", page.text)
        return page, {
            **{name: field(page, name) for name in ("csrf_token", "expected_workspace_revision", "operation_id", "values_evidence_hash")},
            "write_api_key": "fictional-production-write",
        }

    def test_invalid_values_then_interrupted_compilation_resume_after_restart(self):
        test_application = self.ready_production(before_accept=self._check_saved_answers)
        context = self.context
        saved = context.production_runs.production_runs.get_run_values(self.run_id)
        with self.assertRaisesRegex(MigrationFoundationError, "accepted with this fresh data"):
            context.run_setups.replace_values(self.binding, {},
                supplied_controls={test_application.recipe_id: {"control:balances.amount": "999"}},
                expected_revision=saved.revision, actor=context.actor)
        with patch.object(context.run_planning, "target_evidence_from_workspace", return_value=(self.schema, None)), \
                patch.object(context, "write_identity_probe", return_value=self.identity) as probe:
            _, form = self.activation_form()
            for change, message in (({"values_evidence_hash": "old"}, "Production setup changed"),
                                    ({"expected_workspace_revision": "0"}, "The Project changed")):
                with self.subTest(change=change):
                    response = self.client.post(self.url, data={**form, **change}, headers=self.headers)
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn(message, response.text)
                    self.assertNotIn('value="fictional-production-write"', response.text)
                    probe.assert_not_called()
            retained = self.client.post(self.url, data={**form, "write_api_key": "fictional-production-read"}, headers=self.headers)
            self.assertEqual(retained.status_code, 422)
            self.assertIn('200.00', retained.text)
            probe.assert_not_called()
            with patch.object(context.run_planning.repository, "commit_provisioning", side_effect=MigrationRunPlanningError("Interrupted after compiling work areas")):
                interrupted = self.client.post(self.url, data=form, headers=self.headers)
            self.assertEqual(interrupted.status_code, 422, interrupted.text)
            self.assertIn("Finish Production setup", interrupted.text)
            self.assertNotIn("Production Recipe work areas were created", interrupted.text)
            probe.assert_called_once()
            operation = context.production_runs.activation_operation(self.run_id, actor=context.actor)
            self.assertNotEqual(operation.state.value, "COMMITTED")
            self.assertEqual(operation.detail["activation_inputs"]["controls"][test_application.recipe_id], {"control:balances.amount": "200.00"})
            self.assertNotIn("fictional-production-write", str(operation.detail))
            bundle = context.run_planning.repository.get_bundle(self.run_id)
            application = bundle.applications[0]
            mapping = context.mapping_workspace.mappings.get_mapping_revision(application.workspace_id)
            self.assertEqual(mapping.definition.datasets[0].control_expectations[0].expected_total, "200.00")
            redirect = self.client.get(self.run_url, follow_redirects=False)
            self.assertEqual(redirect.status_code, 303)
            self.assertEqual(redirect.headers["location"], self.url)
            overview = self.client.get(f"/projects/{self.project_id}")
            self.assertIn("Setup needs attention", overview.text)
            self.assertIn("Continue Production setup", overview.text)
            with self.assertRaisesRegex(WorkspaceError, "Finish Production setup"):
                _assert_recipe_application_can_prepare(context, _preparation_workspace(context, application.workspace_id))
            with self.assertRaisesRegex(ProductionRunError, "Finish Production setup"):
                context.production_runs.assert_execution_authority(application.workspace_id,
                    read_identity=None, read_credential_generation="", expected_read_credential_generation="",
                    write_identity=None, write_credential_generation="", actor=context.actor)

        # Persistent artifact encryption keys survive restart; session Odoo keys do not.
        restarted_secrets = MemorySecretStore()
        restarted_secrets.values.update({key: value for key, value in context.secret_store.values.items()
                                        if ":protected:" in key})
        app = create_local_app(self.fixture.fixture.root, secret_store=restarted_secrets,
            launch_token="resume-launch", session_secret="resume-session",
            preparation_jobs_enabled=False, odoo_capture_jobs_enabled=False)
        resumed_context = app.state.context
        with TestClient(app) as client, \
                patch.object(resumed_context, "write_identity_probe", side_effect=AssertionError("Resume must not probe Odoo")), \
                patch.object(resumed_context.run_planning.compiler, "materialize", side_effect=AssertionError("Completed work must be reused")):
            client.get("/launch?token=resume-launch")
            self.assertEqual(resumed_context.production_runs.production_runs.get_run_values(self.run_id), saved)
            pending = client.get(self.url)
            self.assertEqual(pending.status_code, 200)
            self.assertIn("Finish Production setup", pending.text)
            self.assertNotIn('name="write_api_key"', pending.text)
            resumed = client.post(self.url + "/resume", data={"csrf_token": field(pending, "csrf_token")},
                headers=self.headers, follow_redirects=False)
            self.assertEqual(resumed.status_code, 303, resumed.text)
            self.assertEqual(resumed.headers["location"], self.run_url)
            completed = resumed_context.production_runs.activation_operation(self.run_id, actor=resumed_context.actor)
            self.assertEqual(completed.operation_id, operation.operation_id)
            self.assertEqual(completed.state.value, "COMMITTED")
            current = resumed_context.run_planning.repository.get_bundle(self.run_id)
            self.assertEqual(current.applications, bundle.applications)
            run_page = client.get(self.run_url)
            self.assertEqual(run_page.status_code, 200, run_page.text)
            self.assertIn("Production run", run_page.text)
            self.assertIn(f'href="{self.run_url}/odoo"', run_page.text)
            overview = client.get(f"/projects/{self.project_id}")
            self.assertIn("Setup complete", overview.text)
            self.assertIn("Continue review and load", overview.text)
            ready = client.get(self.url)
            self.assertIn("Production Recipe work areas were created", ready.text)
            replay = client.post(self.url + "/resume", data={"csrf_token": field(ready, "csrf_token")},
                headers=self.headers, follow_redirects=False)
            self.assertEqual(replay.status_code, 303)
            resumed_context.preparation.prepare(application.workspace_id, actor=resumed_context.actor)
            staging = resumed_context.preparation.staging.get_current_staging_summary(application.workspace_id)
            self.assertTrue(staging.control_totals[0].passed)
            self.assertEqual(staging.control_totals[0].expected_total, "200.00")
            self.assertNotEqual(current.run.data_version_id, self.fixture.setup.data_version.data_version_id)
            execution, reconciliation = complete_application(self, resumed_context, application, expected_total="200.00",
                write_identity=self.identity)
            self.assertEqual(resumed_context.run_planning.repository.get_application(application.application_id).status.value, "RECONCILED")
            test_execution = context.execution.journal.get_current_run(test_application.workspace_id)
            self.assertNotEqual(test_execution.run_id, execution.run_id)
            self.assertNotEqual(test_execution.target_hash, execution.target_hash)
