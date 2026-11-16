"""Celebrate an accepted matching decision once without changing preparation."""

from __future__ import annotations

from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class MatchCelebrationTests(ProjectSetupBrowserTestCase):
    def _checked_matches(self, major=19, mode=OdooConnectionMode.LOCAL):
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=0, odoo_major=major, connection_mode=mode,
        )
        identity = dataset.columns[0].stable_key
        context = self.app.state.context
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id,
            datasets=(DatasetMapping(
                dataset_id=dataset.dataset_id,
                target_model="res.partner",
                mode=MappingTargetMode.UPSERT,
                source_identity_column_keys=(identity,),
                target_identity=(IdentityComponentMapping(
                    source_column_keys=(identity,), target_fields=key.key_fields,
                ),),
                fields=(),
            ),),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=resolve_local_actor(context),
        )
        self.assertNotEqual(validation.status.value, "INVALID")
        working = context.queries.get_mapping_working_draft(workspace_id)
        form = {
            "csrf_token": self.csrf,
            "action": "submit",
            "operation_id": str(uuid4()),
            "expected_parent_version": str(revision.version),
            "expected_working_draft_version": str(working.version),
            "editable_dataset_id": dataset.dataset_id,
            "target_model_0": "res.partner",
            "mode_0": "upsert",
            "on_existing_0": "block",
            "source_identity_0": identity,
            "business_key_0": key.key_id,
            "identity_source_0_0": identity,
        }
        return workspace_id, form

    def test_confirmation_celebrates_once_in_both_versions_and_access_modes(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for json_request in (False, True):
                    with self.subTest(major=major, mode=mode, json=json_request):
                        workspace_id, form = self._checked_matches(major, mode)
                        prepare_url = f"/workspaces/{workspace_id}/prepare"
                        ordinary = self.client.get(prepare_url)
                        self.assertNotIn("data-match-celebration", ordinary.text)
                        arguments = (
                            {"json": {"entries": list(form.items())},
                             "headers": {**POST_HEADERS, "X-CSRF-Token": self.csrf}}
                            if json_request else {"data": form, "headers": POST_HEADERS}
                        )
                        confirmed = self.client.post(
                            f"/workspaces/{workspace_id}/mapping/save",
                            follow_redirects=False,
                            **arguments,
                        )
                        self.assertEqual(confirmed.status_code, 200 if json_request else 303, confirmed.text)
                        destination = (
                            confirmed.json()["redirect_url"] if json_request
                            else confirmed.headers["location"]
                        )
                        self.assertEqual(destination, prepare_url)
                        first = self.client.get(destination)
                        self.assertEqual(first.status_code, 200, first.text)
                        self.assertIn("data-match-celebration", first.text)
                        self.assertIn("Good Job Hugo!", first.text)
                        self.assertIn("/match-celebration.js", first.text)
                        self.assertIn("/match-celebration.css", first.text)
                        self.assertIn('action="' + prepare_url.removesuffix("prepare") + 'summary/check"', first.text)
                        repeat = self.client.get(destination)
                        self.assertNotIn("data-match-celebration", repeat.text)
                        self.assertNotIn("/match-celebration.js", repeat.text)
                        self.assertIn("Ready to prepare", repeat.text)
                        if json_request:
                            replayed = self.client.post(
                                f"/workspaces/{workspace_id}/mapping/save",
                                follow_redirects=False,
                                **arguments,
                            )
                            self.assertEqual(replayed.status_code, 200)
                            replay_page = self.client.get(prepare_url)
                            self.assertNotIn("data-match-celebration", replay_page.text)

    def test_rejected_confirmation_does_not_celebrate(self):
        workspace_id, form = self._checked_matches()
        rejected = self._post(f"/workspaces/{workspace_id}/mapping/save", {
            **form, "expected_parent_version": "999",
        })
        self.assertEqual(rejected.status_code, 303)
        self.assertIn("/mapping", rejected.headers["location"])
        page = self.client.get(f"/workspaces/{workspace_id}/prepare")
        self.assertNotIn("data-match-celebration", page.text)
        self.assertIn("Finish matching first", page.text)

    def test_another_workspace_cannot_consume_the_celebration(self):
        workspace_id, form = self._checked_matches()
        other_id, _ = self._checked_matches()
        confirmed = self._post(f"/workspaces/{workspace_id}/mapping/save", form)
        self.assertEqual(confirmed.status_code, 303)
        other = self.client.get(f"/workspaces/{other_id}/prepare")
        self.assertNotIn("data-match-celebration", other.text)
        first = self.client.get(confirmed.headers["location"])
        self.assertIn("data-match-celebration", first.text)

    def test_celebration_adds_no_database_work(self):
        workspace_id, form = self._checked_matches()
        with count_duckdb_statements() as sql, collect_duckdb_request_timings() as timing:
            confirmed = self._post(f"/workspaces/{workspace_id}/mapping/save", form)
        self.assertEqual(confirmed.status_code, 303)
        # The existing confirmation scopes measured 14 owners, 11 schema
        # checks and 118 statements with this exact local authoring fixture.
        self.assertLessEqual(timing.connection_count, 14)
        self.assertLessEqual(timing.schema_check_count, 11)
        self.assertLessEqual(len(sql), 118)
        self.assertEqual(timing.lock_retry_count, 0)
        for expected in (True, False):
            with count_duckdb_statements() as sql, collect_duckdb_request_timings() as timing:
                page = self.client.get(confirmed.headers["location"])
            self.assertEqual(page.status_code, 200)
            self.assertEqual("data-match-celebration" in page.text, expected)
            # Preparation admission and its existing page projection measured
            # eight owners and six store validations before this UI change.
            self.assertLessEqual(timing.connection_count, 8)
            self.assertLessEqual(timing.schema_check_count, 6)
            self.assertLessEqual(len(sql), 153)
            self.assertEqual(timing.lock_retry_count, 0)
