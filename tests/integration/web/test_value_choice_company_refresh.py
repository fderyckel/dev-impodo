"""Exercise the Unit value-matching dialog after choosing the load company."""

from dataclasses import replace
from unittest.mock import Mock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetRecord,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.presenters.mapping_forms import _related_business_keys
from impodo.web.target_credentials import local_read_credential_binding_hash
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor
from tests.support.recipe_lifecycle import metadata_for_schema


class ValueChoiceCompanyRefreshTests(ProjectSetupBrowserTestCase):
    def test_unit_dialog_refreshes_company_before_reading_choices(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self._check_unit_dialog(major, mode)

    def _check_unit_dialog(self, major, mode):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_model="uom.uom",
            target_model="product.template",
            relationship_field_names=("uom_id",),
            relationship_field_labels=("Unit",),
            connection_mode=mode,
            odoo_major=major,
        )
        state = context.queries.get(workspace_id)
        original = context.queries.get_odoo_schema_catalog(workspace_id)
        metadata = metadata_for_schema(original)
        identity = OdooReadIdentity(
            original.connection_target_hash,
            original.read_principal_hash,
            original.read_permission_hash,
            original.read_context_hash,
            ("product.template",),
            metadata.fingerprint.snapshot_timestamp,
        )
        remote = mode is OdooConnectionMode.REMOTE
        schema = context.schema_workspace.capture(
            workspace_id,
            metadata,
            read_credential_binding_hash=(
                original.read_credential_binding_hash
                if remote
                else local_read_credential_binding_hash(state)
            ),
            read_identity=identity if remote else None,
            actor=actor,
        )
        context.schema_workspace.govern(workspace_id, business_keys=(key,), actor=actor)
        company = OdooCompanySelection(
            schema.connection_target_hash, 105, "Example Factory"
        )
        context.workspace_states.select_odoo_company(
            workspace_id,
            actor=actor,
            expected_revision=context.queries.get(workspace_id).revision,
            selection=company,
        )
        self.assertNotEqual(schema.read_context_hash, company.context_hash)
        related_metadata = replace(
            metadata,
            models={
                "uom.uom": ModelMetadata(
                    "uom.uom",
                    "Unit",
                    {"name": FieldMetadata("name", "char", required=True)},
                )
            },
        )
        calls = []

        def read_fields(workspace, _credential_or_profile, *_models):
            self.assertFalse(getattr(_read_databases, "connections", None))
            self.assertEqual(workspace.odoo_operation_context, company.context)
            calls.append("fields")
            return metadata

        def probe(workspace, _key, models):
            self.assertFalse(getattr(_read_databases, "connections", None))
            self.assertEqual(workspace.odoo_operation_context, company.context)
            return replace(
                identity, context_hash=company.context_hash, readable_models=models
            )

        def read_records(requests):
            self.assertFalse(getattr(_read_databases, "connections", None))
            self.assertEqual(
                [(item.model, item.fields, item.limit) for item in requests],
                [("uom.uom", ("name",), 2001)],
            )
            calls.append("units")
            return RecordSnapshot(
                metadata.fingerprint,
                {"uom.uom": (TargetRecord("uom.uom", 41, {"name": "Units"}),)},
                {"uom.uom": ("name",)},
            )

        def read_local(workspace, _profile, _metadata, requests, *, related_models):
            self.assertEqual(workspace.odoo_operation_context, company.context)
            self.assertEqual(related_models, ("uom.uom",))
            return related_metadata, read_records(requests)

        def connector(config):
            self.assertEqual(config.context["allowed_company_ids"], [105])
            return Mock(
                get_model_metadata=Mock(return_value=related_metadata),
                get_records=Mock(side_effect=read_records),
            )

        payload = {
            "csrf_token": self.csrf,
            "kind": "relationship",
            "dataset_id": dataset.dataset_id,
            "source_column_key": dataset.columns[1].stable_key,
            "target_model": "product.template",
            "target_field": "uom_id",
            "business_key_id": _related_business_keys((), "uom.uom")[0].key_id,
        }
        url = f"/workspaces/{workspace_id}/mapping/value-choices"
        profile = None if remote else object()
        with (
            patch.object(context, "readiness_reader", None),
            patch.object(context, "schema_read_session_factory", None),
            patch.object(context, "schema_reader", side_effect=read_fields),
            patch.object(context, "read_identity_probe", side_effect=probe),
            patch.object(
                context.local_odoo_reader, "get_model_metadata", side_effect=read_fields
            ),
            patch.object(
                context.local_odoo_reader,
                "get_preflight_snapshots",
                side_effect=read_local,
            ),
            patch(
                "impodo.web.routers.matching_company_refresh._selected_local_profile",
                return_value=profile,
            ),
            patch(
                "impodo.web.composition.target_readers._selected_local_profile",
                return_value=profile,
            ),
            patch(
                "impodo.web.composition.target_readers.Json2ReadConnector",
                side_effect=connector,
            ),
            patch(
                "impodo.web.routers.mapping._source_value_choices",
                return_value=({"value": "Unit", "count": 1},),
            ),
        ):
            with (
                count_duckdb_statements() as statements,
                collect_duckdb_request_timings() as timings,
            ):
                choices = self.client.post(url, data=payload, headers=POST_HEADERS)
            self.assertEqual(choices.status_code, 200, choices.text)
            self.assertEqual(calls, ["fields", "units"])
            self.assertEqual(
                choices.json()["source_choices"], [{"value": "Unit", "count": 1}]
            )
            self.assertEqual(
                choices.json()["target_choices"], [{"value": "Units", "label": "Units"}]
            )
            self.assertFalse(choices.json()["target_choices_reused"])
            # The stale request adds the refresh publication scope to the lookup.
            self.assertLessEqual(timings.connection_count, 16)
            self.assertLessEqual(timings.schema_check_count, 11)
            self.assertLessEqual(len(statements), 210)
            self.assertEqual(timings.lock_retry_count, 0)
            self.assertEqual(
                sum("duckdb_columns()" in statement for statement in statements),
                timings.schema_check_count,
            )

            checked = context.queries.get_odoo_schema_catalog(workspace_id)
            self.assertEqual(checked.content_hash, schema.content_hash)
            self.assertEqual(checked.read_context_hash, company.context_hash)
            reopened = self.client.post(url, data=payload, headers=POST_HEADERS)
            self.assertEqual(reopened.status_code, 200, reopened.text)
            self.assertTrue(reopened.json()["target_choices_reused"])
            self.assertEqual(
                reopened.json()["target_choices"], choices.json()["target_choices"]
            )
            self.assertEqual(calls, ["fields", "units"])
