"""Look up one parent key without browsing or caching its whole Odoo model."""

from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
)
from tests.support.identity import resolve_local_actor


class ConstantRecordLookupTests(ProjectSetupBrowserTestCase):
    def test_generic_custom_model_and_key_use_the_same_lookup_on_both_majors(self):
        context = self.app.state.context
        for major in (19, 20):
            with self.subTest(major=major):
                model, key_field = "x.catalog_item", "x_reference"
                workspace_id, form, schema = self.configure(
                    major, model=model, key_field=key_field
                )
                value = "PARENT-42"
                records = (
                    TargetRecord(model, 42, {key_field: value, "name": "Parent item"}),
                )
                with patch.object(
                    context,
                    "readiness_reader",
                    side_effect=self.reader(
                        schema,
                        records,
                        (),
                        key_field=key_field,
                        matching_value=value,
                    ),
                ):
                    response = self.post(
                        workspace_id, {**form, "constant_value": value}
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json()["target_choices"],
                    [{"value": value, "label": "Parent item (PARENT-42)"}],
                )

    def configure(
        self,
        major=20,
        mode=OdooConnectionMode.REMOTE,
        *,
        model="account.account",
        key_field="code",
    ):
        workspace_id, dataset, _ = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_field_names=("parent_id",),
            relationship_field_labels=("Parent Account",),
            relationship_model=model,
            target_model=model,
            connection_mode=mode,
        )
        context = self.app.state.context
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
        fields = tuple(
            SchemaField(name, label, "char", False, False, None, None, ())
            for name, label in (
                (key_field, key_field.replace("_", " ").title()),
                ("name", "Name"),
            )
        )
        schema = replace(
            schema,
            odoo_version=f"{major}.0",
            read_context_hash=company.context_hash,
            models=(
                replace(schema.models[0], fields=(*schema.models[0].fields, *fields)),
            ),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        key = BusinessKeyDefinition(
            f"{model}:{key_field}",
            model,
            (key_field,),
            status=BusinessKeyStatus.CONFIRMED,
        )
        run_local_operation_sync(
            context.schema_workspace.govern_complete,
            workspace_id,
            business_keys=(key,),
            expected_catalog_hash=schema.content_hash,
            actor=actor,
        )
        form = {
            "csrf_token": self.csrf,
            "kind": "constant_relationship",
            "dataset_id": dataset.dataset_id,
            "source_column_key": "",
            "target_model": model,
            "target_field": "parent_id",
            "business_key_id": key.key_id,
            "constant_value": "401100",
        }
        return workspace_id, form, schema

    def reader(
        self,
        schema,
        exact_records,
        broad_records,
        *,
        key_field="code",
        matching_value="401100",
    ):
        model = schema.models[0].name

        def read(state, metadata_requests, record_requests):
            self.assertFalse(
                getattr(_read_databases, "connections", None),
                "Database owner crossed the Odoo boundary",
            )
            self.assertEqual(state.odoo_company.name, "Example Wiltz")
            self.assertEqual(state.odoo_company.context_hash, schema.read_context_hash)
            self.assertEqual(len(record_requests), 1)
            request = record_requests[0]
            self.assertEqual(request.fields, (key_field, "name"))
            self.assertEqual(request.limit, 2_001)
            if request.domain:
                self.assertEqual(request.domain, ((key_field, "=", matching_value),))
            fingerprint = replace(
                _browser_schema(state).fingerprint, odoo_version=schema.odoo_version
            )
            return MetadataSnapshot(
                fingerprint,
                {
                    model: ModelMetadata(
                        model,
                        "Account",
                        {
                            key_field: FieldMetadata(key_field, "char"),
                            "name": FieldMetadata("name", "char"),
                        },
                    )
                },
            ), RecordSnapshot(
                fingerprint,
                {model: exact_records if request.domain else broad_records},
                {model: request.fields},
            )

        return read

    def post(self, workspace_id, form):
        return self.client.post(
            f"/workspaces/{workspace_id}/mapping/value-choices",
            data=form,
            headers=POST_HEADERS,
        )

    def test_exact_parent_lookup_works_above_the_browse_limit_for_both_majors_and_modes(
        self,
    ):
        context = self.app.state.context
        records = (
            TargetRecord(
                "account.account", 1729, {"code": "401100", "name": "Customers"}
            ),
        )
        large = tuple(
            TargetRecord("account.account", i + 1, {"code": str(i), "name": "Other"})
            for i in range(2_001)
        )
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, form, schema = self.configure(major, mode)
                    with (
                        patch.object(
                            context,
                            "readiness_reader",
                            side_effect=self.reader(schema, records, large),
                        ),
                        patch(
                            "impodo.web.routers.mapping._source_value_choices",
                            side_effect=AssertionError(
                                "Constant lookup scanned source rows"
                            ),
                        ),
                    ):
                        broad = self.post(workspace_id, {**form, "constant_value": ""})
                        self.assertEqual(broad.status_code, 422)
                        self.assertIn("Enter a matching value", broad.json()["detail"])
                        with collect_duckdb_request_timings() as timings:
                            exact = self.post(workspace_id, form)
                        self.assertEqual(exact.status_code, 200, exact.text)
                        self.assertEqual(
                            exact.json()["target_choices"],
                            [{"value": "401100", "label": "Customers (401100)"}],
                        )
                        self.assertFalse(exact.json()["target_choices_reused"])
                        # Access opens two owners; the bounded local input read
                        # opens four lineage owners. Exact results have no cache write.
                        self.assertLessEqual(timings.connection_count, 6)
                        self.assertLessEqual(timings.schema_check_count, 4)
                        self.assertEqual(timings.lock_retry_count, 0)

    def test_exact_lookup_does_not_replace_the_full_catalogue_cache(self):
        workspace_id, form, schema = self.configure()
        context = self.app.state.context
        exact = (
            TargetRecord(
                "account.account", 1729, {"code": "401100", "name": "Customers"}
            ),
        )
        broad = (
            TargetRecord(
                "account.account", 7, {"code": "999999", "name": "Other Account"}
            ),
        )
        with patch.object(
            context, "readiness_reader", side_effect=self.reader(schema, exact, broad)
        ) as reader:
            first = self.post(workspace_id, {**form, "constant_value": ""})
            self.assertEqual(first.status_code, 200, first.text)
            chosen = self.post(workspace_id, form)
            self.assertEqual(chosen.json()["target_choices"][0]["value"], "401100")
            cached = self.post(workspace_id, {**form, "constant_value": ""})
            self.assertEqual(
                cached.json()["target_choices"], first.json()["target_choices"]
            )
            self.assertTrue(cached.json()["target_choices_reused"])
            self.assertEqual(reader.call_count, 2)

    def test_missing_and_duplicate_keys_are_not_presented_as_unique_matches(self):
        workspace_id, form, schema = self.configure()
        context = self.app.state.context
        duplicates = tuple(
            TargetRecord("account.account", i, {"code": "401100", "name": "Customers"})
            for i in (1, 2)
        )
        for records in ((), duplicates):
            with (
                self.subTest(count=len(records)),
                patch.object(
                    context,
                    "readiness_reader",
                    side_effect=self.reader(schema, records, ()),
                ),
            ):
                response = self.post(workspace_id, form)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["target_choices"], [])
                self.assertEqual(
                    response.json()["ambiguous_values"], ["401100"] if records else []
                )
