"""Protected relationship paging preserves current bindings and database budgets."""

import json
import re
from dataclasses import replace
from html import unescape
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.relationship_review import RelationshipChoiceFilter
from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    Capability,
)
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.relationship_choice_review import relationship_choice_workspace


class RelationshipChoiceReviewBrowserTests(HierarchyWorkflowFixture):
    def setUp(self):
        super().setUp()
        self.workspace_id, self.owner, self.parent, self.check, self.reads = (
            relationship_choice_workspace(self)
        )
        self.base = f"/workspaces/{self.workspace_id}/mapping"
        self.url = (
            self.base
            + f"/relationship-choices?dataset={self.owner.dataset_id}&field=category_id&check={self.check.check_id}"
        )

    def test_paged_choices_agree_with_health_without_reading_full_evidence_or_source_keys(
        self,
    ):
        health = next(
            item
            for item in self.check.relationship_health_results
            if item.target_field == "category_id"
        )
        self.assertEqual(health.blocked_count, 111)
        repository = self.context.matching_order._preferences
        path = (
            repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with repository._connect(path) as connection:
            connection.execute(
                "UPDATE matching_order_protected_snapshot SET snapshot_json = 'not-json'"
            )
        keys = self.context.matching_order._source_keys
        with (
            patch.object(
                keys,
                "source_included_key_tuples",
                side_effect=AssertionError("No source rescan"),
            ),
            patch.object(
                keys,
                "source_identity_key_tuples",
                side_effect=AssertionError("No source rescan"),
            ),
        ):
            with collect_duckdb_request_timings() as timings:
                response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("111 choice occurrences", response.text)
        self.assertEqual(response.text.count("<tr>"), 101)
        self.assertNotIn("79000001", response.text)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        next_url = unescape(
            re.search(r'href="([^"]+)"[^>]*>Next 100', response.text).group(1)
        )
        second = self.client.get(next_url)
        self.assertIn("Showing 101–111", second.text)
        self.assertEqual(second.text.count("<tr>"), 12)
        previous = unescape(
            re.search(r'href="([^"]+)"[^>]*>Previous 100', second.text).group(1)
        )
        self.assertEqual(self.client.get(previous).text.count("<tr>"), 101)
        filtered = self.client.get(self.url + "&cause=case_only")
        self.assertIn("2 choice occurrences", filtered.text)
        self.assertIn("<code>lower</code>", filtered.text)
        self.assertIn("<code>lowerin</code>", filtered.text)
        self.assertNotIn("<code>LU</code>", self.client.get(self.url).text)
        self.assertEqual(len(self.reads), 1)

    def test_actions_list_members_and_authorization_remain_separate(self):
        mapping = self.client.get(self.base)
        self.assertIn("Review unresolved choices", mapping.text)
        self.assertIn("Change matching rule", mapping.text)
        tags = self.client.get(self.url.replace("field=category_id", "field=tag_ids"))
        self.assertIn("3 choice occurrences", tags.text)
        self.assertIn("<code>TAGMISS</code>", tags.text)
        self.assertNotIn("Required value is blank</strong>", tags.text)
        self.assertNotIn("79000050", tags.text)
        service = self.context.matching_order
        actor = Actor(
            ActorIdentity("test", "mapping-only", "Mapping editor"),
            frozenset({Capability.MAPPING_EDIT}),
        )
        with patch.object(service._preferences, "get_current_check") as read:
            with self.assertRaises(AuthorizationError):
                service.relationship_choice_review_page(
                    self.workspace_id,
                    None,
                    None,
                    None,
                    None,
                    RelationshipChoiceFilter(),
                    page_size=100,
                    actor=actor,
                )
            read.assert_not_called()
        self.assertEqual(len(self.reads), 1)
        self.assertIsNone(
            self.context.execution.journal.get_current_run(self.workspace_id)
        )

    def test_stale_check_receipts_bindings_and_older_checks_require_rechecking(self):
        for suffix in ("&check=old-check", "&after=0&before=4", "&after=-1"):
            response = self.client.get(self.url + suffix, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        service = self.context.matching_order
        with patch.object(
            service._preferences,
            "get_current_check",
            return_value=replace(self.check, relationship_review_version=0),
        ):
            self.assertEqual(
                self.client.get(self.url, follow_redirects=False).status_code, 303
            )
        draft = self.context.queries.get_mapping_working_draft(self.workspace_id)
        self.context.mapping_workspace.save_working_draft(
            self.workspace_id,
            datasets=draft.definition.datasets,
            expected_version=draft.version,
            actor=self.actor,
        )
        self.assertEqual(
            self.client.get(self.url, follow_redirects=False).status_code, 303
        )

    def test_failed_index_publication_keeps_the_previous_check_current(self):
        repository = self.context.matching_order._preferences
        path = (
            repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with repository._connect(path) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT snapshot_json FROM matching_order_protected_snapshot WHERE check_id = ?",
                    [self.check.check_id],
                ).fetchone()[0]
            )
        from uuid import uuid4
        from datetime import UTC, datetime
        from impodo.domain.matching_order import (
            MatchingOrderCheckAttempt,
            MatchingOrderCheckPhase,
            MatchingOrderCheckStatus,
        )
        from impodo.domain.workspace.errors import WorkspaceError
        from impodo.adapters.duckdb import matching_order_repository as module

        check = replace(self.check, check_id=str(uuid4()))
        now = datetime.now(UTC)
        repository.begin_check(
            self.workspace_id,
            MatchingOrderCheckAttempt(
                check.check_id,
                self.workspace_id,
                MatchingOrderCheckStatus.QUEUED,
                MatchingOrderCheckPhase.QUEUED,
                "Waiting to check Odoo",
                0,
                now,
                now,
            ),
            actor=self.actor,
        )
        invalid = {
            **payload,
            "relationship_choices": payload["relationship_choices"][:-1],
        }
        with self.assertRaises(WorkspaceError):
            repository.publish_check(
                self.workspace_id,
                check,
                protected_snapshot_json=json.dumps(invalid),
                actor=self.actor,
            )
        insert = module.insert_relationship_choices

        def interrupted(*args):
            insert(*args)
            raise WorkspaceError("injected after relationship index insertion")

        with patch.object(
            module, "insert_relationship_choices", side_effect=interrupted
        ):
            with self.assertRaises(WorkspaceError):
                repository.publish_check(
                    self.workspace_id,
                    check,
                    protected_snapshot_json=json.dumps(payload),
                    actor=self.actor,
                )
        self.assertEqual(repository.get_current_check(self.workspace_id), self.check)
        with repository._connect(path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM matching_order_protected_relationship_choice WHERE check_id = ?",
                    [check.check_id],
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM matching_order_check WHERE check_id = ?",
                    [check.check_id],
                ).fetchone(),
                (0,),
            )
