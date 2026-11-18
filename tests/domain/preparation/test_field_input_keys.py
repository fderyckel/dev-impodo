"""Optional scalar provenance preserves absent proof and validates saved readsets."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.preparation.staging_contracts import CanonicalLineage
from impodo.domain.shared.models import canonical_json_bytes


def _lineage():
    return CanonicalLineage(
        source_selection_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
        mapping_hash="sha256:" + "c" * 64,
        schema_hash="sha256:" + "d" * 64,
        derived_plan_hash=None,
        dataset="records",
        source_row=4,
        physical_dataset_id="dataset:records",
        physical_source_rows=(4,),
        field_sources={"x_result": ("source:anchor",)},
    )


class FieldInputKeysTests(TestCase):
    def test_absent_proof_does_not_add_any_current_portable_payload_member(self):
        original = _lineage()
        payload = original.to_portable_dict()
        self.assertNotIn("field_input_keys", payload)
        self.assertEqual(CanonicalLineage.from_dict(payload), original)
        self.assertEqual(
            canonical_json_bytes(payload),
            canonical_json_bytes(
                replace(original, field_input_keys={}).to_portable_dict()
            ),
        )

    def test_known_empty_is_saved_separately_from_missing_target_and_order_is_retained(
        self,
    ):
        original = replace(
            _lineage(),
            field_input_keys={
                "x_other": ("source:last", "source:first"),
                "x_constant": (),
            },
        )
        payload = original.to_portable_dict()
        self.assertEqual(
            payload["field_input_keys"],
            {"x_constant": [], "x_other": ["source:last", "source:first"]},
        )
        restored = CanonicalLineage.from_dict(payload)
        self.assertEqual(restored, original)
        self.assertNotIn("x_result", restored.field_input_keys)
        self.assertEqual(restored.field_input_keys["x_constant"], ())

    def test_malformed_current_proof_cannot_be_reinterpreted_as_source_keys(self):
        invalid = (
            [],
            None,
            {"": []},
            {"x_result": "source:anchor"},
            {"x_result": [""]},
            {"x_result": [2]},
            {"x_result": ["same", "same"]},
        )
        payload = _lineage().to_portable_dict()
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                CanonicalLineage.from_dict({**payload, "field_input_keys": value})
