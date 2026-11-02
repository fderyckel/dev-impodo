"""Recipe runs keep an unused optional collection empty on both Odoo majors."""

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.workspace.supporting_review import SupportingReview
from tests.integration.web import test_recipe_target_matches as recipe_fixtures
from tests.support.identity import resolve_local_actor


class RecipeSupportingIntentTests(unittest.TestCase):
    def test_empty_historical_review_does_not_block_recipe_target_check_or_preparation(self):
        for version in ("19.0", "20.0"):
            with self.subTest(version=version):
                fixture = recipe_fixtures.RecipeTargetMatchBrowserTests()
                fixture.setUp()
                try:
                    url = fixture.review_ready_delivery(odoo_version=version)
                    actor = resolve_local_actor(fixture.context)
                    workspace_id = fixture.application.workspace_id
                    saved = SupportingReview(
                        workspace_id, 1, str(uuid4()), "sha256:" + "1" * 64, (),
                        datetime.now(UTC).isoformat(), actor.identity.issuer,
                        actor.identity.subject_id, actor.identity.display_name,
                    )
                    service = fixture.context.schema_workspace.supporting
                    # Seed historical engine evidence. Application commands
                    # still forbid authoring new supporting choices here.
                    service.repository.local.save_supporting_review(workspace_id, saved, expected_version=0, actor=actor)
                    page = fixture.client.get(url)
                    review = fixture.context.recipe_target_matches.build_review(fixture.application, actor=actor)
                    row = review.fields[0].review_rows[0]
                    accepted = fixture.client.post(
                        url, data={**fixture.form(page), row.input_name: "fr_FR"},
                        headers={"Origin": "http://testserver"}, follow_redirects=False,
                    )
                    self.assertEqual(accepted.status_code, 303, accepted.text)
                    application = fixture.context.run_planning.repository.get_application(fixture.application.application_id)
                    self.assertEqual(application.status, RecipeApplicationStatus.READY)
                    fixture.context.preparation.prepare(workspace_id, actor=actor)
                    staging = fixture.context.preparation.staging.get_current_staging_summary(workspace_id)
                    self.assertEqual(staging.reconciliation.blocked_rows, 0)
                    self.assertTrue(staging.control_totals[0].passed)
                    self.assertEqual(service.current(workspace_id, actor=actor), saved)
                finally:
                    fixture.doCleanups()

    def test_application_target_review_and_preparation_respect_saved_skip(self):
        for version in ("19.0", "20.0"):
            with self.subTest(version=version):
                fixture = recipe_fixtures.RecipeTargetMatchBrowserTests()
                fixture.setUp()
                try:
                    url = fixture.review_ready_delivery(odoo_version=version, include_unused_activities=True)
                    actor = resolve_local_actor(fixture.context)
                    page = fixture.client.get(url)
                    self.assertEqual(page.status_code, 200, page.text)
                    review = fixture.context.recipe_target_matches.build_review(fixture.application, actor=actor)
                    row = review.fields[0].review_rows[0]
                    accepted = fixture.client.post(
                        url, data={**fixture.form(page), row.input_name: "fr_FR"},
                        headers={"Origin": "http://testserver"}, follow_redirects=False,
                    )
                    self.assertEqual(accepted.status_code, 303, accepted.text)
                    application = fixture.context.run_planning.repository.get_application(fixture.application.application_id)
                    self.assertEqual(application.status, RecipeApplicationStatus.READY)
                    fixture.context.preparation.prepare(application.workspace_id, actor=actor)
                    staging = fixture.context.preparation.staging.get_current_staging_summary(application.workspace_id)
                    self.assertTrue(staging.control_totals[0].passed)
                    self.assertEqual(staging.control_totals[0].expected_total, "125.50")
                    revision = fixture.context.mapping_workspace.mappings.get_mapping_revision(application.workspace_id)
                    self.assertEqual([x.target_model for x in revision.definition.datasets], ["res.partner"])
                finally:
                    fixture.doCleanups()
