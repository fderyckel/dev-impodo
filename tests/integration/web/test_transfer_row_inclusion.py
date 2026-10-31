"""Exercise authenticated transfer rules, decision review, and confirmation."""

import unittest
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from tests.integration.web import test_transfer_page_reads as fixtures


class TransferRowRuleRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TransferPageReadTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.context = self.fixture.app.state.context
        self.workspace_id = self.fixture._verified_destination_workspace()
        self.path = f"/workspaces/{self.workspace_id}/destination-matching"
        self.dataset = self.context.queries.get_source_selection(
            self.workspace_id
        ).datasets[0]

    def check(self, *, value="Alice", value_type="string", extra=None):
        state = self.context.queries.get(self.workspace_id)
        data = {
            "csrf_token": self.fixture.csrf,
            "revision": str(state.revision),
            "match_key": f"{self.dataset.dataset_id}::{self.dataset.columns[0].stable_key}",
            "destination_handling": f"{self.dataset.dataset_id}::transfer",
            "row_inclusion_mode_0": "matching_rows",
            "row_inclusion_join_0": "all",
            "row_inclusion_condition_id_0_0": str(uuid4()),
            "row_inclusion_source_0_0": self.dataset.columns[0].stable_key,
            "row_inclusion_operator_0_0": "equals",
            "row_inclusion_value_0_0": value,
            "row_inclusion_type_0_0": value_type,
            **(extra or {}),
        }
        return self.fixture._post(self.path, data)

    def confirmation_data(self, *, plan_hash=None):
        state = self.context.queries.get(self.workspace_id)
        return {
            "csrf_token": self.fixture.csrf,
            "revision": str(state.revision),
            "match_plan_hash": plan_hash or state.destination_match_plan.content_hash,
            "confirmation": "confirm",
        }

    def confirm(self, *, plan_hash=None):
        return self.fixture._post(
            self.path + "/rows-to-use/confirm",
            self.confirmation_data(plan_hash=plan_hash),
        )

    def test_checked_population_is_reviewable_and_confirmation_has_bounded_connections(
        self,
    ):
        response = self.check()
        self.assertEqual(response.status_code, 303, response.text)
        state = self.context.queries.get(self.workspace_id)
        plan = state.destination_match_plan
        self.assertFalse(plan.ready)
        self.assertEqual(plan.model_matches[0].rule_excluded_source_row_numbers, (2,))
        page = self.client.get(self.path)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Confirm 1 rows to use", page.text)
        self.assertIn("Rows checked", page.text)
        self.assertIn("mapping-row-inclusion.js", page.text)
        with collect_duckdb_request_timings() as review_timings:
            review = self.client.get(self.path + "/rows-to-use?outcome=excluded")
        self.assertEqual(review.status_code, 200, review.text)
        self.assertIn("Bob", review.text)
        self.assertNotIn("<td>Alice</td>", review.text)
        self.assertLessEqual(review_timings.connection_count, 6)
        data = self.confirmation_data()
        with collect_duckdb_request_timings() as confirmation_timings:
            response = self.fixture._post(self.path + "/rows-to-use/confirm", data)
        self.assertEqual(response.status_code, 303, response.text)
        # Two request-binding reads precede four retained operation owners:
        # registry, project, workspace engine, and source-version database.
        self.assertLessEqual(confirmation_timings.connection_count, 6)
        self.assertLessEqual(confirmation_timings.schema_check_count, 1)
        self.assertTrue(
            self.context.queries.get(self.workspace_id).destination_match_plan.ready
        )
        stale = self.confirm(plan_hash=plan.content_hash)
        self.assertEqual(stale.status_code, 422, stale.text)

    def test_rule_edit_invalidates_confirmation_and_transfer_order(self):
        self.assertEqual(self.check().status_code, 303)
        self.assertEqual(self.confirm().status_code, 303)
        state = self.context.queries.get(self.workspace_id)
        ordered = self.fixture._post(
            f"/workspaces/{self.workspace_id}/transfer-order",
            {
                "csrf_token": self.fixture.csrf,
                "revision": str(state.revision),
            },
        )
        self.assertEqual(ordered.status_code, 303, ordered.text)
        self.assertIsNotNone(
            self.context.queries.get(self.workspace_id).transfer_order_plan
        )
        self.assertEqual(self.check(value="Bob").status_code, 303)
        state = self.context.queries.get(self.workspace_id)
        self.assertFalse(state.destination_match_plan.row_inclusion_confirmed)
        self.assertIsNone(state.transfer_order_plan)
        self.assertIsNone(state.transfer_review_package)
        self.assertIsNone(state.transfer_preflight_report)

    def test_zero_or_unsafe_populations_remain_blocked(self):
        for value, value_type, blocker in (
            ("Nobody", "string", "ROW_INCLUSION_ZERO_INCLUDED"),
            ("1", "integer", "ROW_INCLUSION_CANNOT_EVALUATE"),
        ):
            with self.subTest(blocker=blocker):
                checked = self.check(value=value, value_type=value_type)
                self.assertEqual(checked.status_code, 303, checked.text)
                state = self.context.queries.get(self.workspace_id)
                self.assertIn(
                    blocker,
                    state.destination_match_plan.model_matches[0].blocking_reasons,
                )
                self.assertEqual(self.confirm().status_code, 422)

    def test_rule_form_rejects_slots_outside_the_closed_builder(self):
        response = self.check(extra={"row_inclusion_source_0_8": "untrusted"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIsNone(
            self.context.queries.get(self.workspace_id).destination_match_plan
        )


if __name__ == "__main__":
    unittest.main()
