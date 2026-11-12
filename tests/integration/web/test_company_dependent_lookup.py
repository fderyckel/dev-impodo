"""Choose company-dependent account constants without a second company selector."""

from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.run.odoo_requirements import (
    OdooCheckRelationshipRequirement,
    OdooCheckSupportingRequirement,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.target_readers import _capture_recipe_supporting_values
from impodo.web.presenters.mapping_forms import _related_business_keys
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
)
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm
from tests.support.identity import resolve_local_actor


class CompanyDependentLookupTests(ProjectSetupBrowserTestCase):
    def configure(self, major, mode):
        context = self.app.state.context
        workspace_id, source, identity_key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=2,
            relationship_model="account.account",
            relationship_field_names=(
                "property_account_payable_id",
                "property_account_receivable_id",
            ),
            relationship_field_labels=("Account Payable", "Account Receivable"),
            connection_mode=mode,
            odoo_major=major,
        )
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        company = OdooCompanySelection(
            schema.connection_target_hash, 105, "Example Wiltz"
        )
        context.workspace_states.select_odoo_company(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=company,
        )
        schema = replace(
            schema,
            read_context_hash=company.context_hash,
            models=(
                replace(
                    schema.models[0],
                    fields=tuple(
                        replace(field, company_dependent=True)
                        if field.type == "many2one"
                        else field
                        for field in schema.models[0].fields
                    ),
                ),
            ),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        run_local_operation_sync(
            context.schema_workspace.govern_complete,
            workspace_id,
            business_keys=(identity_key,),
            expected_catalog_hash=schema.content_hash,
            actor=actor,
        )
        keys = _related_business_keys((), "account.account", odoo_major_version=major)
        self.assertEqual([key.key_fields for key in keys], [("code",), ("name",)])
        self.assertTrue(all(not key.scope_fields for key in keys))
        payload = {
            "csrf_token": self.csrf,
            "kind": "constant_relationship",
            "dataset_id": source.dataset_id,
            "source_column_key": "",
            "target_model": "res.partner",
            "target_field": "property_account_payable_id",
            "business_key_id": keys[0].key_id,
            "constant_value": "440000",
        }
        return workspace_id, payload, schema

    def reader(self, schema, calls):
        def read(state, metadata_requests, record_requests):
            self.assertFalse(getattr(_read_databases, "connections", None))
            self.assertEqual(state.odoo_operation_context["allowed_company_ids"], [105])
            calls.append((metadata_requests, record_requests))
            fingerprint = replace(
                _browser_schema(state).fingerprint, odoo_version=schema.odoo_version
            )
            fields = {
                "code": FieldMetadata("code", "char"),
                "name": FieldMetadata("name", "char"),
            }
            return MetadataSnapshot(
                fingerprint,
                {
                    "account.account": ModelMetadata(
                        "account.account", "Account", fields
                    )
                },
            ), RecordSnapshot(
                fingerprint,
                {
                    "account.account": (
                        TargetRecord(
                            "account.account",
                            7,
                            {"code": "440000", "name": "Suppliers"},
                        ),
                    )
                },
                {"account.account": record_requests[0].fields},
            )

        return read

    def test_lookup_display_save_and_reopen_for_both_majors_and_access_modes(self):
        context = self.app.state.context
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, payload, schema = self.configure(major, mode)
                    url = f"/workspaces/{workspace_id}/mapping"
                    calls = []
                    with patch.object(
                        context,
                        "readiness_reader",
                        side_effect=self.reader(schema, calls),
                    ):
                        with (
                            count_duckdb_statements() as sql,
                            collect_duckdb_request_timings() as timing,
                        ):
                            choices = self.client.post(
                                url + "/value-choices",
                                data=payload,
                                headers=POST_HEADERS,
                            )
                        self.assertEqual(choices.status_code, 200, choices.text)
                        self.assertEqual(
                            choices.json()["target_choices"],
                            [{"value": "440000", "label": "Suppliers (440000)"}],
                        )
                        self.assertLessEqual(timing.connection_count, 6)
                        self.assertLessEqual(timing.schema_check_count, 4)
                        self.assertLessEqual(len(sql), 100)
                        self.assertEqual(timing.lock_retry_count, 0)
                        name_key = _related_business_keys(
                            (), "account.account", odoo_major_version=major
                        )[1]
                        named = self.client.post(
                            url + "/value-choices",
                            data={
                                **payload,
                                "business_key_id": name_key.key_id,
                                "constant_value": "Suppliers",
                            },
                            headers=POST_HEADERS,
                        )
                        self.assertEqual(named.status_code, 200, named.text)
                        self.assertEqual(
                            named.json()["target_choices"],
                            [{"value": "Suppliers", "label": "Suppliers"}],
                        )
                        # Different field domains never consume each other's cached list.
                        for edge in (
                            "property_account_payable_id",
                            "property_account_receivable_id",
                            "property_account_payable_id",
                        ):
                            response = self.client.post(
                                url + "/value-choices",
                                data={
                                    **payload,
                                    "target_field": edge,
                                    "constant_value": "",
                                },
                                headers=POST_HEADERS,
                            )
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertFalse(response.json()["target_choices_reused"])
                    self.assertEqual(len(calls), 5)
                    page = self.client.get(url)
                    self.assertIn("Company for this lookup:", page.text)
                    self.assertIn("Example Wiltz", page.text)
                    self.assertIn("Filter loaded Account Payable choices", page.text)
                    self.assertNotIn("Name within Company (if available)", page.text)
                    form = MappingForm(page.text)
                    for name, value in (
                        ("source_identity_0", "column:ref"),
                        ("identity_source_0_0", "column:ref"),
                        ("relation_value_source_0_0", "constant_existing"),
                        ("relation_constant_key_0_0", payload["business_key_id"]),
                        ("relation_constant_component_0_0_0", "440000"),
                    ):
                        form.entries = [
                            entry for entry in form.entries if entry[0] != name
                        ]
                        form.entries.append([name, value])
                    form.entries.append(["action", "save_progress"])
                    saved = self.client.post(
                        url + "/save",
                        json={"entries": form.entries},
                        headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                    )
                    self.assertEqual(saved.status_code, 200, saved.text[:500])
                    draft = context.queries.get_mapping_working_draft(workspace_id)
                    self.assertIsNotNone(draft, saved.text[:500])
                    reference = (
                        draft.definition.datasets[0].relationships[0].constant_reference
                    )
                    self.assertEqual(reference.key_values[0].value, "440000")
                    self.assertEqual(reference.scope_values, ())
                    self.assertEqual(
                        context.queries.get(workspace_id).odoo_company.company_id, 105
                    )
                    reopened = self.client.get(url)
                    self.assertIn('value="440000"', reopened.text)
                    self.assertIn('value="constant_existing" selected', reopened.text)

    def test_recipe_account_references_keep_company_and_portable_key(self):
        context = self.app.state.context
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, _, schema = self.configure(major, mode)
                    schema = replace(
                        schema,
                        models=(
                            *schema.models,
                            SchemaModel(
                                "account.account",
                                "Accounts",
                                (
                                    SchemaField(
                                        "code",
                                        "Code",
                                        "char",
                                        False,
                                        False,
                                        None,
                                        None,
                                        (),
                                    ),
                                    SchemaField(
                                        "name",
                                        "Name",
                                        "char",
                                        True,
                                        False,
                                        None,
                                        None,
                                        (),
                                    ),
                                ),
                            ),
                        ),
                    )
                    requirement = OdooCheckSupportingRequirement(
                        "account.account",
                        ("code",),
                        (),
                        (
                            OdooCheckRelationshipRequirement(
                                "res.partner", "property_account_payable_id", "many2one"
                            ),
                        ),
                        ("Suppliers",),
                    )
                    calls = []
                    with patch.object(
                        context,
                        "readiness_reader",
                        side_effect=self.reader(schema, calls),
                    ):
                        stored = _capture_recipe_supporting_values(
                            context,
                            context.queries.get(workspace_id),
                            schema,
                            (requirement,),
                            actor=resolve_local_actor(context),
                        )
                    self.assertEqual(len(stored), 1)
                    self.assertEqual(stored[0].choices[0].value, "440000")
                    self.assertEqual(
                        stored[0].read_context_hash, schema.read_context_hash
                    )
                    self.assertEqual(
                        stored[0].reference_policy_hash, reference_policy_hash(major)
                    )
