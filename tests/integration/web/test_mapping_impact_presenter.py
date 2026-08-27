from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from impodo.domain.staging.transformation_impact import TransformationImpactRow
from impodo.domain.staging.transformation_impact import (
    selection_rule_impact_definitions,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleSet,
)
from impodo.web.presenters.mapping_impact import (
    _transformation_impact_row_views,
    _transformation_rule_impact_views,
)
from impodo.web.presenters.mapping_view import _mapping_next_step


class TransformationImpactPresenterTests(unittest.TestCase):
    def test_valid_checked_mapping_can_confirm_without_rule_effect_preview(
        self,
    ) -> None:
        next_step = _mapping_next_step(
            workspace_id="workspace-1",
            schema=SimpleNamespace(origin=SimpleNamespace(value="LIVE_API")),
            revision=SimpleNamespace(),
            validation=SimpleNamespace(status=SimpleNamespace(value="VALID")),
            submission=None,
            has_unvalidated_changes=False,
            blocking_issue_views=(),
            previous_check_blocking_issue_views=(),
            readonly_field_recovery=None,
        )

        self.assertTrue(next_step["available"])
        self.assertEqual(next_step["action"], "submit")
        self.assertEqual(next_step["label"], "Confirm field matches")
        self.assertEqual(next_step["blockers"], ())

    def test_edge_spaces_are_explained_when_values_look_identical(self) -> None:
        row = TransformationImpactRow(
            dataset="contacts",
            source_row=9,
            source_column="Telephone",
            target_field="phone",
            raw_value=" +43 000 000 0008 ",
            proposed_value="+43 000 000 0008",
            rules="Source + Trim + Empty to null",
            outcome="changed",
        )

        view = _transformation_impact_row_views((row,))[0]

        self.assertEqual(
            view.original_spacing_note,
            "Contains 1 space before the value and 1 space after the value.",
        )
        self.assertEqual(
            view.prepared_spacing_note,
            "Removed 1 space before the value and 1 space after the value.",
        )

    def test_non_spacing_change_has_no_spacing_note(self) -> None:
        row = TransformationImpactRow(
            dataset="contacts",
            source_row=10,
            source_column="Telephone",
            target_field="phone",
            raw_value="067-77-37-67",
            proposed_value="067773767",
            rules="Remove separators between digits",
            outcome="changed",
        )

        view = _transformation_impact_row_views((row,))[0]

        self.assertEqual(view.original_spacing_note, "")
        self.assertEqual(view.prepared_spacing_note, "")

    def test_selection_rules_show_zero_match_and_overlap_decisions(self) -> None:
        rules = SelectionRuleSet(
            rules=(
                SelectionRule(
                    rule_id=str(uuid4()),
                    conditions=(
                        SelectionCondition(
                            condition_id=str(uuid4()),
                            source_column_key="column:organisation",
                            operator=SelectionConditionOperator.IS_NOT_BLANK,
                        ),
                    ),
                    target_value="company",
                ),
                SelectionRule(
                    rule_id=str(uuid4()),
                    conditions=(
                        SelectionCondition(
                            condition_id=str(uuid4()),
                            source_column_key="column:category",
                            operator=SelectionConditionOperator.EQUALS,
                            comparison_value="business",
                        ),
                    ),
                    target_value="person",
                ),
            ),
            otherwise_value="person",
        )
        field = ScalarFieldMapping(
            target_field="company_type",
            value_source=ScalarValueSource.CONDITIONAL_RULES,
            categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
            selection_rules=rules,
        )
        dataset_id = "dataset:0123456789abcdef01234567"
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash="sha256:" + "1" * 64,
            schema_hash="sha256:" + "2" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset_id,
                    target_model="res.partner",
                    fields=(field,),
                ),
            ),
        )
        definitions = selection_rule_impact_definitions(dataset_id, field)
        impacts = (
            replace(
                definitions[0],
                evaluated_value_count=3,
                matched_value_count=2,
                changed_value_count=2,
            ),
            replace(
                definitions[1],
                evaluated_value_count=3,
                matched_value_count=1,
                changed_value_count=1,
            ),
            replace(
                definitions[2],
                evaluated_value_count=3,
                matched_value_count=0,
                changed_value_count=0,
            ),
            definitions[3],
        )
        snapshot = SimpleNamespace(
            acknowledged_rule_fingerprints=(),
            report=SimpleNamespace(rule_impacts=impacts),
        )

        views = _transformation_rule_impact_views(
            Request({"type": "http", "query_string": b""}),
            "project:contacts",
            snapshot,
            SimpleNamespace(definition=definition),
            SimpleNamespace(
                datasets=(SimpleNamespace(dataset_id=dataset_id, name="Contacts"),)
            ),
            {("Contacts", "company_type"): "Company Type"},
            {
                ("Contacts", "company_type", "company"): "Company",
                ("Contacts", "company_type", "person"): "Individual",
            },
        )

        self.assertEqual(len(views), 2)
        self.assertEqual(views[0]["overlap_count"], 1)
        self.assertEqual(views[0]["target_label"], "Company")
        self.assertEqual(
            views[0]["acknowledgements"][0]["label"],
            "Keep this rule priority",
        )
        self.assertEqual(views[1]["impact"].matched_value_count, 0)
        self.assertEqual(
            views[1]["acknowledgements"][0]["label"],
            "Keep this unused rule",
        )


if __name__ == "__main__":
    unittest.main()
