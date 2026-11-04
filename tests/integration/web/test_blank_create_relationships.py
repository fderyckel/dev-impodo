"""Save, check, and reopen blank relationship choices through Stage 3."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata
from impodo.domain.workspace.contracts import SchemaField
from impodo.web.presenters.mapping_forms import _related_business_keys
from tests.support.browser_scenarios import (
    POST_HEADERS, OdooConnectionMode, ProjectSetupBrowserTestCase, _browser_schema,
)


class BlankCreateRelationshipBrowserTests(ProjectSetupBrowserTestCase):
    def test_match_values_checks_counts_and_reopens_blank_create_choice(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self._exercise(major, mode)

    def _exercise(self, major, mode):
        target_field, related_model, label = (
            ("uom_id", "uom.uom", "Unit") if major == 19
            else ("categ_id", "product.category", "Product Category")
        )
        field = SchemaField(target_field, label, "many2one", True, False, related_model, None, ())
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=0, target_model="product.template", connection_mode=mode,
            odoo_major=major, additional_fields=(
                SchemaField("name", "Name", "char", True, False, None, None, ()), field,
            ),
        )
        context = self.app.state.context
        target = context.queries.get(workspace_id)
        fingerprint = replace(_browser_schema(target).fingerprint, odoo_version=f"{major}.0")
        key = _related_business_keys((), related_model)[0]
        path = f"/workspaces/{workspace_id}/mapping"
        identity, source = (column.stable_key for column in dataset.columns)
        choice_form = {
            "csrf_token": self.csrf, "kind": "relationship", "dataset_id": dataset.dataset_id,
            "source_column_key": source, "target_model": "product.template",
            "target_field": target_field, "business_key_id": key.key_id,
        }
        metadata = MetadataSnapshot(fingerprint, {"product.template": ModelMetadata(
            "product.template", "Product", {target_field: FieldMetadata(
                target_field, "many2one", label=label, required=True, relation=related_model,
            )},
        )}, create_defaults={"product.template": {target_field: 1}})
        target_choices = ({"value": "Default", "label": "Default"},)
        with (
            patch("impodo.web.routers.mapping._source_value_choices", return_value=()),
            patch("impodo.web.routers.mapping._relationship_value_choices",
                  return_value=(target_choices, (), datetime.now(UTC), False)),
            patch.object(context, "readiness_reader", return_value=(metadata, RecordSnapshot(fingerprint, {}, {}))),
        ):
            choices = self.client.post(path + "/value-choices", data=choice_form, headers=POST_HEADERS)
            self.assertEqual(choices.status_code, 200, choices.text)
            self.assertEqual(choices.json()["blank_source_count"], 1)
            self.assertFalse(choices.json()["blank_odoo_default_verified"])
            checked = self.client.post(
                path + "/value-choices", data={**choice_form, "check_blank_default": "1"},
                headers=POST_HEADERS,
            )
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertTrue(checked.json()["blank_odoo_default_verified"])

        blank_choice = {"default": True} if mode is OdooConnectionMode.LOCAL else {"key": key.key_id, "record": "Default"}
        mapping_form = {
            "action": "save_progress", "csrf_token": self.csrf, "editable_dataset_id": dataset.dataset_id,
            "target_model_0": "product.template", "mode_0": "upsert", "on_existing_0": "block",
            "source_identity_0": identity, "business_key_0": business_key.key_id,
            "identity_source_0_0": identity, "visible_scalar_target_0": "name",
            "scalar_value_source_0_1": "source", "scalar_source_0_1": source,
            "visible_relation_target_0": target_field, "relation_source_0_0": source,
            "relation_value_source_0_0": "source", "relation_origin_0_0": "target_catalog",
            "relation_key_0_0": key.key_id, "relation_blank_create_0_0": json.dumps(blank_choice),
        }
        saved = self.client.post(path + "/save", json={"entries": list(mapping_form.items())},
                                 headers={**POST_HEADERS, "X-CSRF-Token": self.csrf})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "committed", saved.text)
        working = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(working.definition.datasets[0].relationships[0].blank_create_values,
                         () if mode is OdooConnectionMode.LOCAL else ("Default",))
        page = self.client.get(path + f"?relation_query={target_field}")
        self.assertIn("mapping-blank-create.js", page.text)
        self.assertIn("When the source is blank on a new record", page.text)
        self.assertIn("data-blank-create-storage", page.text)
