"""Resolve saved checks against their original validation, actor and Odoo access."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.mutations import MappingMutationAction
from impodo.domain.mapping.row_inclusion_review import (
    RowInclusionDatasetReview,
    RowInclusionReviewIdentity,
    RowInclusionReviewReport,
)
from impodo.domain.mapping.validation.evidence import (
    MappingValidationIssue,
    MappingValidationStatus,
)
from impodo.web.composition.local_identity import LocalRequestIdentityResolver
from impodo.web.routers import mapping as mapping_routes
from tests.integration.web.test_mapping_command_performance import MappingCommandFixture
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class MappingCheckReceiptTests(MappingCommandFixture):
    def test_receipt_requires_original_actor_request_and_odoo_access(self):
        workspace_id, entries = self.command_fixture(20, "REMOTE")
        context = self.app.state.context
        actor = resolve_local_actor(context)
        checked_entries = self.changed_entries(
            entries, action="draft", operation_id=str(uuid4())
        )
        checked = self.post_command(workspace_id, checked_entries)
        self.assertEqual(checked.status_code, 200, checked.text)
        path = f"/workspaces/{workspace_id}/mapping"
        receipt_url = (
            f"{path}/mutation-receipts/{dict(checked_entries)['operation_id']}"
        )
        self.assertEqual(
            checked.json()["redirect_url"], f"{path}?step=confirm#confirm-matches"
        )
        with (
            count_duckdb_statements() as sql,
            collect_duckdb_request_timings() as timing,
        ):
            lookup = self.client.get(receipt_url)
        self.assertEqual(
            lookup.json()["redirect_url"], checked.json()["redirect_url"], lookup.text
        )
        self.assertLessEqual(timing.connection_count, 6)
        self.assertLessEqual(timing.schema_check_count, 4)
        self.assertLessEqual(len(sql), 59)
        self.assertEqual(timing.lock_retry_count, 0)

        different = self.changed_entries(checked_entries, on_existing_0="update")
        with patch.object(mapping_routes, "prepare_matching_access") as prepare:
            reused = self.post_command(workspace_id, different)
        prepare.assert_not_called()
        self.assertEqual(reused.status_code, 422, reused.text)
        self.assertIn("already used for another request", reused.json()["detail"])
        other_actor = replace(
            actor, identity=replace(actor.identity, subject_id="another-editor")
        )
        with patch.object(
            context.identity_resolver,
            "resolve",
            LocalRequestIdentityResolver(other_actor).resolve,
        ):
            hidden = self.client.get(receipt_url)
        self.assertEqual(hidden.json()["status"], "not_found", hidden.text)
        self.assertNotIn("content_identity", hidden.json())

        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        rebound = replace(schema, read_credential_binding_hash="sha256:" + "e" * 64)
        context.schema_workspace.schemas.rebind_odoo_schema_access(
            workspace_id,
            rebound,
            actor=actor,
            expected_content_hash=schema.content_hash,
            expected_read_credential_binding_hash=schema.read_credential_binding_hash,
        )
        self.assertEqual(schema.content_hash, rebound.content_hash)
        with patch.object(mapping_routes, "prepare_matching_access") as prepare:
            replayed = self.post_command(workspace_id, checked_entries)
        prepare.assert_not_called()
        for response in (self.client.get(receipt_url), replayed):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "committed")
            self.assertEqual(response.json()["follow_up_status"], "superseded")
            self.assertEqual(
                response.json()["recovery"],
                {"reload_url": f"{path}?step=check", "copy_edits": True},
            )

        pending_id = str(uuid4())
        context.mapping_workspace.begin_mutation(
            workspace_id,
            operation_id=pending_id,
            action=MappingMutationAction.CHECK_MATCHES,
            request_hash="0" * 64,
            submitted_working_draft_version=None,
            submitted_mapping_revision_version=None,
            actor=actor,
        )
        with patch.object(
            context.mapping_workspace,
            "current_check_inputs",
            side_effect=AssertionError("Read pending evidence"),
        ):
            pending = self.client.get(f"{path}/mutation-receipts/{pending_id}")
        self.assertEqual(pending.json()["status"], "pending", pending.text)

    def test_only_current_check_of_same_revision_recovers_its_own_validation(self):
        workspace_id, entries = self.command_fixture(19, "LOCAL")
        context = self.app.state.context
        path = f"/workspaces/{workspace_id}/mapping"
        first = self.post_command(
            workspace_id, self.changed_entries(entries, action="draft")
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_payload = first.json()
        second_entries = self.changed_entries(
            entries,
            action="draft",
            operation_id=str(uuid4()),
            expected_parent_version=str(first_payload["expected_parent_version"]),
            expected_working_draft_version=str(
                first_payload["expected_working_draft_version"]
            ),
        )
        validate = context.mapping_workspace._validate_definition

        def warn(*args):
            result = validate(*args)
            issue = MappingValidationIssue(
                "CHECK_REVIEW",
                "warning",
                "/datasets/0",
                "Review this match.",
                "Review the warning.",
            )
            return replace(
                result,
                status=MappingValidationStatus.VALID_WITH_WARNINGS,
                issues=(*result.issues, issue),
            )

        with patch.object(
            context.mapping_workspace, "_validate_definition", side_effect=warn
        ):
            second = self.post_command(workspace_id, second_entries)
        self.assertEqual(second.status_code, 200, second.text)
        second_payload = second.json()
        self.assertEqual(
            second_payload["content_identity"], first_payload["content_identity"]
        )
        self.assertEqual(
            second_payload["expected_parent_version"],
            first_payload["expected_parent_version"],
        )
        with patch.object(
            context.mapping_workspace,
            "_validate_definition",
            side_effect=AssertionError("Rechecked on recovery"),
        ):
            old = self.client.get(
                f"{path}/mutation-receipts/{first_payload['operation_id']}"
            )
            current = self.client.get(
                f"{path}/mutation-receipts/{second_payload['operation_id']}"
            )
            replay = self.post_command(workspace_id, second_entries)
        self.assertEqual(old.json()["follow_up_status"], "superseded", old.text)
        self.assertEqual(
            current.json()["redirect_url"],
            f"{path}?step=confirm#confirm-matches",
            current.text,
        )
        self.assertEqual(
            replay.json()["redirect_url"], current.json()["redirect_url"], replay.text
        )
        validation = context.queries.get_mapping_validation(
            workspace_id, second_payload["expected_parent_version"]
        )
        self.assertEqual(validation.status, MappingValidationStatus.VALID_WITH_WARNINGS)
        self.assertIsNone(
            context.queries.get_mapping_submission(
                workspace_id, second_payload["expected_parent_version"]
            )
        )

        # The first immutable validation already exists. A new check can select
        # it again without copying it or accidentally leaving the warning current.
        third_entries = self.changed_entries(second_entries, operation_id=str(uuid4()))
        third = self.post_command(workspace_id, third_entries)
        self.assertEqual(third.status_code, 200, third.text)
        current_validation = context.queries.get_mapping_validation(
            workspace_id, third.json()["expected_parent_version"]
        )
        self.assertEqual(current_validation.status, MappingValidationStatus.VALID)
        page = self.client.get(third.json()["redirect_url"])
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn("Review this match.", page.text)
        for previous in (first_payload, second_payload):
            old = self.client.get(f"{path}/mutation-receipts/{previous['operation_id']}")
            self.assertEqual(old.json()["follow_up_status"], "superseded")
        current = self.client.get(f"{path}/mutation-receipts/{third.json()['operation_id']}")
        self.assertEqual(current.json()["redirect_url"], third.json()["redirect_url"])

    def test_new_check_during_row_evaluation_cannot_publish_the_old_snapshot(self):
        workspace_id, entries = self.command_fixture(20, "REMOTE")
        context = self.app.state.context
        dataset = context.queries.get_mapping_source_selection(workspace_id).datasets[0]
        path = f"/workspaces/{workspace_id}/mapping"
        older_id, newer_id = str(uuid4()), str(uuid4())
        filtered = self.changed_entries(entries, action="draft", operation_id=older_id)
        filtered.extend([
            ["row_inclusion_mode_0", "matching_rows"],
            ["row_inclusion_join_0", "all"],
            ["row_inclusion_condition_id_0_0", str(uuid4())],
            ["row_inclusion_source_0_0", dataset.columns[1].stable_key],
            ["row_inclusion_operator_0_0", "equals"],
            ["row_inclusion_value_0_0", "Example"],
            ["row_inclusion_type_0_0", "string"],
        ])

        def finish_newer_check(_workspace, definition, physical, effective, *_args, **_kwargs):
            draft = context.queries.get_mapping_working_draft(workspace_id)
            revision = context.queries.get_mapping_revision(workspace_id)
            replacement = self.changed_entries(
                entries, action="draft", operation_id=newer_id,
                expected_working_draft_version=str(draft.version),
                expected_parent_version=str(revision.version),
            )
            replacement.append(["row_inclusion_mode_0", "all_rows"])
            newer = self.post_command(workspace_id, replacement)
            self.assertEqual(newer.status_code, 200, newer.text)
            self.assertEqual(newer.json()["redirect_url"], f"{path}?step=confirm#confirm-matches")
            return SimpleNamespace(row_inclusion_review=RowInclusionReviewReport(
                RowInclusionReviewIdentity(
                    physical.content_hash, effective.content_hash, definition.content_hash,
                    definition.schema_hash, None,
                ),
                (RowInclusionDatasetReview(dataset.dataset_id, dataset.name, 0, 0, 0, 0, "No matching rows."),),
                (),
            ))

        with patch(
            "impodo.application.workspace.mapping.row_inclusion_review.stage_browser_mapping",
            side_effect=finish_newer_check,
        ):
            older = self.post_command(workspace_id, filtered)
        self.assertEqual(older.status_code, 422, older.text)
        self.assertEqual(older.json()["failure_code"], "MAPPING_ROW_REVIEW_FAILED")
        self.assertTrue(older.json()["partial_save"])
        self.assertEqual(self.client.get(f"{path}/mutation-receipts/{older_id}").json()["status"], "rejected")
        self.assertEqual(self.client.get(f"{path}/mutation-receipts/{newer_id}").json()["redirect_url"],
                         f"{path}?step=confirm#confirm-matches")
