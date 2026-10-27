"""Authenticated browser evidence for a related split with retained input."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.derived_entities import SourceDatasetUse
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


class RelatedSplitBrowserTests(ProjectSetupBrowserTestCase):
    """Prove the generic split choice through current authenticated routes."""

    def _frozen_stock_workspace(self):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Fictional mixed stock rows",
                "source_mode": "FILE",
                "source_system_identity": "Fictional ERP",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files=[
                (
                    "source_file",
                    (
                        "stock.csv",
                        (
                            b"Item,Batch,Location,Quantity\n"
                            b"P001,LOT-001,Stock/A,5\n"
                            b"P002,LOT-002,Stock/B,3\n"
                        ),
                        "text/csv",
                    ),
                ),
            ],
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        context = self.app.state.context
        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {
                "csrf_token": self.csrf,
                "revision": str(context.queries.get(workspace_id).revision),
            },
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        catalog = context.queries.get_source_catalogs(workspace_id)[0]
        confirmed = self.client.post(
            f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
            data={
                "csrf_token": self.csrf,
                "action": "confirm",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
                "selected_0": "1",
                "warnings_acknowledged": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(confirmed.status_code, 303, confirmed.text)
        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={"csrf_token": self.csrf, "dataset_name_0": "stock_rows"},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303, frozen.text)
        selection = context.queries.get_source_selection(workspace_id)
        self.assertIsNotNone(selection)
        assert selection is not None
        return workspace_id, selection.datasets[0]

    @staticmethod
    def _column(dataset, name):
        return next(column for column in dataset.columns if column.source_name == name)

    def test_split_can_keep_original_table_for_a_separately_filtered_mapping(
        self,
    ) -> None:
        workspace_id, dataset = self._frozen_stock_workspace()
        page = self.client.get(f"/workspaces/{workspace_id}/derived-entities")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn(
            "Should Match data also show the original table?",
            page.text,
        )
        self.assertIn("No - use only the two resulting tables", page.text)

        form = {
            "csrf_token": self.csrf,
            "expected_parent_version": "",
            "source_dataset_id": dataset.dataset_id,
            "parent_dataset_name": "product_lots",
            "child_dataset_name": "lotted_stock_rows",
            "parent_key_column_key": self._column(dataset, "Batch").stable_key,
            "scope_column_key": "",
            "child_key_column_key": self._column(dataset, "Item").stable_key,
            "blank_policy": "block",
            "source_dataset_use": "map_and_prepare",
        }
        context = self.app.state.context
        original_preview = context.derived_entities.preview_related_split

        def preview_off_loop(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return original_preview(*args, **kwargs)

        with (
            patch.object(
                context.derived_entities,
                "preview_related_split",
                side_effect=preview_off_loop,
            ),
            collect_duckdb_request_timings() as preview_timings,
        ):
            preview = self.client.post(
                f"/workspaces/{workspace_id}/derived-entities/related/preview",
                data=form,
                headers=POST_HEADERS,
            )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("Review before creating", preview.text)
        self.assertIn(
            'name="source_dataset_use" value="map_and_prepare"',
            preview.text,
        )
        self.assertGreater(preview_timings.connection_count, 0)
        self.assertLessEqual(preview_timings.connection_count, 12)
        self.assertLessEqual(preview_timings.schema_check_count, 10)

        with collect_duckdb_request_timings() as save_timings:
            saved = self.client.post(
                f"/workspaces/{workspace_id}/derived-entities/related/save",
                data=form,
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertGreater(save_timings.connection_count, 0)
        self.assertLessEqual(save_timings.connection_count, 12)
        self.assertLessEqual(save_timings.schema_check_count, 10)

        plan = context.queries.get_derived_entity_plan(workspace_id)
        self.assertIsNotNone(plan)
        self.assertEqual(
            {policy.dataset_id: policy.use for policy in plan.dataset_uses}[
                dataset.dataset_id
            ],
            SourceDatasetUse.MAP_AND_PREPARE,
        )
        effective = context.queries.get_mapping_source_selection(workspace_id)
        self.assertEqual(
            {item.name for item in effective.datasets},
            {"stock_rows", "product_lots", "lotted_stock_rows"},
        )

        summary = self.client.get(saved.headers["location"])
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertIn("Also available in Match data", summary.text)
        self.assertEqual(self.connection_calls, [])
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.read_identity_calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
