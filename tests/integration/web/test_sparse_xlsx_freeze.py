"""Freeze sparse Excel exports without losing original source row identities."""

from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from impodo.application.data_version.source_snapshots import load_source_snapshot_table
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


class SparseXlsxFreezeTests(ProjectSetupBrowserTestCase):
    def test_freeze_publishes_sparse_rows_and_skips_trailing_empty_rows(self) -> None:
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Customers"
        worksheet.append(["Code", "Name"])
        worksheet.append(["C1", "First"])
        worksheet["B5"] = "No code"
        worksheet["A8"] = "C2"
        worksheet["B8"] = "Second"
        worksheet["B12"].fill = PatternFill(fill_type="solid", fgColor="FFFF00")
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Sparse Excel export",
                "source_mode": "FILE",
                "source_system_identity": "Fictional ERP",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files={
                "source_file": (
                    "customers.xlsx",
                    content.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)
        context = self.app.state.context
        workspace = context.queries.get(workspace_id)
        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {"csrf_token": self.csrf, "revision": str(workspace.revision)},
        )
        self.assertEqual(registered.status_code, 303)
        catalog = context.queries.get_source_catalogs(workspace_id)[0]
        self.assertEqual(catalog.tables[0].row_count, 3)
        confirmed = self._post(
            f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
            {
                "csrf_token": self.csrf,
                "action": "confirm",
                "header_row_0": "1",
                "selected_0": "1",
            },
        )
        self.assertEqual(confirmed.status_code, 303)

        frozen = self._post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            {"csrf_token": self.csrf, "dataset_name_0": "customers"},
        )

        self.assertEqual(frozen.status_code, 303, frozen.text)
        selection = context.queries.get_source_selection(workspace_id)
        self.assertEqual(selection.datasets[0].row_count, 3)
        snapshots = context.sources.sources.get_current_source_snapshots(workspace_id)
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots[0]
        with context.sources.artifacts.materialize_source_snapshot(
            selection.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            table = load_source_snapshot_table(path, snapshot)
        self.assertEqual(
            [(row.number, row.values) for row in table.rows],
            [
                (2, {"Code": "C1", "Name": "First"}),
                (5, {"Code": None, "Name": "No code"}),
                (8, {"Code": "C2", "Name": "Second"}),
            ],
        )
