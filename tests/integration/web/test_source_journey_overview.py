"""Show the accepted tables and the next file decision in Stage 1."""

from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


class SourceJourneyOverviewTests(ProjectSetupBrowserTestCase):
    def test_confirmation_exposes_the_next_file_then_table_naming(self):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Two source files",
                "source_mode": "FILE",
                "source_system_identity": "Fictional source",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files=[
                (
                    "source_file",
                    ("customers.csv", b"code,name\nC1,Example\n", "text/csv"),
                ),
                (
                    "source_file",
                    ("addresses.csv", b"code,city\nC1,Brussels\n", "text/csv"),
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
        for index, catalog in enumerate(catalogs):
            confirmed = self._post(
                f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
                {
                    "csrf_token": self.csrf,
                    "action": "confirm",
                    "encoding": "utf-8",
                    "delimiter": ",",
                    "header_row_0": "1",
                    "selected_0": "1",
                },
            )
            self.assertEqual(confirmed.status_code, 303, confirmed.text)
            page = self.client.get(confirmed.headers["location"])
            if index == 0:
                self.assertIn("Confirm next file", page.text)
                self.assertIn(f'href="#source-{catalogs[1].file_id}"', page.text)
                self.assertNotIn("Name and save tables", page.text)
            else:
                self.assertNotIn("Confirm next file", page.text)
                self.assertIn("Name and save tables", page.text)

        frozen = self._post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            {
                "csrf_token": self.csrf,
                "dataset_name_0": "customers",
                "dataset_name_1": "addresses",
            },
        )
        self.assertEqual(frozen.status_code, 303, frozen.text)
        selection = context.queries.get_source_selection(workspace_id)
        self.assertIsNotNone(selection)
        overview = self.client.get(frozen.headers["location"])
        self.assertEqual(overview.status_code, 200, overview.text)
        table = overview.text.split("data-saved-source-tables", 1)[1].split(
            "</table>", 1
        )[0]
        for dataset in selection.datasets:
            self.assertIn(f"<th>{dataset.name}</th>", table)
            self.assertIn(f"<td>{dataset.row_count}</td>", table)
            self.assertIn(f"<td>{len(dataset.columns)}</td>", table)
        self.assertIn("Organize source tables", overview.text)
        self.assertIn(
            "Combine tables, create a related table, or build a hierarchy.",
            overview.text,
        )
        self.assertEqual(
            context.queries.get_source_selection(workspace_id),
            selection,
            "Viewing the overview must not save or replace the accepted source.",
        )
