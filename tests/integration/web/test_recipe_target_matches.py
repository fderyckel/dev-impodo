"""Exercise run-owned target review through real compilation and preparation."""

from datetime import UTC, datetime
from unittest import TestCase
from uuid import uuid4

from impodo.domain.mapping.contracts import CategoricalCoveragePolicy, ValueMapping
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaField, SchemaModel, SchemaOrigin
from tests.integration.web import test_fresh_data_controls as fixtures


class RecipeTargetMatchBrowserTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.FreshDataControlBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client, self.context = self.fixture.client, self.fixture.context
        self.app = self.fixture.fixture.app
        self.context.preparation_jobs = None

    def review_ready_delivery(self):
        """Create a fictional delivery with one valid and one unmatched language."""

        def configure_recipe(definition):
            definition["source_shape"]["datasets"][0]["columns"].append({
                "logical_column_id": "column:balances.language", "source_name": "Language",
                "candidate_type": "TEXT", "required_by": ["mapping"],
            })
            definition["mapping"]["datasets"][0]["fields"].append({
                "logical_field_id": "field:balances.lang", "target_field": "lang", "value_type": "string",
                "provider": {"kind": "SOURCE", "source_column_ids": ["column:balances.language"]},
                "categorical_policy": "EXACT_TARGET_VALUE",
            })
            definition["odoo_target_contract"]["models"][0]["fields"].append({
                "name": "lang", "field_type": "selection", "required": False, "readonly": False,
                "selection_values": ["en", "fr_FR"],
            })

        url = self.fixture.registered_delivery(
            configure_recipe=configure_recipe,
            source_csv=b"Name,Amount,Language\nAcme,100.00,en\nBeta,25.50,French\n",
        )
        page = self.client.get(url)
        accepted = self.client.post(url + "/accept", data={
            "csrf_token": fixtures.field(page, "csrf_token"), "parameter_revision": "",
            "warnings_acknowledged": "1", "control_0": "125.50",
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        setup, actor = self.fixture.setup, self.context.actor
        state = self.context.workspace_states.repository.get(setup.setup_workspace.workspace_id)
        self.context.workspace_states.update_target(
            state.workspace_id, actor=actor, expected_revision=state.revision, odoo_connection_mode="REMOTE",
            odoo_base_url="https://fictional.example.test", odoo_database="fictional_test",
            intended_applications=(), intended_models=("res.partner",),
        )
        schema = OdooSchemaCatalog(
            workspace_id=state.workspace_id, policy_hash=content_hash("schema policy"), captured_at=datetime.now(UTC),
            captured_by="Data manager", connection_mode="REMOTE", database="fictional_test", odoo_version="19.0",
            models=(SchemaModel(name="res.partner", label="Customers", fields=tuple(
                SchemaField(name=name, label=label, type=kind, required=False, readonly=False, relation=None,
                            relation_field=None, selection=choices)
                for name, label, kind, choices in (
                    ("name", "Name", "char", ()), ("credit_limit", "Opening balance", "float", ()),
                    ("lang", "Language", "selection", (("en", "English"), ("fr_FR", "French"))),
                )
            )),), content_hash=content_hash("schema evidence"), origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=content_hash("credential"), read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"), read_context_hash=content_hash("context"),
            connection_target_hash=content_hash("target"),
        )
        self.context.mapping_workspace.schemas.save_odoo_schema_catalog(state.workspace_id, schema, actor=actor)
        result = self.context.test_runs.activate(
            setup.binding.project_id, setup.run.migration_run_id,
            expected_workspace_revision=self.context.migration_projects.get(setup.binding.project_id, actor=actor).optimistic_revision,
            target_schema=schema, target_reference_bundle=None, credential_generation=schema.read_credential_binding_hash,
            operation_id=str(uuid4()), actor=actor,
        )
        self.application = result.applications[0]
        self.assertEqual(self.application.status, RecipeApplicationStatus.BLOCKED)
        issues = self.context.run_planning.repository.list_issues(self.application.application_id)
        self.assertIn("MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE", {item.code for item in issues}, issues)
        self.url = (f"/projects/{setup.binding.project_id}/runs/{setup.run.migration_run_id}"
                    f"/applications/{self.application.application_id}/target-matches")
        return self.url

    def form(self, page):
        return {name: fixtures.field(page, name) for name in (
            "csrf_token", "expected_definition_hash", "expected_working_draft_version", "expected_evidence_hash",
        )}

    def test_review_saves_only_current_choices_and_prepares_accepted_totals(self):
        url = self.review_ready_delivery()
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("1 value needs a match", page.text)
        self.assertNotIn("Open full field matcher", page.text)
        self.assertNotIn('name="control_expected_', page.text)
        review = self.context.recipe_target_matches.build_review(self.application, actor=self.context.actor)
        row = review.fields[0].review_rows[0]
        before = self.context.queries.get_mapping_working_draft(self.application.workspace_id)
        form = self.form(page)
        for payload in (
            {row.input_name: "not-an-odoo-choice"},
            {row.input_name: "fr_FR", "match_99_scalar_99_99": "fr_FR"},
        ):
            rejected = self.client.post(url, data={**form, **payload}, headers={"Origin": "http://testserver"}, follow_redirects=False)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertNotIn("Open full field matcher", rejected.text)
            current = self.context.queries.get_mapping_working_draft(self.application.workspace_id)
            self.assertEqual(current.version, before.version)
        stale = self.client.post(url, data={
            **form, row.input_name: "en", "expected_evidence_hash": content_hash("older Odoo evidence"),
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(stale.status_code, 422, stale.text)
        self.assertIn("Reload and review the current values", stale.text)
        self.assertIn('value="fr_FR" selected', stale.text)
        self.assertNotIn('value="en" selected', stale.text)
        self.assertEqual(self.context.queries.get_mapping_working_draft(self.application.workspace_id).version, before.version)
        accepted = self.client.post(url, data={**form, row.input_name: "fr_FR"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        self.assertNotIn("/mapping", accepted.headers["location"])
        application = self.context.run_planning.repository.get_application(self.application.application_id)
        self.assertEqual(application.status, RecipeApplicationStatus.READY)
        mapping = self.context.mapping_workspace.mappings.get_mapping_revision(application.workspace_id)
        language = next(item for item in mapping.definition.datasets[0].fields if item.target_field == "lang")
        self.assertEqual(language.categorical_policy, CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH)
        self.assertEqual(language.value_mappings, (ValueMapping("French", "fr_FR"), ValueMapping("en", "en")))
        self.assertEqual(mapping.definition.datasets[0].control_expectations[0].expected_total, "125.50")
        self.context.preparation.prepare(application.workspace_id, actor=self.context.actor)
        staging = self.context.preparation.staging.get_current_staging_summary(application.workspace_id)
        self.assertTrue(staging.control_totals[0].passed)
        self.assertEqual(staging.control_totals[0].expected_total, "125.50")
