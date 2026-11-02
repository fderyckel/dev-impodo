"""Exercise live choice refresh, atomic mapping saves and stale target guards."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from impodo.domain.mapping.contracts import CategoricalCoveragePolicy, ValueMapping
from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetFingerprint
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.domain.mapping.mutations import MappingMutationAction
from impodo.domain.serialization import content_hash
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings


def selection_snapshot(schema, model_name, field_name, choices, **changes):
    """Return the exact verified target with one fictional field's new choices."""
    field = next(field for model in schema.models if model.name == model_name
                 for field in model.fields if field.name == field_name)
    values = asdict(field)
    values.pop("create_default_present")
    values.pop("create_default_value")
    values.update(selection=choices, **changes)
    return MetadataSnapshot(
        TargetFingerprint(schema.connection_target_hash, schema.connection_mode,
            schema.database, schema.odoo_version, datetime.now(UTC).isoformat()),
        {model_name: ModelMetadata(model_name, model_name, {field_name: FieldMetadata(**values)})},
    )


class SelectionChoiceRefreshTests(ProjectSetupBrowserTestCase):
    odoo_version = "19.0"
    def configure(self, mode=OdooConnectionMode.LOCAL):
        self.workspace_id, self.dataset, self.key = self._mapping_ready_workspace(
            scalar_field_count=1, selection_field=True, connection_mode=mode)
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)
        self.schema = self.context.queries.get_odoo_schema_catalog(self.workspace_id)
        if self.odoo_version != self.schema.odoo_version:
            governance = self.context.queries.get_schema_governance(self.workspace_id)
            self.schema = replace(self.schema, odoo_version=self.odoo_version,
                policy_hash=odoo_source_policy_hash(int(self.odoo_version.split(".", 1)[0])))
            self.context.mapping_workspace.schemas.save_odoo_schema_catalog(self.workspace_id, self.schema, actor=self.actor)
            self.context.schema_workspace.govern(self.workspace_id, business_keys=governance.business_keys, actor=self.actor)
        self.choices = (("fr_FR", "French"), ("it_IT", "Italian"))
        self.context.readiness_reader = lambda _state, _requests, _records: (
            selection_snapshot(self.schema, "res.partner", "field_0000", self.choices), None)

    def refresh(self):
        with patch("impodo.web.routers.mapping._source_value_choices", return_value=({"value": "French", "count": 1},)):
            return self.client.post(f"/workspaces/{self.workspace_id}/mapping/value-choices", data={
                "csrf_token": self.csrf, "kind": "scalar", "dataset_id": self.dataset.dataset_id,
                "source_column_key": self.dataset.columns[1].stable_key, "target_model": "res.partner",
                "target_field": "field_0000", "refresh": "1",
            }, headers=POST_HEADERS)

    def test_local_and_remote_refresh_stage_only_then_save_retains_rules(self):
        for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
            with self.subTest(mode=mode):
                self.configure(mode)
                governance = self.context.queries.get_schema_governance(self.workspace_id)
                with collect_duckdb_request_timings() as timings:
                    refreshed = self.refresh()
                self.assertEqual(refreshed.status_code, 200, refreshed.text)
                payload = refreshed.json()
                self.assertEqual(payload["selection_added"], 1)
                self.assertEqual(payload["selection_removed"], 1)
                self.assertEqual(self.context.queries.get_odoo_schema_catalog(self.workspace_id), self.schema)
                self.assertLessEqual(timings.connection_count, 24, timings)
                entries = [
                    ["csrf_token", self.csrf], ["action", "save_progress"],
                    ["expected_parent_version", ""], ["expected_working_draft_version", ""],
                    ["editable_dataset_id", self.dataset.dataset_id],
                    ["selection_refreshes", json.dumps([payload["selection_refresh"]])],
                    ["target_model_0", "res.partner"], ["mode_0", "upsert"], ["on_existing_0", "block"],
                    ["source_identity_0", self.dataset.columns[0].stable_key], ["business_key_0", self.key.key_id],
                    ["identity_source_0_0", self.dataset.columns[0].stable_key],
                    ["visible_scalar_target_0", "field_0000"], ["scalar_value_source_0_1", "source"],
                    ["scalar_source_0_1", self.dataset.columns[1].stable_key], ["scalar_type_0_1", "string"],
                    ["scalar_case_0_1", "preserve"], ["scalar_compare_0_1", "1"], ["scalar_null_0_1", "distinct"],
                    ["scalar_value_matches_0_1", '[{"source_value":"French","target_value":"fr_FR"}]'],
                ]
                saved = self.client.post(f"/workspaces/{self.workspace_id}/mapping/save", json={"entries": entries},
                    headers={**POST_HEADERS, "X-CSRF-Token": self.csrf})
                self.assertEqual(saved.status_code, 200, saved.text)
                current = self.context.queries.get_odoo_schema_catalog(self.workspace_id)
                self.assertNotEqual(current.content_hash, self.schema.content_hash)
                governed = self.context.queries.get_schema_governance(self.workspace_id)
                self.assertEqual(governed.business_keys, governance.business_keys)
                self.assertEqual(governed.catalog_hash, current.content_hash)
                working = self.context.queries.get_mapping_working_draft(self.workspace_id)
                self.assertEqual(working.definition.datasets[0].fields[0].value_mappings, (ValueMapping("French", "fr_FR"),))
                self.assertEqual(working.definition.schema_hash, governed.content_hash)

    def test_unchanged_empty_and_broader_changes_are_honest(self):
        self.configure()
        self.choices = self.schema.models[0].fields[1].selection
        unchanged = self.refresh()
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        self.assertEqual(unchanged.json()["selection_added"], 0)
        self.assertEqual(unchanged.json()["selection_removed"], 0)
        self.choices = ()
        empty = self.refresh()
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["target_choices"], [])
        self.context.readiness_reader = lambda _state, _requests, _records: (
            selection_snapshot(self.schema, "res.partner", "field_0000", (), readonly=True), None)
        rejected = self.refresh()
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("beyond its choices", rejected.json()["detail"])
        self.assertEqual(self.context.queries.get_odoo_schema_catalog(self.workspace_id), self.schema)

    def test_stale_or_tampered_candidate_never_changes_schema(self):
        from impodo.web.selection_choices import load_selection_refresh
        self.configure()
        refreshed = self.refresh()
        reference = refreshed.json()["selection_refresh"]
        with self.assertRaisesRegex(Exception, "unavailable"):
            load_selection_refresh(self.context, self.workspace_id, json.dumps([reference[:-1] + ("0" if reference[-1] != "0" else "1")]), actor=self.actor)
        self.context.mapping_workspace.save_working_draft(self.workspace_id, datasets=(), expected_version=None, actor=self.actor)
        with self.assertRaisesRegex(Exception, "matching inputs changed"):
            load_selection_refresh(self.context, self.workspace_id, json.dumps([reference]), actor=self.actor)
        self.assertEqual(self.context.queries.get_odoo_schema_catalog(self.workspace_id), self.schema)

    def test_interrupted_save_rolls_back_schema_governance_draft_and_receipt(self):
        from impodo.web.selection_choices import load_selection_refresh
        self.configure()
        reference = self.refresh().json()["selection_refresh"]
        refresh = load_selection_refresh(self.context, self.workspace_id, json.dumps([reference]), actor=self.actor)
        governance = self.context.queries.get_schema_governance(self.workspace_id)
        operation_id = str(uuid4())
        self.context.mapping_workspace.begin_mutation(self.workspace_id, operation_id=operation_id,
            action=MappingMutationAction.SAVE_PROGRESS, request_hash=content_hash("selection refresh rollback").removeprefix("sha256:"),
            submitted_working_draft_version=None, submitted_mapping_revision_version=None, actor=self.actor)
        repository = self.context.mapping_workspace.mappings
        with patch.object(type(repository), "_commit_mapping_mutation", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.context.mapping_workspace.save_working_draft(self.workspace_id, datasets=(), expected_version=None,
                    actor=self.actor, operation_id=operation_id, schema_refresh=refresh)
        self.assertEqual(self.context.queries.get_odoo_schema_catalog(self.workspace_id), self.schema)
        self.assertEqual(self.context.queries.get_schema_governance(self.workspace_id), governance)
        self.assertIsNone(self.context.queries.get_mapping_working_draft(self.workspace_id))
        self.assertEqual(self.context.mapping_workspace.get_mutation_receipt(self.workspace_id, operation_id, actor=self.actor).state.value, "PENDING")

    def test_remote_transport_verifies_reader_and_reads_one_field(self):
        self.configure(OdooConnectionMode.REMOTE)
        self.context.readiness_reader = None
        self.context.read_identity_probe = lambda *_args: OdooReadIdentity(
            target_hash=self.schema.connection_target_hash, principal_hash=self.schema.read_principal_hash,
            permission_hash=self.schema.read_permission_hash, context_hash=self.schema.read_context_hash,
            readable_models=("res.partner",), observed_at=datetime.now(UTC).isoformat())
        with patch("impodo.web.selection_choices._read_selection_metadata", return_value=
                selection_snapshot(self.schema, "res.partner", "field_0000", self.choices)) as read:
            response = self.refresh()
        self.assertEqual(response.status_code, 200, response.text)
        read.assert_called_once()
        self.assertEqual(read.call_args.args[2][0].fields, ("field_0000",))
        self.context.read_identity_probe = lambda *_args: OdooReadIdentity(
            target_hash=self.schema.connection_target_hash, principal_hash=content_hash("another reader"),
            permission_hash=self.schema.read_permission_hash, context_hash=self.schema.read_context_hash,
            readable_models=("res.partner",), observed_at=datetime.now(UTC).isoformat())
        rejected = self.refresh()
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("access changed", rejected.json()["detail"])
        self.assertEqual(self.context.queries.get_odoo_schema_catalog(self.workspace_id), self.schema)

    def test_local_transport_uses_checked_configuration_and_no_record_read(self):
        self.configure()
        self.context.readiness_reader = None
        with patch("impodo.web.selection_choices._selected_local_profile", return_value=SimpleNamespace()), \
             patch("impodo.web.selection_choices.local_read_credential_binding_hash", return_value=self.schema.read_credential_binding_hash), \
             patch.object(self.context.local_odoo_reader, "get_preflight_snapshots", return_value=(
                 selection_snapshot(self.schema, "res.partner", "field_0000", self.choices), None)) as read:
            response = self.refresh()
        self.assertEqual(response.status_code, 200, response.text)
        read.assert_called_once()
        self.assertEqual(read.call_args.args[3], ())


class SelectionChoiceRefreshOdoo20Tests(SelectionChoiceRefreshTests):
    odoo_version = "20.0"
