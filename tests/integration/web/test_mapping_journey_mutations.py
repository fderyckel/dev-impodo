"""Keep guided navigation separate from saved and confirmed matching evidence."""

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements


class MappingJourneyMutationTests(ProjectSetupBrowserTestCase):
    def assert_checked_page_opens_offline(self, url, step, maximum_statements):
        connector_calls = (
            self.connection_calls, self.schema_calls, self.model_catalog_calls,
            self.read_identity_calls, self.readiness_calls, self.local_odoo_reader.mock_calls,
        )
        before = tuple(len(calls) for calls in connector_calls)
        for visit in ("open", "reopen"):
            with self.subTest(visit=visit, step=step):
                with count_duckdb_statements() as sql, collect_duckdb_request_timings() as timing:
                    page = self.client.get(url, follow_redirects=False)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertEqual(page.context["mapping_journey"]["step"], step)
                self.assertLessEqual(timing.connection_count, 4)
                self.assertLessEqual(timing.schema_check_count, 3)
                self.assertLessEqual(len(sql), maximum_statements)
                self.assertEqual(timing.lock_retry_count, 0)
                self.assertEqual(tuple(len(calls) for calls in connector_calls), before)

    def test_save_check_and_confirmation_keep_distinct_evidence_for_both_majors(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self.exercise_mutations(major, mode)

    def exercise_mutations(self, major, mode):
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=1, odoo_major=major, connection_mode=mode,
        )
        context = self.app.state.context
        sources = context.queries.get_source_selection(workspace_id)
        path = f"/workspaces/{workspace_id}/mapping"
        entries = [
            ["csrf_token", self.csrf],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", dataset.columns[0].stable_key],
            ["business_key_0", key.key_id],
            ["identity_source_0_0", dataset.columns[0].stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "constant"],
            ["scalar_literal_0_1", "Reviewed value"],
        ]

        def command(action, working="", revision="", step="tables", task="fields", operation_id=None):
            return self.client.post(
                f"{path}/save?step={step}&task={task}",
                json={"entries": [
                    *entries,
                    ["action", action],
                    ["operation_id", operation_id or str(uuid4())],
                    ["expected_working_draft_version", working],
                    ["expected_parent_version", revision],
                ]},
                headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            )

        saved = command("save_progress")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "committed")
        self.assertEqual(
            parse_qs(urlsplit(saved.json()["redirect_url"]).query)["task"],
            ["fields"],
        )
        draft = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(draft.definition.datasets[0].fields[0].literal_value, "Reviewed value")
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))

        operation_id = str(uuid4())
        checked = command("draft", str(draft.version), step="check", operation_id=operation_id)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "committed")
        destination = f"{path}?step=confirm#confirm-matches"
        self.assertEqual(checked.json()["redirect_url"], destination)
        for recovered in (
            self.client.get(f"{path}/mutation-receipts/{operation_id}"),
            command("draft", str(draft.version), step="check", operation_id=operation_id),
        ):
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["redirect_url"], destination, recovered.text)
            self.assertEqual(recovered.json()["expected_parent_version"], checked.json()["expected_parent_version"])
        revision = context.queries.get_mapping_revision(workspace_id)
        self.assertIsNone(context.queries.get_mapping_submission(workspace_id, revision.version))

        self.assert_checked_page_opens_offline(
            checked.json()["redirect_url"], "confirm", maximum_statements=126
        )
        self.assertIsNone(context.queries.get_mapping_submission(workspace_id, revision.version))
        confirmed = command(
            "submit", str(checked.json()["expected_working_draft_version"]),
            str(revision.version), step="confirm",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["redirect_url"], f"/workspaces/{workspace_id}/prepare")
        self.assertIsNotNone(context.queries.get_mapping_submission(workspace_id, revision.version))
        self.assertEqual(context.queries.get_source_selection(workspace_id), sources)
        superseded = self.client.get(f"{path}/mutation-receipts/{operation_id}").json()
        self.assertEqual(superseded["status"], "committed")
        self.assertEqual(superseded["follow_up_status"], "superseded")
        self.assertEqual(superseded["redirect_url"], f"{path}?step=check")

    def test_invalid_check_returns_to_issue_queue_without_confirming(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, dataset, key = self._mapping_ready_workspace(
                    scalar_field_count=1, required_scalar_indexes=(0,), odoo_major=major,
                )
                path = f"/workspaces/{workspace_id}/mapping"
                operation_id = str(uuid4())
                form = {
                    "csrf_token": self.csrf,
                    "action": "draft",
                    "operation_id": operation_id,
                    "editable_dataset_id": dataset.dataset_id,
                    "target_model_0": "res.partner",
                    "mode_0": "upsert",
                    "source_identity_0": dataset.columns[0].stable_key,
                    "business_key_0": key.key_id,
                    "identity_source_0_0": dataset.columns[0].stable_key,
                }
                response = self._post(f"{path}/save?step=tables&task=identity", form)
                self.assertEqual(response.status_code, 303, response.text)
                destination = urlsplit(response.headers["location"])
                self.assertEqual(parse_qs(destination.query)["step"], ["check"])
                self.assertEqual(destination.fragment, "next-step-blockers")
                recovered = self.client.get(f"{path}/mutation-receipts/{operation_id}")
                self.assertEqual(recovered.json()["redirect_url"], response.headers["location"], recovered.text)
                replay = self._post(f"{path}/save", form)
                self.assertEqual(replay.status_code, 303, replay.text)
                self.assertEqual(replay.headers["location"], response.headers["location"])
                self.assert_checked_page_opens_offline(
                    response.headers["location"], "check", maximum_statements=122
                )
                revision = self.app.state.context.queries.get_mapping_revision(workspace_id)
                self.assertIsNone(
                    self.app.state.context.queries.get_mapping_submission(workspace_id, revision.version)
                )
                draft = self.app.state.context.queries.get_mapping_working_draft(workspace_id)
                saved = self.client.post(
                    f"/workspaces/{workspace_id}/mapping/save?step=tables&task=links",
                    json={"entries": [
                        ["csrf_token", self.csrf], ["action", "save_progress"],
                        ["operation_id", str(uuid4())],
                        ["expected_parent_version", str(revision.version)],
                        ["expected_working_draft_version", str(draft.version)],
                        ["editable_dataset_id", dataset.dataset_id],
                        ["target_model_0", "res.partner"], ["mode_0", "upsert"],
                        ["source_identity_0", dataset.columns[0].stable_key],
                        ["business_key_0", key.key_id],
                        ["identity_source_0_0", dataset.columns[0].stable_key],
                    ]},
                    headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                )
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json()["table_progress"][dataset.dataset_id], {
                    "status_label": "Needs attention", "status_class": "review",
                })
