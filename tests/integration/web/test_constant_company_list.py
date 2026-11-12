"""Choose one Odoo company for a list field without reading source values."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.mapping.contracts import (
    MAPPING_CONTRACT_VERSION,
    RelationshipValueSource,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.presenters.mapping_forms import _related_business_keys
from impodo.web.presenters.mapping_view import _mapping_dataset_views
from impodo.web.presenters.user_help import DECISION_HELP_BY_ID
from impodo.web.target_credentials import local_read_credential_binding_hash
from tests.integration.web import test_account_company_supporting as fixtures
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor
from tests.support.recipe_lifecycle import metadata_for_schema


class ConstantCompanyListTests(ProjectSetupBrowserTestCase):
    def test_bom_company_choices_recheck_the_selected_company_before_lookup(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        field = SchemaField(
            "company_id", "Company", "many2one", False, False, "res.company", None, (),
        )
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, dataset, key = self._mapping_ready_workspace(
                        scalar_field_count=1, target_model="mrp.bom",
                        additional_fields=(field,), connection_mode=mode, odoo_major=major,
                    )
                    workspace = context.queries.get(workspace_id)
                    original = context.queries.get_odoo_schema_catalog(workspace_id)
                    metadata = metadata_for_schema(original)
                    identity = OdooReadIdentity(
                        original.connection_target_hash, original.read_principal_hash,
                        original.read_permission_hash, original.read_context_hash,
                        ("mrp.bom",), "2026-10-31T00:00:00Z",
                    )
                    remote = mode is OdooConnectionMode.REMOTE
                    binding = (
                        original.read_credential_binding_hash if remote
                        else local_read_credential_binding_hash(workspace)
                    )
                    schema = context.schema_workspace.capture(
                        workspace_id, metadata, read_credential_binding_hash=binding,
                        read_identity=identity if remote else None, actor=actor,
                    )
                    context.schema_workspace.govern(
                        workspace_id, business_keys=(key,), actor=actor,
                    )
                    selected = context.workspace_states.select_odoo_company(
                        workspace_id, actor=actor, expected_revision=context.queries.get(workspace_id).revision,
                        selection=OdooCompanySelection(schema.connection_target_hash, 105, "Example"),
                    )
                    company_hash = selected.odoo_company.context_hash
                    self.assertNotEqual(schema.read_context_hash, company_hash)
                    events = []
                    expected_company = SimpleNamespace(context_hash=company_hash)

                    def read_schema(
                        state, _credential_or_profile, *_models,
                        expected_company=expected_company, events=events, metadata=metadata,
                    ):
                        self.assertFalse(getattr(_read_databases, "connections", None))
                        self.assertEqual(state.odoo_company.context_hash, expected_company.context_hash)
                        events.append("fields")
                        return metadata

                    def read_company(
                        state, metadata_requests, record_requests, *,
                        expected_company=expected_company, events=events, schema=schema,
                    ):
                        self.assertFalse(getattr(_read_databases, "connections", None))
                        self.assertEqual(state.odoo_company.context_hash, expected_company.context_hash)
                        self.assertEqual(metadata_requests[0].model, "res.company")
                        self.assertEqual(record_requests[0].domain, (("name", "=", "Example"),))
                        events.append("choices")
                        return fixtures.AccountCompanySupportingTests.snapshots(self, state, schema)

                    company_key = _related_business_keys((), "res.company")[0]
                    payload = {
                        "csrf_token": self.csrf, "kind": "constant_relationship",
                        "dataset_id": dataset.dataset_id, "source_column_key": "",
                        "target_model": "mrp.bom", "target_field": "company_id",
                        "business_key_id": company_key.key_id, "constant_value": "Example",
                    }
                    url = f"/workspaces/{workspace_id}/mapping/value-choices"
                    with (
                        patch.object(context, "schema_read_session_factory", None),
                        patch.object(context, "schema_reader", side_effect=read_schema),
                        patch.object(
                            context, "read_identity_probe",
                            return_value=replace(identity, context_hash=company_hash),
                        ) as probe,
                        patch.object(context.local_odoo_reader, "get_model_metadata", side_effect=read_schema),
                        patch("impodo.web.routers.matching_company_refresh._selected_local_profile",
                              return_value=None if remote else object()),
                        patch.object(context, "readiness_reader", side_effect=read_company),
                    ):
                        with collect_duckdb_request_timings() as timings:
                            response = self.client.post(url, data=payload, headers=POST_HEADERS)
                        self.assertEqual(response.status_code, 200, response.text)
                        checked = context.queries.get_odoo_schema_catalog(workspace_id)
                        self.assertEqual(checked.read_context_hash, company_hash)
                        self.assertEqual(checked.content_hash, schema.content_hash)
                        self.assertLessEqual(timings.connection_count, 16)
                        self.assertEqual(timings.lock_retry_count, 0)
                        self.assertEqual(events, ["fields", "choices"])
                        if remote:
                            next_company = context.workspace_states.select_odoo_company(
                                workspace_id, actor=actor, expected_revision=selected.revision,
                                selection=OdooCompanySelection(schema.connection_target_hash, 106, "Other"),
                            )
                            company_hash = next_company.odoo_company.context_hash
                            expected_company.context_hash = company_hash
                            probe.return_value = replace(
                                identity, context_hash=company_hash,
                                permission_hash="sha256:" + "8" * 64,
                            )
                            review = self.client.post(url, data=payload, headers=POST_HEADERS)
                            self.assertEqual(review.status_code, 422, review.text)
                            self.assertTrue(review.json()["review_odoo_details"])
                            self.assertEqual(events, ["fields", "choices", "fields"])
                            pending = context.queries.get_odoo_schema_catalog(workspace_id)
                            self.assertIsNotNone(pending.pending_refresh)
                            self.assertEqual(pending.content_hash, schema.content_hash)
                    self.assertEqual(
                        response.json()["target_choices"], [{"value": "Example", "label": "Example"}],
                    )

    def test_choose_save_and_reopen_single_company_without_source_column(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, dataset, key, schema = (
                        fixtures.AccountCompanySupportingTests.account_workspace(
                            self, f"{major}.0", mode
                        )
                    )
                    page, form = fixtures.AccountCompanySupportingTests.review_form(
                        self, workspace_id
                    )
                    saved = self.client.post(
                        f"/workspaces/{workspace_id}/schema/supporting/save",
                        data=form,
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                    self.assertEqual(saved.status_code, 303, saved.text)
                    run_local_operation_sync(
                        context.schema_workspace.govern_complete,
                        workspace_id,
                        business_keys=(key,),
                        expected_catalog_hash=schema.content_hash,
                        actor=actor,
                    )
                    workspace = context.queries.get(workspace_id)
                    page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertIn(
                        "Use one existing Odoo record as the complete list for every row",
                        page.text,
                    )
                    company_key = next(
                        k
                        for k in _related_business_keys((), "res.company")
                        if k.key_fields == ("name",)
                    )
                    payload = {
                        "csrf_token": self.csrf,
                        "kind": "constant_relationship",
                        "dataset_id": dataset.dataset_id,
                        "source_column_key": "",
                        "target_model": "account.account",
                        "target_field": "company_ids",
                        "business_key_id": company_key.key_id,
                    }
                    with (
                        patch(
                            "impodo.web.routers.mapping._source_value_choices",
                            side_effect=AssertionError(
                                "Constant chooser must not scan source rows"
                            ),
                        ),
                        patch.object(
                            context,
                            "readiness_reader",
                            return_value=fixtures.AccountCompanySupportingTests.snapshots(
                                self, workspace, schema
                            ),
                        ) as reader,
                        collect_duckdb_request_timings() as timings,
                    ):
                        choices = self.client.post(
                            f"/workspaces/{workspace_id}/mapping/value-choices",
                            data=payload,
                            headers=POST_HEADERS,
                        )
                        self.assertEqual(choices.status_code, 200, choices.text)
                        reader.assert_called_once()
                        reused = self.client.post(
                            f"/workspaces/{workspace_id}/mapping/value-choices",
                            data=payload,
                            headers=POST_HEADERS,
                        )
                        reader.assert_called_once()
                    self.assertEqual(reused.status_code, 200, reused.text)
                    self.assertEqual(
                        choices.json()["target_choices"],
                        [{"value": "Example", "label": "Example"}],
                    )
                    self.assertTrue(reused.json()["target_choices_reused"])
                    # Each request reads four lineage owners; cache lookup and
                    # capture use bounded additional operations, never row loops.
                    self.assertLessEqual(timings.connection_count, 20)
                    self.assertEqual(timings.lock_retry_count, 0)
                    entries = [
                        ["csrf_token", self.csrf],
                        ["action", "save_progress"],
                        ["expected_parent_version", ""],
                        ["expected_working_draft_version", ""],
                        ["editable_dataset_id", dataset.dataset_id],
                        ["target_model_0", "account.account"],
                        ["mode_0", "upsert"],
                        ["on_existing_0", "block"],
                        ["source_identity_0", dataset.columns[0].stable_key],
                        ["business_key_0", key.key_id],
                        ["identity_source_0_0", dataset.columns[0].stable_key],
                        ["visible_relation_target_0", "company_ids"],
                        ["relation_value_source_0_0", "constant_existing"],
                        ["relation_constant_key_0_0", company_key.key_id],
                        ["relation_constant_component_0_0_0", "Example"],
                        ["relation_compare_0_0", "1"],
                        ["relation_operation_0_0", "replace"],
                        ["relation_missing_0_0", "error"],
                        ["relation_ambiguous_0_0", "error"],
                        ["relation_null_0_0", "distinct"],
                    ]
                    response = self.client.post(
                        f"/workspaces/{workspace_id}/mapping/save",
                        json={"entries": entries},
                        headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    draft = context.queries.get_mapping_working_draft(workspace_id)
                    relation = draft.definition.datasets[0].relationships[0]
                    self.assertEqual(draft.definition.contract_version, MAPPING_CONTRACT_VERSION)
                    self.assertEqual(relation.kind, "many2many")
                    self.assertEqual(relation.source_column_keys, ())
                    self.assertIs(
                        relation.value_source, RelationshipValueSource.CONSTANT_EXISTING
                    )
                    self.assertEqual(
                        relation.constant_reference.key_values[0].value, "Example"
                    )
                    reopened = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertIn('value="constant_existing" selected', reopened.text)
                    self.assertIn('value="Example"', reopened.text)

    def test_parent_identity_is_explained_and_code_identity_leaves_parent_card_available(
        self,
    ):
        context = self.app.state.context
        workspace_id, _dataset, _key, schema = (
            fixtures.AccountCompanySupportingTests.account_workspace(
                self, "20.0", OdooConnectionMode.LOCAL
            )
        )
        parent = SchemaField(
            "parent_id",
            "Parent Account",
            "many2one",
            False,
            False,
            "account.account",
            None,
            (),
        )
        name = SchemaField("name", "Account Name", "char", True, False, None, None, ())
        schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0], fields=(*schema.models[0].fields, name, parent)
                ),
            ),
        )
        selection = context.queries.get_source_selection(workspace_id)
        template = self.app.state.templates.env.get_template(
            "mapping/_dataset.html"
        )
        for fields, scope, shows_parent in (
            (("name",), ("parent_id",), False),
            (("ref",), (), True),
        ):
            governance = SimpleNamespace(
                business_keys=(
                    BusinessKeyDefinition(
                        "key:accounts",
                        "account.account",
                        fields,
                        scope_fields=scope,
                        status=BusinessKeyStatus.CONFIRMED,
                    ),
                )
            )
            view = _mapping_dataset_views(
                selection, schema, governance, (), active_dataset_index=0
            )[0]
            html = template.render(
                view=view,
                workspace_id=workspace_id,
                formula_authoring_issues_by_dataset={},
                decision_help_by_id=DECISION_HELP_BY_ID,
                url_for=lambda *_args, **_kwargs: "/static/bootstrap-icons.svg",
                app_icon=lambda *_args: "",
            )
            if shows_parent:
                self.assertIn('id="relationship-0-parent_id"', html)
            else:
                # The identity editor also names parent_id; only its separate
                # linked-field card must be absent here.
                self.assertNotIn('id="relationship-0-parent_id"', html)
                self.assertIn("This linked field is part of the matching rule", html)
