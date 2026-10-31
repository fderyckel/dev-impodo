"""Final review edits and bounded expectations views use real persisted evidence."""

from dataclasses import replace
from html import unescape
import re
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.shared.access import AuthorizationError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_execution import compared_hierarchy
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


def editor_form(html):
    form_html = re.search(r'<form method="post" action="[^"]+/summary/expectations"[^>]*>(.*?)</form>', html, re.S).group(1)
    result = {name: unescape(value) for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form_html)}
    for index in range(int(result["model_count"])):
        result[f"mode_{index}"] = "upsert"
        for phase in ("create", "update"):
            result[f"{phase}_min_{index}"] = ""
            result[f"{phase}_max_{index}"] = ""
    return result


class BusinessExpectationsBrowserTests(HierarchyWorkflowFixture):
    def setUp(self):
        super().setUp()
        self.workspace_id, _, (self.target, self.snapshot, self.identities) = compared_hierarchy(self, mode="LOCAL")
        self.base = f"/workspaces/{self.workspace_id}"

    def editor(self):
        response = self.client.get(self.base + "/summary/expectations")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def save(self, form):
        return self.client.post(self.base + "/summary/expectations", data=form, headers=POST_HEADERS, follow_redirects=False)

    def review(self):
        return self.context.preflight.business_intent_review(self.workspace_id, self.snapshot.semantic_hash, actor=self.actor)

    def test_summary_and_navigation_use_published_counts_without_decoding_rows(self):
        with patch.object(self.context.preflight, "current_execution_snapshot", side_effect=AssertionError("No row snapshot on a normal page")), patch.object(self.context.preflight.preflight, "get_business_write_observation", side_effect=AssertionError("No effect reconstruction")):
            with collect_duckdb_request_timings() as timings:
                response = self.client.get(self.base + "/summary")
            review = self.client.get(self.base + "/load/review")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Expected versus found", response.text)
        self.assertIn("No count expectation", response.text)
        self.assertIn("Within expectations", review.text)
        self.assertLessEqual(timings.connection_count, 7)
        self.assertLessEqual(timings.schema_check_count, 2)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(self.target.writes, [])

    def test_edit_reassesses_saved_observation_and_deviation_has_correction_link(self):
        page = self.editor()
        form = editor_form(page.text)
        index = next(index for index in range(int(form["model_count"])) if form[f"model_{index}"] == "product.template")
        form[f"create_max_{index}"] = "0"
        remote = len(self.target.requests)
        with collect_duckdb_request_timings() as timings:
            response = self.save(form)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(len(self.target.requests), remote)
        # Route authorization opens two stores before the command scope;
        # saving and reassessing retain four distinct database owners.
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 2)
        self.assertEqual(timings.lock_retry_count, 0)
        review = self.review()
        self.assertFalse(review.ready)
        summary = self.client.get(self.base + "/summary")
        self.assertIn("At most 0", summary.text)
        self.assertIn("#expect-product.template", summary.text)
        navigation = self.context.navigation.get_for_workspace(self.workspace_id, actor=self.actor)
        self.assertFalse(navigation.facts.execution_preview.can_load)
        self.assertEqual(self.target.writes, [])

    def test_stale_edit_cannot_implicitly_adopt_latest_version(self):
        form = editor_form(self.editor().text)
        self.expect_business_writes(self.workspace_id)
        response = self.save(form)
        self.assertEqual(response.status_code, 422, response.text)
        again = editor_form(response.text)
        self.assertEqual(again["expected_version"], form["expected_version"])
        self.assertEqual(again["binding_hash"], form["binding_hash"])
        self.assertEqual(self.save(again).status_code, 422)

    def test_repeated_submission_returns_same_revision_and_csrf_rejects(self):
        form = editor_form(self.editor().text)
        self.assertEqual(self.save(form).status_code, 303)
        first = self.context.business_intents.get(self.workspace_id, actor=self.actor)
        self.assertEqual(self.save(form).status_code, 303)
        self.assertEqual(self.context.business_intents.get(self.workspace_id, actor=self.actor), first)
        form["csrf_token"] = "wrong"
        self.assertEqual(self.save(form).status_code, 403)

    def test_detail_is_authorized_bounded_and_rejects_stale_receipt(self):
        review = self.review()
        url = self.base + f"/summary/expectations/review?snapshot={review.snapshot_hash}&assessment={review.assessment_hash}&model=product.template"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("field effects", response.text)
        self.assertNotIn("PX101", response.text)
        self.assertEqual(self.client.get(url + "&page=0").status_code, 409)
        with patch.object(self.context.preflight.authorization, "require", side_effect=AuthorizationError("denied")), patch.object(self.context.preflight.preflight, "get_business_intent_evidence") as repository:
            with self.assertRaises(AuthorizationError):
                self.context.preflight.business_intent_evidence(self.workspace_id, review.snapshot_hash, actor=self.actor, expected_assessment_hash=review.assessment_hash)
            repository.assert_not_called()
        self.expect_business_writes(self.workspace_id)
        self.assertEqual(self.client.get(url).status_code, 409)

    def test_confirmation_and_completed_run_keep_original_review_after_edit(self):
        original = self.review()
        confirmation = self.client.get(self.base + "/load/confirm")
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        self.assertIn(original.assessment_hash, confirmation.text)
        run = self.context.execution.execute(self.workspace_id, expected_snapshot_hash=self.snapshot.semantic_hash,
            expected_intent_assessment_hash=original.assessment_hash, executor=self.target, actor=self.actor, **self.identities)
        self.expect_business_writes(self.workspace_id)
        run_local_operation_sync(self.context.preflight.reassess_business_intent, self.workspace_id, actor=self.actor)
        approved = self.context.execution.current_preview(self.workspace_id, actor=self.actor).business_intent
        self.assertEqual(approved.assessment_hash, original.assessment_hash)
        response = self.client.get(self.base + f"/summary/expectations/review?snapshot={original.snapshot_hash}&assessment={original.assessment_hash}&run={run.run_id}&model=product.template")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Approved for this saved load", response.text)
