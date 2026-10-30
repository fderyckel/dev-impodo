"""Portable expectations distinguish absence, zero and an explicitly closed scope."""

from dataclasses import replace
from datetime import UTC, datetime
import json
import unittest
from uuid import uuid4

from impodo.domain.workspace.business_intent import BusinessIntent, BusinessIntentBinding, BusinessIntentError, BusinessIntentRevision, BusinessModelIntent, BusinessRelationshipRule, ExpectedCount
from tests.support.business_intent import ACTOR


class BusinessIntentContractTests(unittest.TestCase):
    def test_zero_counts_and_empty_field_lists_are_distinct_from_unspecified(self):
        unspecified = BusinessIntent((BusinessModelIntent("res.partner", "upsert"),))
        zero = BusinessIntent((BusinessModelIntent("res.partner", "upsert", creates=ExpectedCount(maximum=0), update_fields=()),))
        self.assertIsNone(unspecified.models[0].creates.maximum)
        self.assertEqual(zero.models[0].creates.maximum, 0)
        self.assertNotEqual(unspecified.content_hash, zero.content_hash)
        self.assertEqual(zero, BusinessIntent.from_json(zero.to_json()))
        self.assertEqual(BusinessIntent(()).models, ())

    def test_ordering_is_deterministic_and_contains_no_runtime_identity(self):
        relationship = BusinessRelationshipRule("category_ids", ("remove", "add"))
        first = BusinessIntent((BusinessModelIntent("res.partner", "update", update_fields=("name", "email"), update_relationships=(relationship,)), BusinessModelIntent("product.template", "none")))
        second = BusinessIntent(tuple(reversed(first.models)))
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotIn("workspace_id", first.to_json())
        self.assertNotIn("binding", first.to_json())
        self.assertNotIn("approval", first.to_json())

    def test_invalid_counts_and_contradictory_modes_are_rejected(self):
        for values in ((-1, None), (True, None), (1.5, None), (5, 4), (None, 2**63)):
            with self.subTest(values=values), self.assertRaises(BusinessIntentError):
                ExpectedCount(*values)
        with self.assertRaises(BusinessIntentError):
            BusinessModelIntent("res.partner", "update", creates=ExpectedCount(minimum=1))
        with self.assertRaises(BusinessIntentError):
            BusinessModelIntent("res.partner", "none", update_fields=("email",))

    def test_unknown_and_duplicate_contract_meaning_is_rejected(self):
        for raw in (
            {"contract_version": 2, "models": []}, {"contract_version": True, "models": []},
            {"contract_version": 1, "models": [], "approval": True},
        ):
            with self.subTest(raw=raw), self.assertRaises(BusinessIntentError):
                BusinessIntent.from_json(json.dumps(raw))
        rule = BusinessModelIntent("res.partner", "none")
        with self.assertRaises(BusinessIntentError):
            BusinessIntent((rule, rule))
        with self.assertRaises(BusinessIntentError):
            BusinessModelIntent("res.partner", "update", update_fields=("email", "email"))
        with self.assertRaises(ValueError):
            BusinessRelationshipRule("category_ids", ("delete_all",))

    def test_recording_integrity_is_separate_from_reusable_meaning(self):
        intent = BusinessIntent((BusinessModelIntent("res.partner", "update", updates=ExpectedCount(0, 500)),))
        binding = BusinessIntentBinding(*("sha256:" + str(index) * 64 for index in range(1, 7)))
        revision = BusinessIntentRevision(str(uuid4()), 1, str(uuid4()), intent, binding, ACTOR.identity, datetime.now(UTC))
        self.assertEqual(revision, BusinessIntentRevision.from_json(revision.to_json()))
        retried = replace(revision, recorded_at=datetime.now(UTC))
        self.assertEqual(retried.request_hash, revision.request_hash)
        self.assertNotEqual(retried.content_hash, revision.content_hash)
        raw = json.loads(revision.to_json())
        raw["intent"]["models"][0]["updates"]["maximum"] = 999
        with self.assertRaisesRegex(BusinessIntentError, "hash"):
            BusinessIntentRevision.from_json(json.dumps(raw))
