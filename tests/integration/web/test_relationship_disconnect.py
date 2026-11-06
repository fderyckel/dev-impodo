"""Persist explicit removal of linked-field providers through the editor."""

from unittest.mock import patch
from uuid import uuid4

import polars as pl

from tests.support.browser_scenarios import (
    POST_HEADERS,
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.hierarchy_correction import MappingForm


class RelationshipDisconnectBrowserTests(ProjectSetupBrowserTestCase):
    """Keep cleared choices authoritative across saves and checked revisions."""

    def test_source_connection_can_be_cleared_after_checking(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                for kind in ("many2one", "many2many"):
                    with self.subTest(major=major, mode=mode, kind=kind):
                        self._exercise_disconnect(major, mode, kind, "source")

    def test_constant_connection_can_be_cleared_after_checking(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                for kind in ("many2one", "many2many"):
                    with self.subTest(major=major, mode=mode, kind=kind):
                        self._exercise_disconnect(
                            major, mode, kind, "constant_existing"
                        )

    @staticmethod
    def _set_value(form, name, value):
        form.entries[:] = [item for item in form.entries if item[0] != name]
        form.entries.append([name, value])

    def _read_form(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        return MappingForm(response.text)

    def _post_form(self, form, action):
        self._set_value(form, "action", action)
        self._set_value(form, "operation_id", str(uuid4()))
        response = self.client.post(
            form.action,
            json={"entries": form.entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "committed", response.text)

    def _exercise_disconnect(self, major, mode, kind, provider):
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=2,
            relationship_field_type=kind,
            odoo_major=major,
            connection_mode=mode,
        )
        context = self.app.state.context
        source_before = context.queries.get_mapping_source_selection(workspace_id)
        path = f"/workspaces/{workspace_id}/mapping?step=tables&task=links"
        identity, source = (column.stable_key for column in dataset.columns)
        form = self._read_form(path)
        for name, value in {
            "source_identity_0": identity,
            "business_key_0": key.key_id,
            "identity_source_0_0": identity,
        }.items():
            self._set_value(form, name, value)
        for index in (0, 1):
            for name, value in {
                f"relation_value_source_0_{index}": "source",
                f"relation_source_0_{index}": source,
                f"relation_key_0_{index}": key.key_id,
                f"relation_origin_0_{index}": "target_catalog",
                f"relation_categorical_policy_0_{index}": "EXACT_BUSINESS_KEY",
            }.items():
                self._set_value(form, name, value)
        self._post_form(form, "save_progress")

        form = self._read_form(path)
        if provider == "constant_existing":
            self._set_value(form, "relation_value_source_0_0", provider)
            self._set_value(form, "relation_constant_key_0_0", key.key_id)
            self._set_value(form, "relation_constant_component_0_0_0", "Example")
        else:
            # Older clients submit source columns without a provider selector.
            form.entries[:] = [
                item for item in form.entries if item[0] != "relation_value_source_0_0"
            ]
        self._post_form(form, "save_progress")
        working = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(len(working.definition.datasets[0].relationships), 2)
        self.assertEqual(
            dict(self._read_form(path).entries)["relation_value_source_0_0"],
            provider,
        )

        # This fixture uses synthetic source evidence and simulated Odoo reads.
        # Exercise the real check and persistence commands with bounded row data.
        with (
            patch(
                "impodo.web.routers.mapping._capture_mapping_supporting_values",
                return_value=(),
            ),
            patch.object(
                context.categorical_coverage,
                "_scan_dataset",
                return_value=pl.DataFrame({identity: ["P001"], source: ["Example"]}),
            ),
        ):
            self._post_form(self._read_form(path), "draft")
            checked = context.queries.get_mapping_revision(workspace_id)
            self.assertEqual(len(checked.definition.datasets[0].relationships), 2)

            # Edit one visible field while another saved link stays off-page.
            filtered = path + "&relation_query=relation_0000"
            form = self._read_form(filtered)
            self.assertEqual(
                [
                    value
                    for name, value in form.entries
                    if name == "visible_relation_target_0"
                ],
                ["relation_0000"],
            )
            self._set_value(form, "relation_value_source_0_0", "")
            # Hidden source controls retain their old values in browser submissions.
            self._set_value(form, "relation_source_0_0", source)
            self._post_form(form, "save_progress")
            cleared = context.queries.get_mapping_working_draft(workspace_id)
            self.assertEqual(
                tuple(
                    item.target_field
                    for item in cleared.definition.datasets[0].relationships
                ),
                ("relation_0001",),
            )
            self.assertNotEqual(cleared.content_hash, working.content_hash)
            self.assertEqual(
                context.queries.get_mapping_revision(workspace_id), checked
            )

            reopened = self.client.get(filtered)
            self.assertEqual(reopened.status_code, 200, reopened.text)
            self.assertEqual(
                dict(MappingForm(reopened.text).entries)["relation_value_source_0_0"],
                "",
            )
            self.assertEqual(
                reopened.context["dataset_views"][0]["relation_mapped_total"], 1
            )
            self._post_form(MappingForm(reopened.text), "draft")
            rechecked = context.queries.get_mapping_revision(workspace_id)
            self.assertGreater(rechecked.version, checked.version)
            self.assertEqual(rechecked.definition.datasets, cleared.definition.datasets)
            self.assertEqual(
                dict(self._read_form(filtered).entries)["relation_value_source_0_0"],
                "",
            )
        self.assertEqual(
            context.queries.get_mapping_source_selection(workspace_id), source_before
        )
