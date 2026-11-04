"""Exercise reviewed Odoo field additions through the Match data routes."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ScalarFieldMapping,
)
from impodo.domain.shared.models import FieldMetadata
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase, _browser_schema
from tests.support.identity import resolve_local_actor


class MappingOptionalFieldsTests(ProjectSetupBrowserTestCase):
    def test_optional_refresh_keeps_saved_matches_and_enables_comparison(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self._exercise_refresh(major, mode)

    def _exercise_refresh(self, major, mode):
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=1, connection_mode=mode, odoo_major=major,
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        previous = context.queries.get_odoo_schema_catalog(workspace_id)
        template = _browser_schema(state)
        model = template.models["res.partner"]
        fields = {
            "ref": model.fields["ref"],
            "field_0000": FieldMetadata("field_0000", "char", label="Existing field"),
        }
        snapshot = replace(
            template,
            fingerprint=replace(template.fingerprint, odoo_version=f"{major}.0"),
            models={model.model: replace(model, fields=fields)},
        )
        identity = (
            context.read_identity_probe(state, "read-secret", (model.model,))
            if mode is OdooConnectionMode.REMOTE else None
        )
        binding = previous.read_credential_binding_hash
        initial = context.schema_workspace.capture(
            workspace_id, snapshot, read_credential_binding_hash=binding,
            read_identity=identity, actor=actor,
        )
        rules = context.schema_workspace.govern(workspace_id, business_keys=(key,), actor=actor)
        identity_column, value_column = (column.stable_key for column in dataset.columns)
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model=model.model,
            mode=MappingTargetMode.UPSERT,
            source_identity_column_keys=(identity_column,),
            target_identity=(IdentityComponentMapping(
                source_column_keys=(identity_column,), target_fields=key.key_fields,
            ),),
            fields=(ScalarFieldMapping("field_0000", value_column),),
        )
        revision, _ = context.mapping_workspace.check_definition(
            workspace_id, datasets=(mapping,), expected_parent_version=None,
            expected_working_draft_version=None, actor=actor,
        )
        url = f"/workspaces/{workspace_id}"
        self.assertIn("Check for new or changed Odoo fields", self.client.get(f"{url}/mapping").text)
        reference = FieldMetadata(
            "customer_reference", "char", label="Customer Reference", readonly=True, stored=True,
        )

        for required in (True, False):
            observed = replace(snapshot, models={model.model: replace(model, fields={
                **fields,
                reference.name: replace(reference, required=required),
                "x_optional": FieldMetadata("x_optional", "char", stored=True),
            })})
            with patch(
                "impodo.web.routers.schema._read_selected_schema_sync",
                return_value=(observed, binding, identity, None),
            ):
                check = self._post(f"{url}/schema/capture", {
                    "csrf_token": self.csrf, "return_to_mapping": "1",
                })
            self.assertEqual(check.status_code, 303)
            self.assertEqual(check.headers["location"], f"{url}/mapping#odoo-field-refresh")
            review = self.client.get(check.headers["location"])
            self.assertIn("Odoo field changes", review.text)
            self.assertIn("Customer Reference", review.text)
            checked = context.queries.get_odoo_schema_catalog(workspace_id)
            self.assertEqual(checked.content_hash, initial.content_hash)
            self.assertEqual(context.queries.get_mapping_revision(workspace_id), revision)
            pending = checked.pending_refresh
            if not required and mode is OdooConnectionMode.REMOTE:
                stage_two = self.client.get(f"{url}/schema")
                self.assertIn("Apply new optional fields", stage_two.text)
                self.assertNotIn("retires matching and prepared work", stage_two.text)
                confirmation = {
                    "csrf_token": self.csrf,
                    "candidate_id": pending.candidate_id,
                    "expected_current_content_hash": pending.expected_current_content_hash,
                    "candidate_semantic_hash": pending.semantic_hash,
                    "confirm_schema_refresh": "1",
                    "working_version": "0",
                }
                stale = self._post(f"{url}/schema/capture/confirm", confirmation)
                self.assertEqual(stale.status_code, 422)
                self.assertEqual(context.queries.get_mapping_revision(workspace_id), revision)
                self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), checked)
                confirmation["working_version"] = "1"
                applied = self._post(f"{url}/schema/capture/confirm", confirmation)
            else:
                applied = self._post(f"{url}/mapping/odoo-fields/apply", {
                    "csrf_token": self.csrf,
                    "candidate_id": pending.candidate_id,
                    "working_version": "1",
                })
            self.assertEqual(applied.status_code, 303)
            self.assertTrue(applied.headers["location"].startswith(f"{url}/mapping"))
            result = self.client.get(applied.headers["location"])
            if required:
                self.assertIn("Review the new required fields on Odoo data", result.text)
                self.assertEqual(context.queries.get_mapping_revision(workspace_id), revision)

        self.assertIn("kept your field matches and matching rules", result.text)
        refreshed = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertIsNone(refreshed.pending_refresh)
        self.assertNotEqual(refreshed.content_hash, initial.content_hash)
        governance = context.queries.get_schema_governance(workspace_id)
        self.assertEqual(governance.business_keys, rules.business_keys)
        self.assertEqual(governance.catalog_hash, refreshed.content_hash)
        draft = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(draft.definition.datasets, revision.definition.datasets)
        self.assertEqual(draft.definition.schema_hash, governance.content_hash)
        page = self.client.get(f"{url}/mapping?compare_only=1&field_query=customer_reference")
        self.assertIn('data-target-field="customer_reference"', page.text)
        self.assertIn("Compare only", page.text)
        self.assertIn('type="hidden" name="scalar_validate_only_0_0" value="1"', page.text)
        writable = self.client.get(f"{url}/mapping?field_query=x_optional")
        self.assertIn('data-target-field="x_optional"', writable.text)
        saved = self._post(f"{url}/mapping/save?compare_only=1&field_query=customer_reference", {
            "csrf_token": self.csrf,
            "action": "draft",
            "expected_parent_version": "",
            "expected_working_draft_version": str(draft.version),
            "editable_dataset_id": dataset.dataset_id,
            "target_model_0": model.model,
            "mode_0": "upsert",
            "source_identity_0": identity_column,
            "business_key_0": key.key_id,
            "identity_source_0_0": identity_column,
            "visible_scalar_target_0": reference.name,
            "scalar_value_source_0_0": "source",
            "scalar_source_0_0": value_column,
            "scalar_validate_only_0_0": "1",
            "scalar_compare_0_0": "1",
        })
        self.assertEqual(saved.status_code, 303)
        current = context.queries.get_mapping_revision(workspace_id)
        self.assertIsNotNone(current)
        saved_fields = {
            field.target_field: field for field in current.definition.datasets[0].fields
        }
        self.assertEqual(saved_fields["field_0000"], revision.definition.datasets[0].fields[0])
        self.assertTrue(saved_fields[reference.name].compare)
        self.assertTrue(saved_fields[reference.name].validate_only)
        validation = context.queries.get_mapping_validation(workspace_id, current.version)
        self.assertEqual(validation.status.value, "VALID")
