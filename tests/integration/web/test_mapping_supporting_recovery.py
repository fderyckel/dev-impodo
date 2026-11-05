"""Recover stale related-data choices without replacing the mapping editor."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    DatasetMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    IdentityComponentMapping,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.presenters.supporting_review import mapping_supporting_review_view
from tests.integration.web import test_account_company_supporting as accounts
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.hierarchy_correction import MappingForm
from tests.support.identity import resolve_local_actor


def related_review_workspace(fixture, major=20, mode=OdooConnectionMode.REMOTE):
    """Build a saved review followed by refreshed access in a fictional workspace."""
    workspace_id, dataset, key, schema = (
        accounts.AccountCompanySupportingTests.account_workspace(
            fixture, f"{major}.0", mode
        )
    )
    context = fixture.app.state.context
    actor = resolve_local_actor(context)
    state = context.queries.get(workspace_id)
    state = context.workspace_states.select_odoo_company(
        workspace_id,
        actor=actor,
        expected_revision=state.revision,
        selection=OdooCompanySelection(
            schema.connection_target_hash, 105, "Example Wiltz"
        ),
    )
    _, form = accounts.AccountCompanySupportingTests.review_form(fixture, workspace_id)
    form["action_1"] = "SKIP_OPTIONAL"
    saved = fixture.client.post(
        f"/workspaces/{workspace_id}/schema/supporting/save",
        data=form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    fixture.assertEqual(saved.status_code, 303, saved.text)
    run_local_operation_sync(
        context.schema_workspace.govern_complete,
        workspace_id,
        business_keys=(key,),
        expected_catalog_hash=schema.content_hash,
        actor=actor,
    )
    company = RelationshipMapping(
        "company_ids",
        "many2many",
        (),
        RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.company"),
        value_source=RelationshipValueSource.CONSTANT_EXISTING,
        constant_reference=ConstantBusinessReference(
            key_values=(ConstantReferenceComponent("name", "Example"),)
        ),
        required=True,
    )
    mapping = DatasetMapping(
        dataset.dataset_id,
        "account.account",
        source_identity_column_keys=(dataset.columns[0].stable_key,),
        target_identity=(
            IdentityComponentMapping((dataset.columns[0].stable_key,), ("ref",)),
        ),
        relationships=(company,),
        fields=(
            ScalarFieldMapping(
                "field_0000",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="Saved account name",
            ),
        ),
    )
    run_local_operation_sync(
        context.mapping_workspace.save_working_draft,
        workspace_id,
        datasets=(mapping,),
        expected_version=None,
        actor=actor,
    )
    refreshed = replace(schema, read_context_hash=state.odoo_company.context_hash)
    context.schema_workspace.schemas.save_odoo_create_defaults(
        workspace_id,
        refreshed,
        expected_content_hash=schema.content_hash,
        expected_company_context_hash=state.odoo_company.context_hash,
        expected_read_credential_binding_hash=schema.read_credential_binding_hash,
        actor=actor,
    )
    context.readiness_reader = lambda workspace, _metadata, _records: (
        accounts.AccountCompanySupportingTests.snapshots(fixture, workspace, refreshed)
    )
    context.read_identity_probe = lambda _workspace, _secret, models: OdooReadIdentity(
        target_hash=refreshed.connection_target_hash,
        principal_hash=refreshed.read_principal_hash,
        permission_hash=refreshed.read_permission_hash,
        context_hash=refreshed.read_context_hash,
        readable_models=tuple(models),
        observed_at="2026-11-02T12:00:00Z",
    )
    return workspace_id, dataset, key, refreshed


class MappingSupportingRecoveryTests(ProjectSetupBrowserTestCase):
    def test_company_explanation_requires_the_exact_previous_review(self):
        workspace_id, _, _, schema = related_review_workspace(self)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        packet = context.schema_workspace.supporting_review_inputs(
            workspace_id, actor=actor
        )
        request = SimpleNamespace(
            query_params={},
            session={
                "supporting_review_refresh": {
                    "workspace_id": workspace_id,
                    "schema_hash": schema.content_hash,
                    "context_hash": schema.read_context_hash,
                    "credential_hash": schema.read_credential_binding_hash,
                    "previous_context_hash": "sha256:" + "3" * 64,
                    "previous_credential_hash": schema.read_credential_binding_hash,
                }
            },
        )

        def view(selection):
            return mapping_supporting_review_view(
                request,
                context,
                packet.workspace,
                selection,
                schema,
                packet.governance,
                actor=actor,
                preparation=packet.preparation,
            )

        self.assertIn(
            "Odoo access changed for Example Wiltz", view(packet.selection)["message"]
        )
        changed = replace(packet.selection, content_hash="sha256:" + "a" * 64)
        self.assertNotIn("Odoo access changed for", view(changed)["message"])
        self.assertIn("source or Odoo details changed", view(changed)["message"])

    def test_stale_review_recovers_locally_for_both_majors_and_connection_modes(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, dataset, key, _ = related_review_workspace(
                        self, major, mode
                    )
                    before = context.queries.get_mapping_working_draft(workspace_id)
                    calls = tuple(self.readiness_calls)
                    with collect_duckdb_request_timings() as timings:
                        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertEqual(
                        page.context["related_data_review"]["problem"], "STALE"
                    )
                    self.assertIn("Review related-data choices", page.text)
                    self.assertIn(
                        "Keep these choices if they are still correct", page.text
                    )
                    self.assertNotIn("Needs confirmation", page.text)
                    self.assertNotIn(
                        "Your current field mappings stay on this page", page.text
                    )
                    self.assertLess(
                        page.text.index("Confirm choices and check matches"),
                        page.text.index("data-related-review-details"),
                    )
                    # The rich page already opens eight owners across access and page scopes.
                    self.assertLessEqual(timings.connection_count, 8)
                    self.assertLessEqual(timings.schema_check_count, 6)
                    with collect_duckdb_request_timings() as timings:
                        fragment = self.client.get(
                            f"/workspaces/{workspace_id}/mapping/supporting-review"
                        )
                    self.assertEqual(fragment.status_code, 200, fragment.text)
                    self.assertEqual(fragment.headers["cache-control"], "no-store")
                    self.assertNotIn("data-mapping-form", fragment.text)
                    self.assertLessEqual(timings.connection_count, 6)
                    self.assertLessEqual(timings.schema_check_count, 4)
                    self.assertEqual(timings.lock_retry_count, 0)
                    draft = context.queries.get_mapping_working_draft(workspace_id)
                    operation = str(uuid4())
                    entries = [
                        ["csrf_token", self.csrf],
                        ["operation_id", operation],
                        ["action", "draft"],
                        ["expected_parent_version", ""],
                        ["expected_working_draft_version", str(draft.version)],
                        ["editable_dataset_id", dataset.dataset_id],
                        ["target_model_0", "account.account"],
                        ["mode_0", "upsert"],
                        ["on_existing_0", "block"],
                        ["source_identity_0", dataset.columns[0].stable_key],
                        ["business_key_0", key.key_id],
                        ["identity_source_0_0", dataset.columns[0].stable_key],
                    ]
                    rejected = self.client.post(
                        f"/workspaces/{workspace_id}/mapping/save",
                        json={"entries": entries},
                        headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                    )
                    self.assertEqual(rejected.status_code, 422, rejected.text)
                    self.assertEqual(
                        rejected.json()["failure_code"], "SUPPORTING_REVIEW_REQUIRED"
                    )
                    receipt = self.client.get(
                        f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation}"
                    )
                    self.assertEqual(
                        receipt.json()["failure_code"], "SUPPORTING_REVIEW_REQUIRED"
                    )
                    form = dict(
                        MappingForm(
                            fragment.text, form_attribute="data-related-review-form"
                        ).entries
                    )
                    headers = {**POST_HEADERS, "Accept": "application/json"}
                    with collect_duckdb_request_timings() as timings:
                        saved = self.client.post(
                            f"/workspaces/{workspace_id}/schema/supporting/save",
                            data=form,
                            headers=headers,
                        )
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertLessEqual(timings.connection_count, 6)
                    self.assertLessEqual(timings.schema_check_count, 4)
                    retried = self.client.post(
                        f"/workspaces/{workspace_id}/schema/supporting/save",
                        data=form,
                        headers=headers,
                    )
                    self.assertEqual(retried.json(), saved.json())
                    self.assertEqual(
                        context.queries.get_mapping_working_draft(workspace_id), before
                    )
                    self.assertEqual(tuple(self.readiness_calls), calls)
                    packet = context.schema_workspace.supporting_review_inputs(
                        workspace_id, actor=actor
                    )
                    self.assertIsNotNone(
                        context.schema_workspace.supporting.require_current(
                            packet, actor=actor
                        )
                    )
                    checked_form = MappingForm(page.text)
                    checked = self.client.post(
                        checked_form.action,
                        json={"entries": [*checked_form.entries, ["action", "draft"]]},
                        headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                    )
                    self.assertEqual(checked.status_code, 200, checked.text)
                    reopened = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertTrue(reopened.context["related_data_review"]["current"])

    def test_changed_evidence_rejects_inline_save_without_losing_the_mapping(self):
        workspace_id, _, _, schema = related_review_workspace(self)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        before = context.queries.get_mapping_working_draft(workspace_id)
        review = context.schema_workspace.supporting.current(workspace_id, actor=actor)
        _, form = accounts.AccountCompanySupportingTests.review_form(self, workspace_id)
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id,
            replace(schema, read_context_hash="sha256:" + "f" * 64),
            actor=actor,
        )
        response = self.client.post(
            f"/workspaces/{workspace_id}/schema/supporting/save",
            data=form,
            headers={**POST_HEADERS, "Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("changed", response.json()["detail"])
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id), before
        )
        self.assertEqual(
            context.schema_workspace.supporting.current(workspace_id, actor=actor),
            review,
        )
