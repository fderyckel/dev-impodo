"""Bounded review output preserves explicit zero, field rules, and receipts."""

from dataclasses import replace
import unittest

from impodo.application.workspace.execution.business_review import build_business_intent_review, business_intent_evidence_page, BusinessIntentReview
from impodo.domain.workspace.business_intent import BusinessIntent, BusinessModelIntent, ExpectedCount
from impodo.domain.workspace.business_intent_assessment import assess_business_intent, ObservedModelWrites
from tests.application.workspace.execution.test_service import _snapshot
from tests.support.business_intent_assessment import observation_for, revision_for


class BusinessReviewTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = _snapshot()
        self.observation = observation_for(self.snapshot)
        self.revision = revision_for(self.snapshot)

    def test_projection_is_bounded_and_keeps_zero_distinct_from_unspecified(self):
        models = tuple(ObservedModelWrites(f"x.model_{index}", 1, 0, ("name",), (), (), ()) for index in range(100))
        observation = replace(self.observation, models=models)
        intent = BusinessIntent(tuple(BusinessModelIntent(item.model, "upsert", updates=ExpectedCount(maximum=0)) for item in models))
        revision = replace(self.revision, intent=intent)
        assessment = assess_business_intent(observation, revision)
        review = build_business_intent_review(observation, revision, assessment, labels={})
        self.assertEqual(len(review.models), 20)
        self.assertEqual(review.model_count, 100)
        self.assertIsNone(review.models[0].create_max)
        self.assertEqual(review.models[0].update_max, 0)
        self.assertTrue(review.ready)
        self.assertNotEqual(review.stale().state, review.state)
        self.assertEqual(review.stale().assessment_hash, "")
        detail = business_intent_evidence_page(observation, revision, assessment, model="x.model_99", page=1, labels={})
        self.assertEqual(len(detail.models), 100)
        self.assertEqual(detail.model.model, "x.model_99")

    def test_detail_pages_include_restricted_fields_even_when_not_proposed(self):
        model = self.observation.models[0].model
        intent = BusinessIntent((BusinessModelIntent(model, "upsert", create_fields=tuple(f"x_field_{index:03}" for index in range(120))),))
        revision = replace(self.revision, intent=intent)
        assessment = assess_business_intent(self.observation, revision)
        page = business_intent_evidence_page(self.observation, revision, assessment, model=model, page=2, labels={})
        self.assertEqual(len(page.fields), 50)
        self.assertGreater(page.page_count, 2)
        self.assertTrue(any(not item.proposed and item.allowed for item in page.fields))
        with self.assertRaises(ValueError):
            business_intent_evidence_page(self.observation, revision, assessment, model="res.unknown", page=1, labels={})

    def test_expected_state_roundtrips_without_rows_or_field_sets(self):
        from dataclasses import asdict
        assessment = assess_business_intent(self.observation, self.revision)
        review = build_business_intent_review(self.observation, self.revision, assessment, labels={})
        raw = asdict(review)
        self.assertEqual(BusinessIntentReview.from_dict(raw), review)
        self.assertNotIn("fields", raw)
        self.assertEqual(review.assessment_hash, assessment.content_hash)
        self.assertEqual(review.snapshot_hash, self.snapshot.semantic_hash)
