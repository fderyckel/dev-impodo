"""Protected group pages reuse the publication index and retain business authority."""

import re
from dataclasses import replace
from html import unescape
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.matching_order import MatchingIdentityChoiceFilter
from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    Capability,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.identity_group_review import duplicate_identity_workspace


class IdentityGroupReviewBrowserTests(HierarchyWorkflowFixture):
    def setUp(self):
        super().setUp()
        self.workspace_id, self.dataset, self.check, self.reads = (
            duplicate_identity_workspace(self)
        )
        self.base = f"/workspaces/{self.workspace_id}/mapping"
        self.url = (
            self.base
            + f"/identity-choices?kind=duplicates&check={self.check.check_id}&cause=repeated_source"
        )

    def test_large_group_pages_are_bounded_current_and_do_not_decode_the_snapshot(self):
        repository = self.context.matching_order._preferences
        database = (
            repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with repository._connect(database) as connection:
            # The normalized index must be sufficient even if this blob cannot be decoded.
            connection.execute(
                "UPDATE matching_order_protected_snapshot SET snapshot_json = 'not-json'"
            )
        with patch.object(
            self.context.matching_order._source_keys,
            "source_identity_key_tuples",
            side_effect=AssertionError("No source scan"),
        ):
            with collect_duckdb_request_timings() as timings:
                response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("of 105 group members", response.text)
        self.assertEqual(response.text.count("<td>Included source position"), 100)
        self.assertNotIn("92100001", response.text)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 1)
        self.assertEqual(timings.lock_retry_count, 0)
        next_url = unescape(
            re.search(r'href="([^"]+)"[^>]*>Next 100', response.text).group(1)
        )
        second = self.client.get(next_url)
        self.assertIn("Included source position 105", second.text)
        self.assertEqual(second.text.count("<td>Included source position"), 5)
        previous = unescape(
            re.search(r'href="([^"]+)"[^>]*>Previous 100', second.text).group(1)
        )
        self.assertEqual(
            self.client.get(previous).text.count("<td>Included source position"), 100
        )
        group_url = unescape(
            re.search(r'href="([^"]+)"[^>]*>Open complete group', response.text).group(
                1
            )
        )
        self.assertIn("of 105 group members", self.client.get(group_url).text)
        self.assertEqual(len(self.reads), 1)
        self.assertIsNone(
            self.context.execution.journal.get_current_run(self.workspace_id)
        )

    def test_observed_ambiguity_keeps_compound_scopes_and_ids_protected(self):
        page = self.client.get(self.base)
        self.assertIn("Review repeated source identities", page.text)
        self.assertIn("Review ambiguous Odoo matches", page.text)
        response = self.client.get(
            self.url.replace("repeated_source", "ambiguous_identity_target")
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("2 groups", response.text)
        self.assertIn("not a complete Odoo inventory", response.text)
        self.assertNotIn("<code>LU</code>", response.text)
        self.assertNotIn("92100001", self.check.to_json())
        self.assertNotIn('"DUP"', self.check.to_json())
        self.assertEqual(self.check.identity_results[0].source_repeated_row_count, 105)
        self.assertEqual(self.check.identity_results[0].target_ambiguous_key_count, 2)
        self.assertEqual(len(self.reads), 1)

    def test_access_denial_precedes_evidence_and_stale_bindings_reject_the_page(self):
        selection = self.context.queries.get_mapping_source_selection(self.workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(self.workspace_id)
        governance = self.context.queries.get_schema_governance(self.workspace_id)
        draft = self.context.queries.get_mapping_working_draft(self.workspace_id)
        service = self.context.matching_order
        actor = Actor(
            ActorIdentity("test", "mapping-only", "Mapping editor"),
            frozenset({Capability.MAPPING_EDIT}),
        )
        with patch.object(service._preferences, "get_current_check") as read:
            with self.assertRaises(AuthorizationError):
                service.identity_choice_review_page(
                    self.workspace_id,
                    selection,
                    schema,
                    governance,
                    draft,
                    MatchingIdentityChoiceFilter(kind="duplicates"),
                    page_size=100,
                    actor=actor,
                )
            read.assert_not_called()
        with self.assertRaises(WorkspaceError):
            service.identity_choice_review_page(
                self.workspace_id,
                selection,
                schema,
                governance,
                draft,
                MatchingIdentityChoiceFilter(kind="duplicates"),
                page_size=100,
                expected_check_id="old-check",
                actor=self.actor,
            )
        self.context.mapping_workspace.save_working_draft(
            self.workspace_id,
            datasets=draft.definition.datasets,
            expected_version=draft.version,
            actor=self.actor,
        )
        response = self.client.get(self.url, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_old_check_requires_a_new_explicit_publication(self):
        service = self.context.matching_order
        current = service._preferences.get_current_check
        with patch.object(
            service._preferences,
            "get_current_check",
            return_value=replace(self.check, identity_review_version=0),
        ):
            response = self.client.get(self.url, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(current(self.workspace_id).identity_review_version, 1)
