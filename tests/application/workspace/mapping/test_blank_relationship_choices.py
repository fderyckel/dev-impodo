"""Keep intentional blanks out of live matching probes and Recipe review gaps."""

import unittest
from types import SimpleNamespace

from impodo.adapters.artifacts.mapping_review import _coverage_proposed_value
from impodo.application.run.target_matches import _target_value_rows
from impodo.application.workspace.mapping.order_service import (
    _relationship_source_key_counts,
)
from impodo.domain.mapping.validation.evidence import CategoricalValueCount
from tests.domain.mapping.test_relationship_blank_values import relationship


class BlankRelationshipChoiceTests(unittest.TestCase):
    def test_simulation_excludes_intentional_blanks_from_target_key_requests(self):
        counts, blanks, incomplete = _relationship_source_key_counts(
            relationship(),
            (("TO_DEFINE",), ("Standard",), (None,)),
            target_fields=("name",),
        )
        self.assertEqual(dict(counts), {("Standard",): 1})
        self.assertEqual((blanks, incomplete), (2, 0))

    def test_recipe_review_treats_saved_blank_as_handled_without_a_target_record(self):
        result = SimpleNamespace(
            distinct_values=(CategoricalValueCount(("TO_DEFINE",), 87),),
            uncovered_values=(),
        )
        rows = _target_value_rows(
            0,
            0,
            "relationship",
            result,
            (),
            {},
            exact_values={},
            blank_source_values=frozenset({"TO_DEFINE"}),
            submitted=None,
        )
        self.assertFalse(rows[0].needs_review)
        self.assertEqual(rows[0].current_target_label, "Leave blank (no linked record)")
        self.assertEqual(rows[0].selected_target_value, "")

    def test_review_workbook_names_the_intentional_blank(self):
        mapping = SimpleNamespace(fields=(), relationships=(relationship(),))
        result = SimpleNamespace(target_field="x_list_id")
        self.assertEqual(
            _coverage_proposed_value(mapping, result, ("TO_DEFINE",)),
            "Leave blank (no linked record)",
        )
