from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from impodo.application.workspace.mapping.bounded_direct_review import (
    direct_transformation_impact,
    uses_bounded_direct_review,
)
from impodo.application.workspace.mapping.row_inclusion_review import (
    RowInclusionReviewService,
)
from impodo.application.workspace.mapping.transformation_impact import (
    TransformationImpactService,
)
from impodo.domain.errors import ReadinessError
from tests.application.workspace.preparation.test_capability import (
    _definition,
    _selection,
)


class BoundedDirectReviewRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = _selection((25_001,))
        self.definition = _definition(self.selection)
        self.context = SimpleNamespace(
            workspace_state=object(),
            revision=SimpleNamespace(definition=self.definition, version=1),
            physical_selection=self.selection,
            effective_selection=self.selection,
            plan=None,
            identity=object(),
        )

    def test_rule_effects_use_bounded_route_above_materialized_limit(self) -> None:
        sources = MagicMock()
        sources.get_source_catalogs.return_value = ()
        sources.get_current_source_snapshots.return_value = ()
        service = TransformationImpactService(
            MagicMock(), MagicMock(), sources, MagicMock(), MagicMock(),
            MagicMock(), MagicMock(),
        )
        expected = object()
        with (
            patch.object(service, "context", return_value=self.context),
            patch.object(
                service,
                "replace_snapshot",
                side_effect=lambda _workspace, _identity, build, **_kwargs: build(
                    lambda _impact: None
                ),
            ),
            patch(
                "impodo.application.workspace.mapping.transformation_impact."
                "direct_transformation_impact",
                return_value=expected,
            ) as bounded,
            patch(
                "impodo.application.workspace.mapping.transformation_impact."
                "stage_browser_mapping",
                side_effect=AssertionError("materialized evaluator was used"),
            ),
        ):
            result = service.prepare_snapshot("workspace", actor=MagicMock())
        self.assertIs(result, expected)
        bounded.assert_called_once()

    def test_rows_to_use_use_bounded_route_above_materialized_limit(self) -> None:
        checked_mapping = MagicMock()
        checked_mapping.context.return_value = self.context
        checked_mapping.sources.get_source_catalogs.return_value = ()
        checked_mapping.sources.get_current_source_snapshots.return_value = ()
        checked_mapping.mappings.get_mapping_working_draft.return_value = None
        reviews = MagicMock()
        expected = object()
        reviews.replace_current_review.return_value = expected
        service = RowInclusionReviewService(
            checked_mapping, reviews, MagicMock()
        )
        report = object()
        with (
            patch(
                "impodo.application.workspace.mapping.row_inclusion_review."
                "direct_row_inclusion_review",
                return_value=report,
            ) as bounded,
            patch(
                "impodo.application.workspace.mapping.row_inclusion_review."
                "stage_browser_mapping",
                side_effect=AssertionError("materialized evaluator was used"),
            ),
        ):
            result = service.check_current("workspace", actor=MagicMock())
        self.assertIs(result, expected)
        bounded.assert_called_once()
        self.assertIs(
            reviews.replace_current_review.call_args.args[1], report
        )

    def test_direct_review_rejects_50_001_before_opening_sources(self) -> None:
        large = replace(
            self.selection,
            datasets=(replace(self.selection.datasets[0], row_count=50_001),),
        )
        with self.assertRaisesRegex(ReadinessError, "50,000"):
            direct_transformation_impact(
                MagicMock(), self.definition, large, large, (), MagicMock(),
                (), lambda _row: None,
            )

    def test_materialized_route_stays_for_small_or_derived_selections(self) -> None:
        small = _selection((25_000,))
        self.assertFalse(uses_bounded_direct_review(small, small, None))
        self.assertTrue(
            uses_bounded_direct_review(
                self.selection, self.selection, None
            )
        )
        self.assertFalse(
            uses_bounded_direct_review(
                self.selection, self.selection, MagicMock()
            )
        )


if __name__ == "__main__":
    unittest.main()
