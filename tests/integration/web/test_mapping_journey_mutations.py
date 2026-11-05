"""Keep guided navigation separate from saved and confirmed matching evidence."""

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase


class MappingJourneyMutationTests(ProjectSetupBrowserTestCase):
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

        def command(action, working="", revision="", step="tables", task="fields"):
            return self.client.post(
                f"{path}/save?step={step}&task={task}",
                json={"entries": [
                    *entries,
                    ["action", action],
                    ["operation_id", str(uuid4())],
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

        checked = command("draft", str(draft.version), step="check")
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "committed")
        self.assertEqual(
            parse_qs(urlsplit(checked.json()["redirect_url"]).query)["step"],
            ["check"],
        )
        revision = context.queries.get_mapping_revision(workspace_id)
        self.assertIsNone(context.queries.get_mapping_submission(workspace_id, revision.version))

        viewed = self.client.get(f"{path}?step=confirm")
        self.assertEqual(viewed.status_code, 200, viewed.text)
        self.assertIsNone(context.queries.get_mapping_submission(workspace_id, revision.version))
        confirmed = command(
            "submit", str(checked.json()["expected_working_draft_version"]),
            str(revision.version), step="confirm",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["redirect_url"], f"/workspaces/{workspace_id}/prepare")
        self.assertIsNotNone(context.queries.get_mapping_submission(workspace_id, revision.version))
        self.assertEqual(context.queries.get_source_selection(workspace_id), sources)

    def test_invalid_check_returns_to_issue_queue_without_confirming(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace_id, dataset, key = self._mapping_ready_workspace(
                    scalar_field_count=1, required_scalar_indexes=(0,), odoo_major=major,
                )
                response = self._post(
                    f"/workspaces/{workspace_id}/mapping/save?step=tables&task=identity",
                    {
                        "csrf_token": self.csrf,
                        "action": "draft",
                        "editable_dataset_id": dataset.dataset_id,
                        "target_model_0": "res.partner",
                        "mode_0": "upsert",
                        "source_identity_0": dataset.columns[0].stable_key,
                        "business_key_0": key.key_id,
                        "identity_source_0_0": dataset.columns[0].stable_key,
                    },
                )
                self.assertEqual(response.status_code, 303, response.text)
                destination = urlsplit(response.headers["location"])
                self.assertEqual(parse_qs(destination.query)["step"], ["check"])
                self.assertEqual(destination.fragment, "next-step-blockers")
                revision = self.app.state.context.queries.get_mapping_revision(workspace_id)
                self.assertIsNone(
                    self.app.state.context.queries.get_mapping_submission(workspace_id, revision.version)
                )
