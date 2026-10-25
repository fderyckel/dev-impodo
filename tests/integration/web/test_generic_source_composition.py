"""Prove exact composition is neutral to source format and Odoo model."""

from __future__ import annotations

from io import BytesIO
import re

from openpyxl import Workbook

from impodo.web.exact_join_forms import (
    available_exact_join_selection,
    exact_join_projection_candidates,
)
from impodo.web.presenters.schema import _dataset_choices
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


def _equipment_details_workbook() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Equipment details"
    worksheet.append(["Company", "AssetRef", "Description", "Location"])
    worksheet.append(["BE", "A-100", "Assembly line", "Plant 1"])
    worksheet.append(["LU", "A-200", "Inspection unit", "Plant 2"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class GenericSourceCompositionTests(ProjectSetupBrowserTestCase):
    @staticmethod
    def _column(dataset, name):
        return next(
            column for column in dataset.columns if column.source_name == name
        )

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

    def _frozen_mixed_format_workspace(self):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Mixed-format equipment composition",
                "source_mode": "FILE",
                "source_system_identity": "Fictional source system",
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
                        "asset_core.csv",
                        (
                            b"Company,AssetRef,SerialNumber\n"
                            b"BE,A-100,SN-100\n"
                            b"LU,A-200,SN-200\n"
                        ),
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                        "asset_details.xlsx",
                        _equipment_details_workbook(),
                        (
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"
                        ),
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
        self.assertEqual(
            {catalog.format for catalog in catalogs},
            {"CSV", "XLSX"},
        )
        for catalog in catalogs:
            self.assertEqual(len(catalog.tables), 1)
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
        choices = _dataset_choices(context, workspace_id)
        names = {
            index: (
                "asset_core"
                if choice["file_name"] == "asset_core.csv"
                else "asset_details"
            )
            for index, choice in enumerate(choices)
        }
        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                **{
                    f"dataset_name_{index}": name
                    for index, name in names.items()
                },
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303, frozen.text)
        selection = context.queries.get_source_selection(workspace_id)
        self.assertIsNotNone(selection)
        return workspace_id, selection, {
            dataset.name: dataset for dataset in selection.datasets
        }

    def _join_form(self, selection, by_name, *, expected_version=""):
        left = by_name["asset_core"]
        right = by_name["asset_details"]
        candidates = exact_join_projection_candidates(
            available_exact_join_selection(selection, None)
        )
        selected_names = {
            (left.dataset_id, "SerialNumber"): "serial_number",
            (right.dataset_id, "Description"): "description",
            (right.dataset_id, "Location"): "location",
        }
        fields = {
            "csrf_token": self.csrf,
            "expected_parent_version": expected_version,
            "output_dataset_name": "equipment_master",
            "left_dataset_id": left.dataset_id,
            "right_dataset_id": right.dataset_id,
            "kind": "LEFT",
            "left_dataset_use": "preparation_only",
            "right_dataset_use": "preparation_only",
            "key_left_1": (
                f'{left.dataset_id}|{self._column(left, "Company").stable_key}'
            ),
            "key_right_1": (
                f'{right.dataset_id}|{self._column(right, "Company").stable_key}'
            ),
            "key_type_1": "string",
            "key_left_2": (
                f'{left.dataset_id}|{self._column(left, "AssetRef").stable_key}'
            ),
            "key_right_2": (
                f'{right.dataset_id}|{self._column(right, "AssetRef").stable_key}'
            ),
            "key_type_2": "string",
        }
        for index in range(3, 6):
            fields[f"key_left_{index}"] = ""
            fields[f"key_right_{index}"] = ""
            fields[f"key_type_{index}"] = "string"
        fields["projection"] = [
            str(index)
            for index, (dataset, column) in enumerate(candidates)
            if (dataset.dataset_id, column.source_name) in selected_names
        ]
        fields["projection_name"] = [
            selected_names.get(
                (dataset.dataset_id, column.source_name),
                column.source_name,
            )
            for dataset, column in candidates
        ]
        return fields

    def test_csv_and_xlsx_combine_into_one_model_neutral_dataset(self) -> None:
        workspace_id, selection, by_name = self._frozen_mixed_format_workspace()
        preview = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/join/preview",
            data=self._join_form(selection, by_name),
            headers=POST_HEADERS,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("The base grain is preserved", preview.text)
        action = f"/workspaces/{workspace_id}/derived-entities/join/save"
        form = self._join_form(
            selection,
            by_name,
            expected_version=self._hidden_value(
                preview.text,
                action,
                "expected_parent_version",
            ),
        )
        form["rule_id"] = self._hidden_value(
            preview.text,
            action,
            "rule_id",
        )
        saved = self.client.post(
            action,
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303, saved.text)
        plan = self.app.state.context.queries.get_derived_entity_plan(workspace_id)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.rules), 1)
        self.assertNotIn("target_model", str(plan.rules[0].to_dict()))
        effective = self.app.state.context.queries.get_mapping_source_selection(
            workspace_id
        )
        self.assertEqual(
            [(dataset.name, dataset.row_count) for dataset in effective.datasets],
            [("equipment_master", 2)],
        )
        self.assertEqual(self.connection_calls, [])
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.read_identity_calls, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
