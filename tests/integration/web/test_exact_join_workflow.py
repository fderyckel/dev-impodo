"""Authenticated browser evidence for exact source-table composition."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
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


class ExactJoinBrowserTests(ProjectSetupBrowserTestCase):
    """Prove the fictional customer workflow through current HTTP routes."""

    def _frozen_customer_workspace(self):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Fictional customer composition",
                "source_mode": "FILE",
                "source_system_identity": "Fictional AX",
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
                        "cust02.csv",
                        (
                            b"Partition,Party,AccountNum,Company\n"
                            b"part-1,100,C001,United Caps\n"
                            b"part-1,200,C002,Fictional Retail\n"
                        ),
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    (
                        "cust01.csv",
                        (
                            b"Partition,RecId,Name,Email\n"
                            b"part-1,100,United Caps,info@example.test\n"
                            b"part-1,200,Fictional Retail,buyer@example.test\n"
                            b"part-1,999,Unused Party,unused@example.test\n"
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
        self.assertEqual(len(catalogs), 2)
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
        dataset_names = {
            index: (
                "customer_accounts"
                if catalog.display_name == "cust02.csv"
                else "party_details"
            )
            for index, catalog in enumerate(catalogs)
        }
        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                **{
                    f"dataset_name_{index}": name
                    for index, name in dataset_names.items()
                },
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303, frozen.text)
        selection = context.queries.get_source_selection(workspace_id)
        self.assertIsNotNone(selection)
        by_name = {dataset.name: dataset for dataset in selection.datasets}
        self.assertEqual(set(by_name), {"customer_accounts", "party_details"})
        return workspace_id, selection, by_name

    @staticmethod
    def _column(dataset, name):
        return next(column for column in dataset.columns if column.source_name == name)

    def _join_form(self, selection, by_name, *, expected_version=""):
        left = by_name["customer_accounts"]
        right = by_name["party_details"]
        available = available_exact_join_selection(selection, None)
        candidates = exact_join_projection_candidates(available)
        selected_names = {
            (left.dataset_id, "AccountNum"): "account_number",
            (left.dataset_id, "Company"): "company",
            (right.dataset_id, "Name"): "name",
            (right.dataset_id, "Email"): "email",
        }
        fields = {
            "csrf_token": self.csrf,
            "expected_parent_version": expected_version,
            "output_dataset_name": "customer_contacts",
            "left_dataset_id": left.dataset_id,
            "right_dataset_id": right.dataset_id,
            "kind": "LEFT",
            "left_dataset_use": "preparation_only",
            "right_dataset_use": "preparation_only",
            "key_left_1": (
                f'{left.dataset_id}|{self._column(left, "Partition").stable_key}'
            ),
            "key_right_1": (
                f'{right.dataset_id}|{self._column(right, "Partition").stable_key}'
            ),
            "key_type_1": "string",
            "key_left_2": (
                f'{left.dataset_id}|{self._column(left, "Party").stable_key}'
            ),
            "key_right_2": (
                f'{right.dataset_id}|{self._column(right, "RecId").stable_key}'
            ),
            "key_type_2": "integer",
        }
        for index in range(3, 6):
            fields[f"key_left_{index}"] = ""
            fields[f"key_right_{index}"] = ""
            fields[f"key_type_{index}"] = "string"
        projection_indexes = []
        projection_names = []
        for index, (dataset, column) in enumerate(candidates):
            output_name = selected_names.get(
                (dataset.dataset_id, column.source_name),
                column.source_name,
            )
            if (dataset.dataset_id, column.source_name) in selected_names:
                projection_indexes.append(str(index))
            projection_names.append(output_name)
        fields["projection"] = projection_indexes
        fields["projection_name"] = projection_names
        return fields

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

    def test_customer_tables_preview_save_and_leave_only_the_logical_dataset_to_map(
        self,
    ) -> None:
        workspace_id, selection, by_name = self._frozen_customer_workspace()
        page = self.client.get(f"/workspaces/{workspace_id}/derived-entities")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Combine columns from multiple tables", page.text)
        self.assertIn('data-exact-join-form', page.text)
        self.assertIn('name="key_left_5"', page.text)
        self.assertIn('name="projection_name"', page.text)

        original_preview = self.app.state.context.derived_entities.preview_join

        def preview_off_loop(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return original_preview(*args, **kwargs)

        with (
            patch.object(
                self.app.state.context.derived_entities,
                "preview_join",
                side_effect=preview_off_loop,
            ),
            collect_duckdb_request_timings() as preview_timings,
        ):
            preview = self.client.post(
                f"/workspaces/{workspace_id}/derived-entities/join/preview",
                data=self._join_form(selection, by_name),
                headers=POST_HEADERS,
            )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("Complete-data review", preview.text)
        self.assertIn("The base grain is preserved", preview.text)
        self.assertIn("customer_contacts", preview.text)
        self.assertIn("Preparation only", preview.text)
        self.assertIn("Unused added rows", preview.text)
        self.assertIn(">1</strong>", preview.text)
        self.assertGreater(preview_timings.connection_count, 0)
        self.assertLessEqual(preview_timings.connection_count, 12)
        # The command owns the project, Data version, workspace, source package,
        # selection, plan, and artifact stores inside one retained operation.
        self.assertLessEqual(preview_timings.schema_check_count, 10)

        save_action = f"/workspaces/{workspace_id}/derived-entities/join/save"
        rule_id = self._hidden_value(preview.text, save_action, "rule_id")
        expected_version = self._hidden_value(
            preview.text,
            save_action,
            "expected_parent_version",
        )
        save_form = self._join_form(
            selection,
            by_name,
            expected_version=expected_version,
        )
        save_form["rule_id"] = rule_id

        with collect_duckdb_request_timings() as save_timings:
            saved = self.client.post(
                save_action,
                data=save_form,
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertIn(f"#join-rule-{rule_id}", saved.headers["location"])
        self.assertGreater(save_timings.connection_count, 0)
        self.assertLessEqual(save_timings.connection_count, 12)
        self.assertLessEqual(save_timings.schema_check_count, 10)

        context = self.app.state.context
        plan = context.queries.get_derived_entity_plan(workspace_id)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.join_reviews), 1)
        self.assertEqual(
            {policy.use for policy in plan.dataset_uses},
            {SourceDatasetUse.PREPARATION_ONLY},
        )
        effective = context.queries.get_mapping_source_selection(workspace_id)
        self.assertEqual(
            [(dataset.name, dataset.row_count) for dataset in effective.datasets],
            [("customer_contacts", 2)],
        )

        summary = self.client.get(saved.headers["location"])
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertIn("Combined table prepared", summary.text)
        self.assertIn("Preparation only - hidden from Match data", summary.text)
        self.assertIn("Mapped logical dataset", summary.text)
        self.assertIn("Shown in Match data", summary.text)
        self.assertEqual(self.connection_calls, [])
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.read_identity_calls, [])

    def test_invalid_or_stale_join_does_not_replace_saved_evidence(self) -> None:
        workspace_id, selection, by_name = self._frozen_customer_workspace()
        left = by_name["customer_accounts"]
        invalid = self._join_form(selection, by_name)
        invalid["right_dataset_id"] = left.dataset_id
        rejected = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/join/preview",
            data=invalid,
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("Choose two different source datasets", rejected.text)
        self.assertIsNone(
            self.app.state.context.queries.get_derived_entity_plan(workspace_id)
        )

        preview = self.client.post(
            f"/workspaces/{workspace_id}/derived-entities/join/preview",
            data=self._join_form(selection, by_name),
            headers=POST_HEADERS,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        action = f"/workspaces/{workspace_id}/derived-entities/join/save"
        stale = self._join_form(selection, by_name, expected_version="99")
        stale["rule_id"] = self._hidden_value(preview.text, action, "rule_id")
        rejected_save = self.client.post(
            action,
            data=stale,
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected_save.status_code, 422, rejected_save.text)
        self.assertIn("modified by another request", rejected_save.text)
        self.assertIsNone(
            self.app.state.context.queries.get_derived_entity_plan(workspace_id)
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
