"""Browser evidence for file-source review, correction, and acceptance."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.project.foundation import MigrationFoundationError
from tests.support.browser_scenarios import (
    POST_HEADERS,
    BytesIO,
    ProjectSetupBrowserTestCase,
    SourceSelection,
    _created_workspace_id,
    _workspace_data_version_id,
    datetime,
    timezone,
    uuid4,
)
from tests.support.identity import resolve_local_actor


class FileSourceWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_source_files_can_change_only_before_table_choices_are_saved(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Correctable source files",
            source_system="CSV",
        )
        kept = context.intake.accept(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            display_name="customers.csv",
            stream=BytesIO(b"code,name\nC1,Kept\n"),
        )
        current = context.queries.get(workspace_state.workspace_id)
        wrong = context.intake.accept(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=current.revision,
            display_name="wrong.csv",
            stream=BytesIO(b"code,name\nBAD,Wrong\n"),
        )
        current = context.queries.get(workspace_state.workspace_id)
        files_page = self.client.get(
            f"/workspaces/{workspace_state.workspace_id}/files"
        )
        self.assertEqual(
            files_page.text.count("data-source-file-remove-form"),
            2,
        )
        self.assertIn("data-source-file-remove-dialog", files_page.text)

        wrong_path = (
            context.workspace_states.repository.workspace_directory(
                workspace_state.workspace_id
            )
            / "inbox"
            / wrong.stored_name
        )
        removed_draft = self._post(
            f"/workspaces/{workspace_state.workspace_id}/files/{wrong.file_id}/remove",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "return_to": "files",
            },
        )
        self.assertEqual(removed_draft.status_code, 303)
        self.assertFalse(wrong_path.exists())
        current = context.queries.get(workspace_state.workspace_id)
        registered = context.workspace_states.register(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=current.revision,
        )

        get_workspace = context.queries.get

        def read_workspace_in_worker(workspace_id):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return get_workspace(workspace_id)

        with (
            patch.object(
                type(context.queries), "get", side_effect=read_workspace_in_worker
            ),
            collect_duckdb_request_timings() as timings,
        ):
            source_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
        self.assertEqual(source_page.status_code, 200)
        self.assertLessEqual(timings.connection_count, 8)
        self.assertEqual(
            source_page.text.count("data-source-file-remove-form"),
            1,
        )
        self.assertIn(
            f'action="/workspaces/{workspace_state.workspace_id}/sources/files"',
            source_page.text,
        )
        replacement_upload = self.client.post(
            f"/workspaces/{workspace_state.workspace_id}/sources/files",
            data={
                "csrf_token": self.csrf,
                "revision": str(registered.revision),
            },
            files={
                "source_file": (
                    "corrected.csv",
                    b"code,name\nC2,Corrected\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(replacement_upload.status_code, 303)
        current = context.queries.get(workspace_state.workspace_id)
        corrected = next(
            item
            for item in current.source_files
            if item.display_name == "corrected.csv"
        )
        removed_registered = self._post(
            f"/workspaces/{workspace_state.workspace_id}/files/{corrected.file_id}/remove",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "return_to": "sources",
            },
        )
        self.assertEqual(removed_registered.status_code, 303)
        self.assertEqual(
            removed_registered.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources#source-files",
        )
        removed_page = self.client.get(removed_registered.headers["location"])
        self.assertIn(
            "Removed corrected.csv from this Data version.",
            removed_page.text,
        )
        datasets_page = self.client.get(
            f"/workspaces/{workspace_state.workspace_id}/datasets"
        )
        self.assertEqual(
            datasets_page.text.count("data-source-file-remove-form"),
            1,
        )

        current = context.queries.get(workspace_state.workspace_id)
        now = datetime.now(timezone.utc)
        context.sources.sources.save_source_selection(
            workspace_state.workspace_id,
            SourceSelection(
                selection_id=str(uuid4()),
                version=1,
                data_version_id=_workspace_data_version_id(
                    context,
                    workspace_state.workspace_id,
                ),
                created_at=now,
                created_by=resolve_local_actor(context).identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=resolve_local_actor(context),
        )
        blocked = self._post(
            f"/workspaces/{workspace_state.workspace_id}/files/{kept.file_id}/remove",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "return_to": "datasets",
            },
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn(
            "Source files cannot be changed after table choices are saved",
            blocked.text,
        )
        self.assertNotIn("data-source-file-remove-form", blocked.text)

    def test_source_review_saves_table_choices_without_an_intermediate_page(
        self,
    ) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Inline source confirmation",
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
                    "customers.csv",
                    b"code,name\nC001,Example\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)
        context = self.app.state.context
        workspace_state = context.queries.get(workspace_id)
        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
            },
        )
        self.assertEqual(registered.status_code, 303)
        self.assertEqual(
            registered.headers["location"],
            f"/workspaces/{workspace_id}/sources#source-files",
        )
        inspection_page = self.client.get(registered.headers["location"])
        self.assertIn("Checked 1 source file.", inspection_page.text)
        self.assertIn("customers.csv", inspection_page.text)
        self.assertIn("Check files again", inspection_page.text)
        self.assertNotIn("Your files have not been checked yet", inspection_page.text)
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
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(confirmed.status_code, 303)
        source_page = self.client.get(confirmed.headers["location"])
        self.assertIn("Save the tables for this data version", source_page.text)
        self.assertIn('name="dataset_name_0"', source_page.text)
        self.assertIn("Use 1 to 63 characters", source_page.text)
        self.assertIn("Start with a lowercase letter", source_page.text)
        self.assertIn("Give each table a different name", source_page.text)
        self.assertIn("data-dataset-name", source_page.text)
        self.assertIn(
            f'action="/workspaces/{workspace_id}/datasets/freeze"',
            source_page.text,
        )

        unfinished_page = self.client.get(
            f"/workspaces/{workspace_id}/datasets",
            follow_redirects=False,
        )
        self.assertEqual(unfinished_page.status_code, 303)
        self.assertEqual(
            unfinished_page.headers["location"],
            f"/workspaces/{workspace_id}/sources#table-choices",
        )

        invalid_name = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "Product-withUoM_v1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(invalid_name.status_code, 422)
        self.assertIn("Start with a lowercase letter from a to z", invalid_name.text)
        self.assertIn(
            "Use only lowercase letters, numbers, and underscores",
            invalid_name.text,
        )

        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "customers",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303)
        saved_page = self.client.get(frozen.headers["location"])
        self.assertIn("Saved source tables", saved_page.text)
        self.assertIn("Tables ready for the next step", saved_page.text)
        self.assertNotIn('name="dataset_name_0"', saved_page.text)

        saved_selection = context.queries.get_source_selection(workspace_id)
        saved_catalogs = context.queries.get_source_catalogs(workspace_id)
        self.assertIsNotNone(saved_selection)
        with patch.object(
            context.sources,
            "freeze_selection",
            side_effect=AssertionError("duplicate request refroze the source"),
        ):
            repeated_freeze = self.client.post(
                f"/workspaces/{workspace_id}/datasets/freeze",
                data={
                    "csrf_token": self.csrf,
                    "dataset_name_0": "customers",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(repeated_freeze.status_code, 303)
        self.assertEqual(
            context.queries.get_source_selection(workspace_id),
            saved_selection,
        )

        changed_freeze = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "renamed_customers",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(changed_freeze.status_code, 422)
        self.assertIn("already frozen", changed_freeze.text)
        self.assertEqual(
            context.queries.get_source_selection(workspace_id),
            saved_selection,
        )
        source_page = self.client.get(f"/workspaces/{workspace_id}/sources")
        self.assertEqual(source_page.status_code, 200)
        self.assertNotIn("Check files again", source_page.text)
        self.assertIn('class="source-review-fields" disabled', source_page.text)

        with patch(
            "impodo.application.data_version.source_worker.inspect_source_file_isolated",
            side_effect=AssertionError("Saved files must not be reinspected"),
        ):
            rejected = self._post(
                f"/workspaces/{workspace_id}/sources/inspect",
                {"csrf_token": self.csrf},
            )
        self.assertEqual(rejected.status_code, 422)
        self.assertIn(
            "The tables for this Data version are already saved", rejected.text
        )
        self.assertEqual(
            context.queries.get_source_selection(workspace_id), saved_selection
        )

        rejected_preview = self._post(
            f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
            {
                "csrf_token": self.csrf,
                "action": "preview",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
            },
        )
        self.assertEqual(rejected_preview.status_code, 422)
        self.assertIn(
            "The tables for this Data version are already saved", rejected_preview.text
        )
        self.assertEqual(
            context.queries.get_source_selection(workspace_id), saved_selection
        )

        with self.assertRaisesRegex(
            MigrationFoundationError,
            "Accepted DataVersion source evidence is immutable",
        ):
            context.sources.sources.save_source_catalogs(
                workspace_id,
                saved_catalogs,
                actor=resolve_local_actor(context),
            )
        self.assertEqual(
            context.queries.get_source_selection(workspace_id), saved_selection
        )
        self.assertEqual(
            context.queries.get_source_catalogs(workspace_id), saved_catalogs
        )

        hierarchy_page = self.client.get(f"/workspaces/{workspace_id}/derived-entities")
        self.assertEqual(hierarchy_page.status_code, 200)
        self.assertIn(
            "Build a hierarchy from separate fields",
            hierarchy_page.text,
        )
        self.assertIn("Several fields form a hierarchy", hierarchy_page.text)
        self.assertIn(
            "models/refresh?return_to=hierarchy",
            hierarchy_page.text,
        )
        self.assertIn("/static/derived-entities.js", hierarchy_page.text)
        for section_id in (
            "lookup-extraction",
            "hierarchy-extraction",
            "create-related-datasets",
        ):
            with self.subTest(section_id=section_id):
                self.assertIn(
                    f'id="{section_id}" data-derived-entity-section>',
                    hierarchy_page.text,
                )
                self.assertIn(
                    f'type="button" data-derived-entity-target="{section_id}"',
                    hierarchy_page.text,
                )
        derived_script = self.client.get("/static/derived-entities.js")
        self.assertEqual(derived_script.status_code, 200)
        self.assertIn("parent.open = true", derived_script.text)
        self.assertIn("revealCurrentTarget", derived_script.text)
        self.assertIn('window.addEventListener("pageshow"', derived_script.text)
