"""Business intent observes every intended effect without counting read reuse."""

from dataclasses import replace
import unittest

from impodo.domain.execution_snapshot import FieldIntent, RelationshipCompletion, RelationshipPlan
from impodo.domain.workspace.business_intent import BusinessIntent, BusinessModelIntent, BusinessRelationshipRule, ExpectedCount
from impodo.domain.workspace.business_intent_assessment import BusinessIntentAssessment, BusinessWriteObservation, assess_business_intent, observe_business_writes
from tests.application.workspace.execution.test_service import _snapshot
from tests.support.business_intent_assessment import observation_for, revision_for


class BusinessIntentAssessmentTests(unittest.TestCase):
    def test_unexpected_product_and_forbidden_price_block(self):
        snapshot = _snapshot()
        product = next(row for row in snapshot.rows if row.target_model == "product.template")
        product = replace(product, disposition="UPDATE", fields=(FieldIntent("list_price", "SET_VALUE", 45),))
        observed = observation_for(replace(snapshot, rows=(product,)))
        rule = BusinessIntent((BusinessModelIntent("product.template", "update", update_fields=("name",)),))
        assessment = assess_business_intent(observed, revision_for(snapshot, intent=rule))
        self.assertFalse(assessment.ready)
        self.assertIn("list_price", " ".join(assessment.deviations))
        self.assertFalse(assess_business_intent(observation_for(snapshot), revision_for(snapshot, intent=rule)).ready)

    def test_contact_range_and_zero_limits_include_models_without_writes(self):
        snapshot = _snapshot()
        observed = observation_for(snapshot)
        counts = {item.model: item for item in observed.models}
        rules = BusinessIntent(tuple(BusinessModelIntent(model, "upsert", creates=ExpectedCount(value.creates, value.creates), updates=ExpectedCount(value.updates, value.updates)) for model, value in counts.items()))
        self.assertTrue(assess_business_intent(observed, revision_for(snapshot, intent=rules)).ready)
        empty = replace(observed, models=())
        assessment = assess_business_intent(empty, revision_for(snapshot, intent=BusinessIntent((BusinessModelIntent("res.partner", "update", updates=ExpectedCount(minimum=1)),))))
        self.assertIn("below minimum", " ".join(assessment.deviations))

    def test_supporting_create_and_deferred_completion_count_once(self):
        snapshot = _snapshot()
        row = next(row for row in snapshot.rows if row.target_model == "product.template")
        snapshot = replace(snapshot, rows=(row, replace(row, row_id="other")), relationship_plan=RelationshipPlan(completions=(RelationshipCompletion(row.row_id, "categ_id", ()),)))
        observed = observation_for(snapshot)
        product = next(item for item in observed.models if item.model == "product.template")
        category = next(item for item in observed.models if item.model == "product.category")
        self.assertEqual((product.creates, product.updates), (1, 0))
        self.assertEqual((category.creates, category.updates), (0, 0))
        self.assertEqual(product.create_relationships[0].field, "categ_id")

    def test_authored_add_survives_normalized_replace_and_omitted_fields_do_not_change(self):
        snapshot = _snapshot()
        row = snapshot.rows[0]
        row = replace(row, disposition="UPDATE", fields=(FieldIntent("category_ids", "SET_VALUE", (), kind="relation", relation_operation="replace"), FieldIntent("name", "OMIT")))
        observed = observe_business_writes(replace(snapshot, rows=(row,)), authored_operations=((row.dataset, "category_ids", "add"),))
        rules = BusinessIntent((BusinessModelIntent(row.target_model, "update", update_fields=(), update_relationships=(BusinessRelationshipRule("category_ids", ("add",)),)),))
        self.assertTrue(assess_business_intent(observed, revision_for(snapshot, intent=rules)).ready)
        replace_rule = replace(rules.models[0], update_relationships=(BusinessRelationshipRule("category_ids", ("replace",)),))
        self.assertFalse(assess_business_intent(observed, revision_for(snapshot, intent=BusinessIntent((replace_rule,)))).ready)

    def test_missing_and_stale_intent_fail_closed_and_roundtrip_preserves_hashes(self):
        snapshot = _snapshot()
        observed = observation_for(snapshot)
        missing = assess_business_intent(observed, None)
        self.assertFalse(missing.ready)
        revision = revision_for(snapshot)
        stale = replace(revision, binding=replace(revision.binding, mapping_hash="sha256:" + "9" * 64))
        self.assertFalse(assess_business_intent(observed, stale).ready)
        self.assertEqual(observed, BusinessWriteObservation.from_json(observed.to_json()))
        assessment = assess_business_intent(observed, revision)
        self.assertEqual(assessment, BusinessIntentAssessment.from_json(assessment.to_json()))
        self.assertNotIn("Category", observed.to_json())

    def test_reduced_scope_and_changed_snapshot_have_distinct_assessments(self):
        snapshot = _snapshot()
        first = observation_for(snapshot)
        reduced = observation_for(replace(snapshot, rows=()), reduced_scope_hash="sha256:" + "8" * 64)
        self.assertNotEqual(first.content_hash, reduced.content_hash)
        revision = revision_for(snapshot)
        self.assertNotEqual(assess_business_intent(first, revision).content_hash, assess_business_intent(reduced, revision).content_hash)
