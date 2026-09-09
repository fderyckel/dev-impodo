"""Exercise the authenticated Fresh data form with the real Recipe compiler."""

from dataclasses import replace
from datetime import UTC, datetime
import re
from unittest import TestCase
from uuid import uuid4

from impodo.domain.data_version.models import DataVersionState
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, target_identity_hash
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.recipe_lifecycle import metadata_for_schema
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaOrigin, SchemaModel, SchemaField
from tests.application.run import test_integrated_recipe_runs as fixtures


def field(page, name):
    return re.search(r'name="' + name + r'" value="([^"]*)"', page.text).group(1)


class FreshDataControlBrowserTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.IntegratedRecipeRunBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.context = self.fixture.app.state.context

    def registered_delivery(self, *, configure_recipe=None, source_csv=b"Name,Amount\nAcme,100.00\nBeta,25.50\n"):
        """Prepare isolated fictional data through the current browser routes."""

        page = self.client.get("/projects/new")
        created = self.client.post("/projects/new", data={
            "csrf_token": field(page, "csrf_token"), "creation_request_id": field(page, "creation_request_id"),
            "display_name": "Opening balances", "migration_purpose": "Verify this delivery's totals",
            "source_mode": "FILE", "source_system_identity": "Fictional ERP",
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(created.status_code, 303)
        project_id = created.headers["location"].rsplit("/", 1)[-1]
        context, actor = self.context, self.context.actor
        workspace = context.migration_workspaces.list_for_project(project_id, actor=actor)[0]
        data_version = context.data_versions.get(workspace.data_version_id, actor=actor)
        now = datetime.now(UTC)
        context.data_versions.repository.save_data_version(replace(
            data_version, state=DataVersionState.FROZEN, source_package_hash=content_hash("authoring delivery"),
            updated_at=now, frozen_at=now,
        ), expected_revision=data_version.optimistic_revision, event_type="TEST_DATA_VERSION_FROZEN", actor=actor)
        definition = fixtures.IntegratedRecipeCompiler._definition(logical_name="Balances", model="res.partner", fields=("name", "credit_limit"))
        definition["source_shape"]["datasets"][0]["columns"].append({
            "logical_column_id": "column:balances.amount", "source_name": "Amount",
            "candidate_type": "DECIMAL", "required_by": ["mapping"],
        })
        definition["parameter_definitions"] = {"parameters": []}
        dataset = definition["mapping"]["datasets"][0]
        dataset["mode"] = "CREATE"
        dataset["on_existing"] = "block"
        dataset["source_identity_column_ids"] = ["column:balances.name"]
        dataset["identity"] = [{
            "source_column_ids": ["column:balances.name"], "target_fields": ["name"], "value_type": "string",
        }]
        dataset["fields"] = [
            {"logical_field_id": f"field:balances.{target}", "target_field": target, "value_type": value_type, "provider": {
                "kind": "SOURCE", "source_column_ids": [column],
            }} for target, value_type, column in (
                ("credit_limit", "decimal", "column:balances.amount"),
            )
        ]
        definition["odoo_target_contract"]["models"][0]["fields"][1]["field_type"] = "float"
        definition["odoo_target_contract"]["business_keys"] = [{
            "model": "res.partner", "ordered_fields": ["name"], "scope_fields": [],
        }]
        definition["control_definitions"] = {"controls": [{
            "logical_control_id": "control:balances.amount", "name": "Opening balance total",
            "dataset_id": "dataset:balances", "target_field": "credit_limit", "unit": "EUR",
            "tolerance": "0.01", "calculation": "SUM", "invariant_expectation": False,
        }]}
        if configure_recipe is not None:
            configure_recipe(definition)
        publication = context.recipes.repository.publish_recipe(
            project_id=project_id, data_version_id=data_version.data_version_id, workspace_id=workspace.workspace_id,
            recipe_id=None, expected_recipe_revision=None, display_name="Customer balances",
            business_purpose="Check the opening balances before loading customers", compiled_recipe=definition,
            compatibility_hints={}, compilation_provenance={}, operation_id=str(uuid4()),
            request_hash=content_hash("balances Recipe"), actor=actor,
        )
        setup = context.test_runs.start_setup(project_id,
            expected_workspace_revision=context.migration_projects.get(project_id, actor=actor).optimistic_revision,
            recipe_revisions=((publication.recipe.recipe_id, publication.revision.version),), dependencies=(),
            label="September balances", export_as_of="2026-09-01", operation_id=str(uuid4()), actor=actor)
        self.setup, self.publication = setup, publication
        url = f"/projects/{project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        page = self.client.get(url)
        uploaded = self.client.post(url + "/files", data={
            "csrf_token": field(page, "csrf_token"), "revision": field(page, "revision"),
        }, files={"source_file": ("balances.csv", source_csv, "text/csv")},
            headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        page = self.client.get(url)
        registered = self.client.post(url + "/register", data={
            "csrf_token": field(page, "csrf_token"), "revision": field(page, "revision"),
        }, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(registered.status_code, 303, registered.text)
        return url

    def test_control_only_recipe_requires_totals_before_acceptance_and_compiles_them(self):
        url = self.registered_delivery()
        page = self.client.get(url)
        self.assertIn("Expected totals for this delivery", page.text)
        self.assertIn('name="control_0"', page.text)
        self.assertNotIn('name="parameter_0"', page.text)
        form = {"csrf_token": field(page, "csrf_token"), "parameter_revision": "", "warnings_acknowledged": "1"}
        missing = self.client.post(url + "/accept", data=form, headers={"Origin": "http://testserver"})
        self.assertEqual(missing.status_code, 422)
        self.assertIn("Enter Opening balance total", missing.text)
        self.assertEqual(self.context.data_versions.get(self.setup.data_version.data_version_id, actor=self.context.actor).state, DataVersionState.DRAFT)
        invalid = self.client.post(url + "/accept", data={**form, "control_0": "NaN"}, headers={"Origin": "http://testserver"})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("must be a finite number", invalid.text)
        accepted = self.client.post(url + "/accept", data={**form, "control_0": "125.50"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        page = self.client.get(url)
        self.assertIn("125.50 EUR", page.text)
        self.assertIn("Continue to Check Odoo", page.text)
        self.assertNotIn('name="control_0"', page.text)
        self._activate_with_real_compiler()

    def _activate_with_real_compiler(self, *, prepare=True):
        context, setup, actor = self.context, self.setup, self.context.actor
        state = context.workspace_states.repository.get(setup.setup_workspace.workspace_id)
        context.workspace_states.update_target(state.workspace_id, actor=actor, expected_revision=state.revision,
            odoo_connection_mode="REMOTE", odoo_base_url="https://fictional.example.test", odoo_database="fictional_test",
            intended_applications=(), intended_models=("res.partner",))
        schema = OdooSchemaCatalog(
            workspace_id=state.workspace_id, policy_hash=content_hash("schema policy"), captured_at=datetime.now(UTC),
            captured_by="Data manager", connection_mode="REMOTE", database="fictional_test", odoo_version="19.0",
            models=(SchemaModel(name="res.partner", label="Customers", fields=(
                SchemaField(name="name", label="Name", type="char", required=False, readonly=False, relation=None, relation_field=None, selection=()),
                SchemaField(name="credit_limit", label="Opening balance", type="float", required=False, readonly=False, relation=None, relation_field=None, selection=()),
            )),), content_hash=content_hash("schema evidence"), origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=content_hash("read credential"), read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"), read_context_hash=content_hash("context"),
            connection_target_hash=target_identity_hash(connection_mode="REMOTE", base_url="https://fictional.example.test", database="fictional_test"),
        )
        state = context.workspace_states.repository.get(state.workspace_id)
        credential = store_target_credential(context.secret_store, state, TargetCredentialRole.READ,
            "fictional-test-read", persistent=False)
        schema = context.schema_workspace.capture(state.workspace_id, metadata_for_schema(schema),
            read_credential_binding_hash=credential.binding_hash, read_identity=OdooReadIdentity(
                target_hash=schema.connection_target_hash, principal_hash=schema.read_principal_hash,
                permission_hash=schema.read_permission_hash, context_hash=schema.read_context_hash,
                readable_models=("res.partner",), observed_at=datetime.now(UTC).isoformat()), actor=actor)
        result = context.test_runs.activate(setup.binding.project_id, setup.run.migration_run_id,
            expected_workspace_revision=context.migration_projects.get(setup.binding.project_id, actor=actor).optimistic_revision,
            target_schema=schema, target_reference_bundle=None, credential_generation=schema.read_credential_binding_hash,
            operation_id=str(uuid4()), actor=actor)
        application = result.applications[0]
        self.assertEqual(application.status, RecipeApplicationStatus.READY, context.run_planning.repository.list_issues(application.application_id))
        mapping = context.mapping_workspace.mappings.get_mapping_revision(application.workspace_id)
        self.assertEqual(mapping.definition.datasets[0].control_expectations[0].expected_total, "125.50")
        page = self.client.get(f"/workspaces/{application.workspace_id}/mapping")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Accepted control totals", page.text)
        self.assertNotIn('name="control_expected_0_0"', page.text)
        if not prepare:
            return application
        context.preparation.prepare(application.workspace_id, actor=actor)
        staging = context.preparation.staging.get_current_staging_summary(application.workspace_id)
        self.assertEqual(len(staging.control_totals), 1)
        self.assertTrue(staging.control_totals[0].passed)
        self.assertEqual(staging.control_totals[0].expected_total, "125.50")
        return application
