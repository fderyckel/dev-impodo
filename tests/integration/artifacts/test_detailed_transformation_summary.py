"""Keep Excel rule explanations exact, ordered, and tied to saved choices."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.domain.mapping.contracts import (
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleSet,
    ValueMapping,
)
from impodo.domain.preflight.mapping_summary import (
    ReviewWorkbookFieldRule,
    workbook_mapping_rules,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy, TextTransformStep
from tests.integration.artifacts import test_mapping_review_workbook as mapping_fixtures
from tests.integration.artifacts import test_transformation_summary as summary_fixtures


class DetailedTransformationSummaryTests(TestCase):
    def rules(self, *fields, restricted=False, **mapping_changes):
        revision, _validation, selection, _schema = (
            mapping_fixtures.MappingReviewWorkbookTests()._evidence()
        )
        mapping = replace(revision.definition.datasets[0], fields=fields, **mapping_changes)
        definition = replace(revision.definition, datasets=(mapping,))
        return workbook_mapping_rules(definition, selection, restricted=restricted)

    def test_names_columns_and_shows_literal_values_and_ordered_text_operations(self):
        rules = self.rules(
            ScalarFieldMapping(
                target_field="active",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="true",
                value_type="boolean",
            ),
            ScalarFieldMapping(
                target_field="vat",
                source_column_key="name",
                transform=ScalarTransformPolicy(
                    trim=True,
                    text_steps=(
                        TextTransformStep(
                            search_value="BE",
                            replacement_value="",
                            search_mode="starts_with",
                        ),
                        TextTransformStep(
                            kind="remove_separators_between_digits", characters=" .-"
                        ),
                        TextTransformStep(
                            search_value="[^0-9]",
                            replacement_value="",
                            search_mode="pattern",
                            replace_all=False,
                        ),
                    ),
                    case_mode="uppercase",
                ),
            ),
        )
        active = rules[("Orders", "active")]
        self.assertEqual(active.source_labels, ())
        self.assertIn('set the initial value to "true"', "\n".join(active.steps))
        self.assertIn("For every included row", "\n".join(active.steps))
        vat = rules[("Orders", "vat")]
        self.assertEqual(vat.source_labels, ("Order Reference",))
        self.assertEqual(vat.target_model, "sale.order")
        steps = "\n".join(vat.steps)
        self.assertLess(
            steps.index("leading and trailing"), steps.index("Text change 1")
        )
        self.assertLess(steps.index("Text change 1"), steps.index("Text change 2"))
        self.assertLess(steps.index("Text change 2"), steps.index("Text change 3"))
        self.assertLess(steps.index("Text change 3"), steps.index("uppercase"))
        self.assertIn('prefix "BE"', steps)
        self.assertIn('characters " .-" only between digits', steps)
        self.assertIn('first match of regular expression "[^0-9]" with ""', steps)

    def test_conditions_value_matches_and_fallback_values_are_explicit(self):
        condition = SelectionCondition(
            "00000000-0000-4000-8000-000000000001",
            "name",
            SelectionConditionOperator.EQUALS,
            "Belgium",
        )
        rules = self.rules(
            ScalarFieldMapping(
                target_field="tracking",
                value_source=ScalarValueSource.CONDITIONAL_RULES,
                selection_rules=SelectionRuleSet(
                    (
                        SelectionRule(
                            "00000000-0000-4000-8000-000000000002", (condition,), "be"
                        ),
                    ),
                    otherwise_value="other",
                ),
            ),
            ScalarFieldMapping(
                target_field="lang",
                source_column_key="name",
                value_mappings=(
                    ValueMapping("French", "fr_FR"),
                    ValueMapping("Dutch", "nl_NL"),
                ),
            ),
            ScalarFieldMapping(
                target_field="note",
                source_column_key="name",
                value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
                literal_value="No notes",
                transform=ScalarTransformPolicy(trim=True, empty_as_null=True),
            ),
        )
        choices = "\n".join(rules[("Orders", "tracking")].steps)
        self.assertIn("first matching", choices)
        self.assertIn('Order Reference (string) equals "Belgium"', choices)
        self.assertIn('use "be"', choices)
        self.assertIn('no condition matches, use "other"', choices)
        self.assertIn('"French" → "fr_FR"', "\n".join(rules[("Orders", "lang")].steps))
        fallback = "\n".join(rules[("Orders", "note")].steps)
        self.assertIn('produces null, use "No notes"', fallback)

    def test_filter_and_multi_column_steps_use_source_headings(self):
        rules = self.rules(
            ScalarFieldMapping(
                target_field="note",
                value_source=ScalarValueSource.CONCATENATE,
                concatenation=ScalarConcatenation(("name", "external_id")),
            ),
            row_inclusion=RowInclusionPolicy(
                mode=RowInclusionMode.MATCHING_ROWS,
                conditions=(
                    RowInclusionCondition(
                        "00000000-0000-4000-8000-000000000001",
                        "name", SelectionConditionOperator.EQUALS, "BOM",
                    ),
                    RowInclusionCondition(
                        "00000000-0000-4000-8000-000000000002",
                        "external_id", SelectionConditionOperator.IS_NOT_BLANK,
                    ),
                ),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="partner_id",
                    kind="many2one",
                    source_column_keys=("external_id",),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="res.partner",
                        key_mappings=(ReferenceKeyMapping("external_id", "ref"),),
                    ),
                ),
            ),
        )
        note = rules[("Orders", "note")]
        self.assertEqual(note.source_labels, ("Order Reference", "External ID"))
        self.assertEqual(
            note.steps[0],
            "Rows to use: Include a row when Order Reference is exactly BOM and External ID is not blank.",
        )
        self.assertIn("Combine Order Reference, External ID, in this order", note.steps[1])
        partner = rules[("Orders", "partner_id")]
        self.assertEqual(partner.source_labels, ("External ID",))
        self.assertIn("Match External ID to res.partner.ref.", partner.steps)

    def test_formula_source_headings_come_from_variables_and_not_quoted_text(self):
        for expression, expected in (("column_1", ("Order Reference",)), ("'column_1'", ())):
            with self.subTest(expression=expression):
                rules = self.rules(
                    ScalarFieldMapping(
                        target_field="computed",
                        value_source=ScalarValueSource.CONSTANT,
                        literal_value="test",
                        transform=ScalarTransformPolicy(formula=expression),
                    )
                )
                self.assertEqual(rules[("Orders", "computed")].source_labels, expected)

    def test_restricted_rule_parameters_remain_withheld(self):
        rules = self.rules(
            ScalarFieldMapping(
                target_field="name",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="PRIVATE-CONSTANT",
                transform=ScalarTransformPolicy(
                    formula="'PRIVATE-FORMULA'",
                    text_steps=(TextTransformStep(search_value="PRIVATE-PATTERN"),),
                ),
            ),
            restricted=True,
        )
        text = "\n".join(rules[("Orders", "name")].steps)
        self.assertNotIn("PRIVATE-", text)
        self.assertIn("Hidden for restricted data", text)

    def test_export_shows_headings_with_field_level_counts_for_both_majors(self):
        fixture = summary_fixtures.TransformationSummaryTests()
        for major in (19, 20):
            for mode in ("local", "remote"):
                with (
                    self.subTest(major=major, mode=mode),
                    TemporaryDirectory() as directory,
                ):
                    manifest, path, evidence = fixture._fixture(
                        directory, major=major, mode=mode
                    )
                    record = evidence.records[0]
                    effect = fixture._effect(record)
                    rule = ReviewWorkbookFieldRule(
                        "res.partner",
                        ("Customer Name",),
                        (
                            "Use the source column value.",
                            "Convert letter case to title.",
                        ),
                    )
                    evidence = replace(
                        evidence,
                        cell_effects=(effect, effect),
                        mapping_rules={
                            (record.dataset, "name"): rule,
                            (record.dataset, "active"): ReviewWorkbookFieldRule(
                                "res.partner",
                                (),
                                (
                                    'For every included row, set the initial value to "true".',
                                ),
                            ),
                        },
                    )
                    original = manifest.read_bytes()
                    write_review_workbook(manifest, path, review_evidence=evidence)
                    workbook = load_workbook(path)
                    self.addCleanup(workbook.close)
                    sheet = workbook.worksheets[0]
                    rows = list(sheet.iter_rows(values_only=True))
                    headers = next(row for row in rows if "Transformation steps" in row)
                    self.assertEqual(
                        tuple(value for value in headers if value is not None),
                        (
                            "Source dataset", "Source column", "Odoo field label",
                            "Odoo model.field", "Transformation steps", "Affected records",
                        ),
                    )
                    name = next(
                        row
                        for row in rows
                        if row[3] == "res.partner.name"
                    )
                    self.assertEqual(
                        name[1:4],
                        ("Customer Name", "Contact name", "res.partner.name"),
                    )
                    self.assertEqual(name[7], 1)
                    active = next(
                        row
                        for row in rows
                        if row[3] == "res.partner.active"
                    )
                    self.assertEqual(active[1], "No source column")
                    self.assertEqual(active[7], 0)
                    self.assertTrue(
                        any(row[4] and "title" in str(row[4]) for row in rows)
                    )
                    self.assertIn("$A$1:$H$", str(sheet.print_area))
                    self.assertEqual(manifest.read_bytes(), original)

    def test_long_rule_lists_keep_every_parameter_without_exceeding_cell_limits(self):
        fixture = summary_fixtures.TransformationSummaryTests()
        with TemporaryDirectory() as directory:
            manifest, path, evidence = fixture._fixture(directory)
            record = evidence.records[0]
            text = "; ".join(
                f'"choice-{index}" → "target-{index}"' for index in range(1000)
            )
            evidence = replace(
                evidence,
                mapping_rules={
                    (record.dataset, "name"): ReviewWorkbookFieldRule(
                        "res.partner",
                        ("Customer Name",),
                        (text,),
                    )
                },
            )
            write_review_workbook(manifest, path, review_evidence=evidence)
            workbook = load_workbook(path)
            self.addCleanup(workbook.close)
            sheet, _, rows = fixture._summary_rows(workbook)
            cells = [row[4] for row in rows if row[4]]
            self.assertIn('"target-999"', "\n".join(cells))
            self.assertEqual("".join(value.replace("\n", "") for value in cells), "1. " + text)
            self.assertTrue(all(len(value) < 32767 for value in cells))
            self.assertTrue(
                all(
                    row.height is None or row.height <= 409
                    for row in sheet.row_dimensions.values()
                )
            )
