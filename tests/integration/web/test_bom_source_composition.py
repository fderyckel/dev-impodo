"""Authenticated BoM-family qualification for exact source composition."""

from __future__ import annotations

import re

from impodo.domain.workspace.derived_entities import SourceDatasetUse
from impodo.web.exact_join_forms import (
    available_exact_join_selection,
    exact_join_projection_candidates,
)
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


class BomSourceCompositionBrowserTests(ProjectSetupBrowserTestCase):
    @staticmethod
    def _column(dataset, name):
        return next(column for column in dataset.columns if column.source_name == name)

    @staticmethod
    def _hidden_value(page: str, action: str, name: str) -> str:
        form = re.search(
            rf'<form method="post" action="{re.escape(action)}">(.*?)</form>',
            page,
            re.DOTALL,
        )
        if form is None:
            raise AssertionError(f"Form {action} was not rendered")
        value = re.search(
            rf'name="{re.escape(name)}" value="([^"]*)"',
            form.group(1),
        )
        if value is None:
            raise AssertionError(f"Field {name} was not rendered")
        return value.group(1)

    def _frozen_bom_workspace(self):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Fictional BoM composition",
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
                        "bom_header_core.csv",
                        (
                            b"Company,Bom,Product\n"
                            b"BE,B-100,P-100\n"
                            b"LU,B-100,P-200\n"
                        ),
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                        "bom_header_details.csv",
                        (
                            b"Company,Bom,Description\n"
                            b"BE,B-100,Belgian cap\n"
                            b"LU,B-100,Luxembourg cap\n"
                        ),
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                        "bom_line_core.csv",
                        (
                            b"Company,Bom,Line,Component,Quantity\n"
                            b"BE,B-100,10,C-10,2.0\n"
                            b"BE,B-100,20,C-20,1.0\n"
                            b"LU,B-100,10,C-30,4.0\n"
                        ),
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                        "bom_line_details.csv",
                        (
                            b"Company,Bom,Line,ScrapPercent\n"
                            b"BE,B-100,10,1.5\n"
                            b"BE,B-100,20,0.0\n"
                            b"LU,B-100,10,2.0\n"
                        ),
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                        "bom_operations.csv",
                        (
                            b"Company,Bom,Operation,WorkCenter,Duration\n"
                            b"BE,B-100,10,WC-10,1.0\n"
                            b"BE,B-100,20,WC-20,2.0\n"
                            b"LU,B-100,10,WC-10,1.5\n"
                            b"LU,B-100,20,WC-30,2.5\n"
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
        catalogs = context.queries.get_source_catalogs(workspace_id)
        self.assertEqual(len(catalogs), 5)
        for catalog in catalogs:
            confirmed = self.client.post(
                f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
                data={
                    "csrf_token": self.csrf,
                    "action": "confirm",
                    "encoding": "utf-8",
                    "delimiter": ",",
                    "header_row_0": "1",
                    "selected_0": "1",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(confirmed.status_code, 303, confirmed.text)
        names = {
            "bom_header_core.csv": "bom_header_core",
            "bom_header_details.csv": "bom_header_details",
            "bom_line_core.csv": "bom_line_core",
            "bom_line_details.csv": "bom_line_details",
            "bom_operations.csv": "bom_operations",
        }
        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                **{
                    f"dataset_name_{index}": names[catalog.display_name]
                    for index, catalog in enumerate(catalogs)
                },
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303, frozen.text)
        selection = context.queries.get_source_selection(workspace_id)
        self.assertIsNotNone(selection)
        return workspace_id, selection

    def _join_form(
        self,
        *,
        selection,
        plan,
        left_name: str,
        right_name: str,
        output_name: str,
        keys: tuple[tuple[str, str, str], ...],
        output_fields: tuple[tuple[str, str, str], ...],
        expected_version: str = "",
    ):
        available = available_exact_join_selection(selection, plan)
        by_name = {dataset.name: dataset for dataset in available.datasets}
        left = by_name[left_name]
        right = by_name[right_name]
        candidates = exact_join_projection_candidates(available)
        selected = {
            (by_name[dataset_name].dataset_id, source_name): result_name
            for dataset_name, source_name, result_name in output_fields
        }
        fields = {
            "csrf_token": self.csrf,
            "expected_parent_version": expected_version,
            "output_dataset_name": output_name,
            "left_dataset_id": left.dataset_id,
            "right_dataset_id": right.dataset_id,
            "kind": "INNER",
            "require_all_right_rows": "1",
            "left_dataset_use": "preparation_only",
            "right_dataset_use": "preparation_only",
        }
        for index in range(1, 6):
            if index <= len(keys):
                left_field, right_field, value_type = keys[index - 1]
                fields[f"key_left_{index}"] = (
                    f"{left.dataset_id}|{self._column(left, left_field).stable_key}"
                )
                fields[f"key_right_{index}"] = (
                    f"{right.dataset_id}|{self._column(right, right_field).stable_key}"
                )
                fields[f"key_type_{index}"] = value_type
            else:
                fields[f"key_left_{index}"] = ""
                fields[f"key_right_{index}"] = ""
                fields[f"key_type_{index}"] = "string"
        projection_indexes = []
        projection_names = []
        for index, (dataset, column) in enumerate(candidates):
            selected_name = selected.get((dataset.dataset_id, column.source_name))
            if selected_name is not None:
                projection_indexes.append(str(index))
            projection_names.append(selected_name or column.source_name)
        fields["projection"] = projection_indexes
        fields["projection_name"] = projection_names
        return fields

    def _save_join(self, workspace_id, selection, plan, **join):
        action = f"/workspaces/{workspace_id}/derived-entities/join/save"
        preview = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/join/preview",
            data=self._join_form(selection=selection, plan=plan, **join),
            headers=POST_HEADERS,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("The base grain is preserved", preview.text)
        form = self._join_form(
            selection=selection,
            plan=plan,
            expected_version=self._hidden_value(
                preview.text,
                action,
                "expected_parent_version",
            ),
            **join,
        )
        form["rule_id"] = self._hidden_value(preview.text, action, "rule_id")
        saved = self.client.post(
            action,
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303, saved.text)
        return self.app.state.context.queries.get_derived_entity_plan(workspace_id)

    def test_header_lines_and_operations_keep_separate_browser_grains(self) -> None:
        workspace_id, selection = self._frozen_bom_workspace()
        plan = self._save_join(
            workspace_id,
            selection,
            None,
            left_name="bom_header_core",
            right_name="bom_header_details",
            output_name="bom_headers",
            keys=(("Company", "Company", "string"), ("Bom", "Bom", "string")),
            output_fields=(
                ("bom_header_core", "Company", "Company"),
                ("bom_header_core", "Bom", "Bom"),
                ("bom_header_core", "Product", "Product"),
                ("bom_header_details", "Description", "Description"),
            ),
        )
        self.assertIsNotNone(plan)
        plan = self._save_join(
            workspace_id,
            selection,
            plan,
            left_name="bom_line_core",
            right_name="bom_line_details",
            output_name="bom_lines",
            keys=(
                ("Company", "Company", "string"),
                ("Bom", "Bom", "string"),
                ("Line", "Line", "integer"),
            ),
            output_fields=(
                ("bom_line_core", "Company", "Company"),
                ("bom_line_core", "Bom", "Bom"),
                ("bom_line_core", "Line", "Line"),
                ("bom_line_core", "Component", "Component"),
                ("bom_line_core", "Quantity", "Quantity"),
                ("bom_line_details", "ScrapPercent", "Scrap percent"),
            ),
        )

        self.assertEqual(len(plan.rules), 2)
        self.assertEqual(len(plan.join_reviews), 2)
        uses_by_name = {
            dataset.name: policy.use
            for dataset in selection.datasets
            for policy in plan.dataset_uses
            if policy.dataset_id == dataset.dataset_id
        }
        self.assertEqual(
            uses_by_name,
            {
                "bom_header_core": SourceDatasetUse.PREPARATION_ONLY,
                "bom_header_details": SourceDatasetUse.PREPARATION_ONLY,
                "bom_line_core": SourceDatasetUse.PREPARATION_ONLY,
                "bom_line_details": SourceDatasetUse.PREPARATION_ONLY,
                "bom_operations": SourceDatasetUse.MAP_DIRECTLY,
            },
        )
        effective = self.app.state.context.queries.get_mapping_source_selection(
            workspace_id
        )
        self.assertEqual(
            {dataset.name: dataset.row_count for dataset in effective.datasets},
            {"bom_headers": 2, "bom_lines": 3, "bom_operations": 4},
        )
        page = self.client.get(f"/workspaces/{workspace_id}/derived-entities")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("bom_headers", page.text)
        self.assertIn("bom_lines", page.text)
        self.assertEqual(self.connection_calls, [])
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.read_identity_calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
