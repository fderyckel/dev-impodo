"""Prove single-anchor scalar originals using validated formula metadata only."""

from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from impodo.domain.mapping.contracts import (
    ScalarFieldMapping,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.preflight.scalar_inputs import (
    scalar_formula_aliases,
    scalar_original_inputs,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.workspace.contracts import SourceDatasetColumn

_ANCHOR = "column:anchor"
_OTHER = "column:other"
_COLUMNS = (
    SourceDatasetColumn(3, "Other", _OTHER, "string"),
    SourceDatasetColumn(7, "Anchor", _ANCHOR, "string"),
)


def _field(formula: str = "", *, fallback: bool = False) -> ScalarFieldMapping:
    return ScalarFieldMapping(
        "name",
        _ANCHOR,
        value_source=ScalarValueSource.SOURCE_WITH_FALLBACK
        if fallback
        else ScalarValueSource.SOURCE,
        literal_value="Fallback" if fallback else None,
        transform=ScalarTransformPolicy(formula=formula, trim=True, empty_as_null=True),
    )


class ScalarOriginalInputTests(TestCase):
    def setUp(self) -> None:
        self.aliases = scalar_formula_aliases(_COLUMNS)
        self.keys = frozenset({_ANCHOR, _OTHER})

    def proof(self, field: ScalarFieldMapping, *, aliases=None):
        return scalar_original_inputs(
            field,
            source_keys=self.keys,
            formula_aliases=self.aliases if aliases is None else aliases,
        )

    def test_aliases_use_frozen_ordinals_instead_of_display_positions(self) -> None:
        self.assertEqual(self.aliases, {"column_3": _OTHER, "column_7": _ANCHOR})
        self.assertEqual(self.proof(_field("column_7")), (_ANCHOR,))
        self.assertIsNone(self.proof(_field("column_2")))

    def test_ambiguous_or_malformed_ordinal_metadata_is_rejected(self) -> None:
        invalid = (
            (_COLUMNS[0], replace(_COLUMNS[1], ordinal=3)),
            (_COLUMNS[0], replace(_COLUMNS[1], stable_key=_OTHER)),
            *(
                (replace(_COLUMNS[0], ordinal=ordinal), _COLUMNS[1])
                for ordinal in (0, -1, True, 1.5)
            ),
            (replace(_COLUMNS[0], stable_key=""), _COLUMNS[1]),
            (replace(_COLUMNS[0], stable_key=" "), _COLUMNS[1]),
        )
        for columns in invalid:
            with self.subTest(columns=columns):
                self.assertIsNone(scalar_formula_aliases(columns))
        self.assertIsNone(
            scalar_original_inputs(
                _field("value"), source_keys=self.keys, formula_aliases=None
            )
        )

    def test_direct_no_formula_source_and_fallback_keep_reviewed_choice_behavior(
        self,
    ) -> None:
        for fallback in (False, True):
            field = replace(
                _field(fallback=fallback),
                value_mappings=(ValueMapping("A", "Matched"),),
            )
            with self.subTest(fallback=fallback):
                self.assertEqual(
                    scalar_original_inputs(
                        field, source_keys=self.keys, formula_aliases=None
                    ),
                    (_ANCHOR,),
                )

    def test_every_used_name_must_resolve_to_the_same_saved_anchor(self) -> None:
        for formula in (
            "value",
            "upper(value)",
            "value + column_7",
            "column_7 / column_7",
            "upper(value, value)",
        ):
            for fallback in (False, True):
                with self.subTest(formula=formula, fallback=fallback):
                    self.assertEqual(
                        self.proof(_field(formula, fallback=fallback)), (_ANCHOR,)
                    )

    def test_constant_only_invalid_unknown_and_other_column_formulas_are_unproven(
        self,
    ) -> None:
        for formula in (
            '"FIXED"',
            "1 / 0",
            "False",
            "upper",
            "value +",
            "column_999",
            "value ** 2",
            "column_3",
            "value + column_3",
            "value if True else column_3",
        ):
            for fallback in (False, True):
                with self.subTest(formula=formula, fallback=fallback):
                    self.assertIsNone(self.proof(_field(formula, fallback=fallback)))

    def test_branching_formulas_are_deferred_including_nested_unused_inputs(
        self,
    ) -> None:
        formulas = (
            'value if False else "FIXED"',
            "value if False else 1 / 0",
            "True or value",
            "False and value",
            "0 > 1 > value",
            'concat("prefix", value if False else "FIXED")',
            "upper(value if value else 'FIXED')",
        )
        for formula in formulas:
            with self.subTest(formula=formula):
                self.assertIsNone(self.proof(_field(formula)))
        for formula in ("value == 1", "-value", "not value", "coalesce(value, 1)"):
            with self.subTest(eager_formula=formula):
                self.assertEqual(self.proof(_field(formula)), (_ANCHOR,))

    def test_formula_value_mappings_are_excluded_because_they_can_bypass_the_rule(
        self,
    ) -> None:
        field = replace(_field("value"), value_mappings=(ValueMapping("A", "Matched"),))
        self.assertIsNone(self.proof(field))

    def test_unknown_anchor_and_constant_provider_never_gain_formula_proof(
        self,
    ) -> None:
        for field in (
            replace(_field("value"), source_column_key="missing"),
            replace(_field("value"), source_column_key=None),
            ScalarFieldMapping(
                "name",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="Fixed",
                transform=ScalarTransformPolicy(formula="column_7"),
            ),
        ):
            with self.subTest(field=field):
                self.assertIsNone(self.proof(field))

    def test_metadata_proof_never_evaluates_scalar_rules(self) -> None:
        with (
            patch("impodo.domain.recipe.value_rules.evaluate_formula") as formula,
            patch(
                "impodo.domain.mapping.scalar_values.evaluate_scalar_mapping_value"
            ) as scalar,
        ):
            self.assertEqual(self.proof(_field("value / 0")), (_ANCHOR,))
        formula.assert_not_called()
        scalar.assert_not_called()
