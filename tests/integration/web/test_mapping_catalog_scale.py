"""Large mapping-catalog browser evidence."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    POST_HEADERS,
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ProjectSetupBrowserTestCase,
    ScalarFieldMapping,
    ScalarValueSource,
)


class LargeMappingCatalogBrowserTests(ProjectSetupBrowserTestCase):
    def test_large_mapping_catalog_is_paged_and_saved_sparsely(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1500
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        initial = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=tuple(
                        ScalarFieldMapping(
                            target_field=f"field_{index:04d}",
                            source_column_key=source_value.stable_key,
                            value_source=ScalarValueSource.SOURCE,
                        )
                        for index in range(1500)
                    ),
                ),
            ),
            expected_version=None,
            actor=context.actor,
        )
        self.assertEqual(initial.version, 1)

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("data-scalar-mapping-row"), 3)
        self.assertIn("Showing 3 of 1500 fields", page.text)
        self.assertIn("Page 1 of 500", page.text)
        self.assertIn("field_0000", page.text)
        self.assertNotIn("field_0003</code>", page.text)
        self.assertIn("template data-source-column-options", page.text)
        self.assertLess(page.text.count(source_value.stable_key), 100)

        last_page = self.client.get(
            f"/workspaces/{workspace_id}/mapping?scalar_page=500"
        )
        self.assertEqual(last_page.text.count("data-scalar-mapping-row"), 3)
        self.assertIn("field_1499", last_page.text)
        self.assertIn("scalar_page=500", last_page.text)

        expanded = self.client.get(
            f"/workspaces/{workspace_id}/mapping?scalar_page_size=50"
        )
        self.assertEqual(expanded.text.count("data-scalar-mapping-row"), 50)
        self.assertIn("field_0049", expanded.text)
        self.assertNotIn("field_0050</code>", expanded.text)
        self.assertIn('data-scalar-page-size="50"', expanded.text)

        rejected_size = self.client.get(
            f"/workspaces/{workspace_id}/mapping?scalar_page_size=100"
        )
        self.assertEqual(
            rejected_size.text.count("data-scalar-mapping-row"),
            3,
        )
        self.assertIn('data-scalar-page-size="3"', rejected_size.text)

        searched = self.client.get(
            f"/workspaces/{workspace_id}/mapping?field_query=field_1499"
        )
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.text.count("data-scalar-mapping-row"), 1)
        self.assertIn("field_1499", searched.text)
        self.assertNotIn("field_0000</code>", searched.text)

        fragment = self.client.get(
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            "?field_query=field_1499"
        )
        self.assertEqual(fragment.status_code, 200)
        self.assertEqual(fragment.text.count("data-scalar-mapping-row"), 1)
        self.assertIn("field_1499", fragment.text)
        self.assertNotIn("data-relation-field-catalog", fragment.text)
        self.assertNotIn("<main", fragment.text)
        self.assertEqual(fragment.headers["cache-control"], "no-store")
        self.assertIn("projection;dur=", fragment.headers["server-timing"])

        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", "1"],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "constant"],
            ["scalar_literal_0_1", "Updated safely"],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
        ]
        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn("redirect_url", saved.json())
        self.assertEqual(saved.json()["expected_working_draft_version"], 2)
        self.assertIn("saved_at", saved.json())
        self.assertEqual(
            saved.json()["redirect_url"],
            f"/workspaces/{workspace_id}/mapping#mapping-dataset-0",
        )
        saved_again_entries = [list(entry) for entry in entries]
        for entry in saved_again_entries:
            if entry[0] == "expected_working_draft_version":
                entry[1] = "2"
            elif entry[0] == "scalar_literal_0_1":
                entry[1] = "Updated safely again"
        saved_again = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": saved_again_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(saved_again.status_code, 200)
        self.assertEqual(
            saved_again.json()["expected_working_draft_version"],
            3,
        )
        working = context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
        self.assertEqual(working.version, 3)
        self.assertEqual(len(working.definition.datasets[0].fields), 1500)
        updated = {
            item.target_field: item
            for item in working.definition.datasets[0].fields
        }
        self.assertEqual(
            updated["field_0000"].literal_value,
            "Updated safely again",
        )
        self.assertEqual(
            updated["field_1499"].source_column_key,
            source_value.stable_key,
        )

        denied = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": "incorrect",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id).version,
            3,
        )

        invalid_entries = [
            list(item)
            for item in entries
            if item[0] not in {"source_identity_0", "identity_source_0_0"}
        ]
        for item in invalid_entries:
            if item[0] == "action":
                item[1] = "submit"
            elif item[0] == "expected_working_draft_version":
                item[1] = "3"
        invalid = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": invalid_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["expected_working_draft_version"], 3)
        self.assertIsNone(invalid.json()["expected_parent_version"])
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            ).version,
            3,
        )
        self.assertIsNone(
            context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )

        retry_entries = [list(item) for item in entries]
        for item in retry_entries:
            if item[0] == "expected_working_draft_version":
                item[1] = "3"
            elif item[0] == "expected_parent_version":
                item[1] = ""
        retried = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": retry_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id).version,
            4,
        )

        oversized = b'{"entries":[["csrf_token","' + (
            b"x" * (5 * 1024 * 1024)
        ) + b'"]]}'
        rejected = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            content=oversized,
            headers={
                **POST_HEADERS,
                "Content-Type": "application/json",
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(rejected.status_code, 413)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id).version,
            4,
        )

        excessive_form = "&".join(
            [f"field_{index}=x" for index in range(25_001)]
        )
        recovered = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            content=excessive_form,
            headers={
                **POST_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        self.assertEqual(recovered.status_code, 303)
        self.assertIn("save_error=request_rejected", recovered.headers["location"])
        recovery_page = self.client.get(recovered.headers["location"])
        self.assertIn("No mapping change was saved", recovery_page.text)
