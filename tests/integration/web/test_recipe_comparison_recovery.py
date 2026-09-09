"""Keep Recipe comparison pages within their authorized workspace."""

from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch

from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.shared.models import OdooReadIdentity
from impodo.web.target_credentials import target_read_credential_id
from tests.integration.web import test_fresh_data_controls as fixtures
from tests.support.recipe_lifecycle import metadata_for_schema, prepare_test_application


class RecipeComparisonRecoveryBrowserTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.FreshDataControlBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.context, self.client = self.fixture.context, self.fixture.client

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
        context, actor = self.context, self.context.actor
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

        owner = context.target_credential_workspace(workspace_id)
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
