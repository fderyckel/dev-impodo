"""Accepted source descriptors remain independent of row preview/evaluation."""

from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.structural import (
    AggregateSpec,
    GroupAggregateRule,
    GroupKey,
    StructuralOutputColumn,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    derived_dataset_links,
    related_dataset_links,
)
from impodo.domain.workspace.supporting_plan import MAX_SUPPORTING_RULES
from impodo.domain.workspace.supporting_sources import (
    SupportingSourceLimitError,
    supporting_source_descriptors,
)

HASH = "sha256:" + "a" * 64


def accepted_selection():
    return SourceSelection(
        str(uuid4()),
        1,
        str(uuid4()),
        datetime.now(timezone.utc),
        "Operator",
        (
            SourceDataset(
                "products",
                "products",
                FileSourceBinding(
                    "file",
                    "csv",
                    HASH,
                    HASH,
                    "utf-8",
                    ",",
                    1,
                ),
                25_000,
                tuple(
                    SourceDatasetColumn(i, key, key, "string")
                    for i, key in enumerate(("product", "category", "line"), 1)
                ),
            ),
        ),
        HASH,
    )


def lookup_rule():
    return DerivedEntityRule(
        str(uuid4()),
        "categories",
        "products",
        "category",
        "x.category",
        "name",
        "categories",
    )


def preparation(selection, *rules):
    return DerivedEntityPlan(
        str(uuid4()),
        1,
        "workspace",
        selection.content_hash,
        tuple(rules),
        datetime.now(timezone.utc),
        "Operator",
    )


class SupportingSourceDescriptorTests(unittest.TestCase):
    def test_structural_output_is_described_without_executing_the_rule(self):
        selection = accepted_selection()
        rule = GroupAggregateRule(
            str(uuid4()),
            "category_totals",
            "products",
            (
                StructuralOutputColumn("key", "Category", "string"),
                StructuralOutputColumn("count", "Count", "integer"),
            ),
            (GroupKey("key", "category"),),
            (AggregateSpec("count", "COUNT"),),
        )
        with patch(
            "impodo.domain.structural.execute_structural_rules",
            side_effect=AssertionError("row evaluation forbidden"),
        ):
            result = supporting_source_descriptors(
                selection, preparation(selection, rule)
            )
        generated = next(item for item in result if item.origin == "DERIVED")
        self.assertEqual(generated.dataset_id, structural_dataset_id(rule))
        self.assertEqual(generated.label, "category_totals")
        self.assertIsNone(generated.target_model)

    def test_physical_selection_needs_no_preview_or_file_read(self):
        result = supporting_source_descriptors(accepted_selection(), None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].origin, "PHYSICAL")
        self.assertIsNone(result[0].target_model)
        self.assertFalse(hasattr(result[0], "rows"))
        self.assertFalse(hasattr(result[0], "row_count"))

    def test_lookup_uses_the_saved_model_and_shared_dataset_identity(self):
        selection = accepted_selection()
        plan = preparation(selection, lookup_rule())
        with patch(
            "impodo.domain.workspace.derived_entities.preview_derived_entities",
            side_effect=AssertionError("preview forbidden"),
        ):
            result = supporting_source_descriptors(selection, plan)
        generated = next(item for item in result if item.origin == "DERIVED")
        self.assertEqual(
            generated.dataset_id, derived_dataset_links(plan)[0].derived_dataset_id
        )
        self.assertEqual(generated.target_model, "x.category")
        self.assertEqual(generated.label, "categories")

    def test_split_keeps_shared_parent_and_child_ids_without_inferring_models(self):
        selection = accepted_selection()
        rule = RelatedDatasetRule(
            str(uuid4()), "products", "parents", "lines", "product", "line"
        )
        plan = preparation(selection, rule)
        with patch(
            "impodo.domain.workspace.derived_entities.preview_related_datasets",
            side_effect=AssertionError("preview forbidden"),
        ):
            result = supporting_source_descriptors(selection, plan)
        link = related_dataset_links(plan)[0]
        self.assertEqual(
            {item.dataset_id for item in result},
            {link.parent_dataset_id, link.child_dataset_id},
        )
        self.assertTrue(all(item.target_model is None for item in result))

    def test_preparation_only_physical_input_is_not_an_incoming_candidate(self):
        selection = accepted_selection()
        plan = replace(
            preparation(selection, lookup_rule()),
            dataset_uses=(
                SourceDatasetUsePolicy("products", SourceDatasetUse.PREPARATION_ONLY),
            ),
        )
        result = supporting_source_descriptors(selection, plan)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].origin, "DERIVED")

    def test_stale_plan_and_missing_lookup_column_fail_closed(self):
        selection = accepted_selection()
        with self.assertRaisesRegex(ValueError, "accepted selection"):
            supporting_source_descriptors(
                selection,
                replace(preparation(selection), source_selection_hash="stale"),
            )
        with self.assertRaisesRegex(ValueError, "unavailable source field"):
            supporting_source_descriptors(
                selection,
                preparation(
                    selection, replace(lookup_rule(), source_column_key="missing")
                ),
            )

    def test_rule_cap_stops_before_any_structural_projection(self):
        selection = accepted_selection()
        plan = preparation(
            selection, *(lookup_rule() for _ in range(MAX_SUPPORTING_RULES + 1))
        )
        with patch(
            "impodo.domain.workspace.supporting_sources.structural_mapping_selection"
        ) as projection:
            with self.assertRaises(SupportingSourceLimitError):
                supporting_source_descriptors(selection, plan)
        projection.assert_not_called()
