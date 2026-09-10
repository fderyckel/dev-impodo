from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations
import unittest

from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    dependency_ordered_execution_datasets,
)
from impodo.domain.matching_order import (
    DatasetOrderEdge,
    MatchingOrderConfidence,
    MatchingOrderFact,
    MatchingOrderPreference,
    MatchingOrderRelationshipOutcome,
    MatchingOrderRelationshipResult,
    MatchingOrderSource,
    live_matching_order_recommendation,
    recommend_dataset_matching_order,
    order_dataset_dependency_components,
)
from impodo.domain.workspace.errors import WorkspaceError


def _edge(owner: str, dependency: str) -> DatasetOrderEdge:
    return DatasetOrderEdge(
        owner_dataset=owner,
        dependency_dataset=dependency,
    )


def _execution_dataset(
    dataset: str,
    *,
    dependencies: tuple[str, ...] = (),
) -> ExecutionDataset:
    return ExecutionDataset(
        dataset=dataset,
        target_model=f"x.{dataset}",
        sequence=0,
        dependencies=dependencies,
        existing_policy="create_only",
        identity_fields=("x_code",),
        scope_fields=(),
    )


class DatasetMatchingOrderTests(unittest.TestCase):
    def test_live_check_removes_only_a_completely_target_satisfied_edge(self) -> None:
        local = recommend_dataset_matching_order(
            ("bom", "article", "route"),
            (
                MatchingOrderFact(
                    owner_dataset="bom",
                    dependency_dataset="article",
                    source=MatchingOrderSource.SAVED_MAPPING,
                    confidence=MatchingOrderConfidence.CONFIRMED,
                    target_field="article_id",
                ),
                MatchingOrderFact(
                    owner_dataset="bom",
                    dependency_dataset="route",
                    source=MatchingOrderSource.SAVED_MAPPING,
                    confidence=MatchingOrderConfidence.CONFIRMED,
                    target_field="route_id",
                ),
            ),
        )

        refined = live_matching_order_recommendation(
            local,
            (
                MatchingOrderRelationshipResult(
                    owner_dataset_id="bom",
                    dependency_dataset_id="article",
                    target_field="article_id",
                    outcome=MatchingOrderRelationshipOutcome.TARGET,
                    target_count=4,
                ),
                MatchingOrderRelationshipResult(
                    owner_dataset_id="bom",
                    dependency_dataset_id="route",
                    target_field="route_id",
                    outcome=MatchingOrderRelationshipOutcome.MIXED,
                    target_count=2,
                    incoming_count=1,
                ),
            ),
        )

        self.assertNotIn(
            ("bom", "article"),
            {
                (item.owner_dataset, item.dependency_dataset)
                for item in refined.facts
            },
        )
        self.assertIn(
            ("bom", "route"),
            {
                (item.owner_dataset, item.dependency_dataset)
                for item in refined.facts
            },
        )

    def test_missing_and_ambiguous_live_results_remain_conservative(self) -> None:
        for outcome, counts in (
            (MatchingOrderRelationshipOutcome.MISSING, {"missing_count": 1}),
            (
                MatchingOrderRelationshipOutcome.AMBIGUOUS,
                {"ambiguous_count": 1},
            ),
        ):
            with self.subTest(outcome=outcome):
                local = recommend_dataset_matching_order(
                    ("consumer", "support"),
                    (
                        MatchingOrderFact(
                            owner_dataset="consumer",
                            dependency_dataset="support",
                            source=MatchingOrderSource.SAVED_MAPPING,
                            confidence=MatchingOrderConfidence.CONFIRMED,
                            target_field="support_id",
                        ),
                    ),
                )
                refined = live_matching_order_recommendation(
                    local,
                    (
                        MatchingOrderRelationshipResult(
                            owner_dataset_id="consumer",
                            dependency_dataset_id="support",
                            target_field="support_id",
                            outcome=outcome,
                            **counts,
                        ),
                    ),
                )
                self.assertEqual(
                    refined.ordered_dataset_ids,
                    ("support", "consumer"),
                )

    def test_display_preference_round_trips_as_a_bounded_contract(self) -> None:
        preference = MatchingOrderPreference(
            workspace_id="  workspace:test  ",
            version=2,
            source_selection_hash="sha256:" + "a" * 64,
            ordered_dataset_ids=(" dataset:article ", "dataset:bom"),
            updated_at=datetime(2026, 9, 10, 9, 30, tzinfo=UTC),
            actor_issuer=" urn:test ",
            actor_subject=" operator ",
            actor_display_name=" Data manager ",
        )

        restored = MatchingOrderPreference.from_json(preference.to_json())

        self.assertEqual(restored, preference)
        self.assertEqual(preference.workspace_id, "workspace:test")
        self.assertEqual(
            preference.ordered_dataset_ids,
            ("dataset:article", "dataset:bom"),
        )
        self.assertEqual(preference.actor_display_name, "Data manager")

    def test_display_preference_rejects_duplicate_tables(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "datasets are invalid"):
            MatchingOrderPreference(
                workspace_id="workspace:test",
                version=1,
                source_selection_hash="sha256:" + "a" * 64,
                ordered_dataset_ids=("dataset:article", "dataset:article"),
                updated_at=datetime.now(UTC),
                actor_issuer="urn:test",
                actor_subject="operator",
                actor_display_name="Data manager",
            )

    def test_explicit_tie_break_makes_input_permutations_equivalent(self) -> None:
        dataset_ids = (
            "plw_bomversion",
            "plw_routeopr",
            "plw_article",
            "plw_bom",
            "plw_route",
        )
        edges = (
            _edge("plw_bom", "plw_article"),
            _edge("plw_bomversion", "plw_bom"),
            _edge("plw_routeopr", "plw_route"),
        )
        tie_break = (
            "plw_article",
            "plw_route",
            "plw_bom",
            "plw_routeopr",
            "plw_bomversion",
        )

        for candidate in permutations(dataset_ids):
            result = order_dataset_dependency_components(
                candidate,
                reversed(edges),
                tie_break_order=tie_break,
            )
            self.assertEqual(result.ordered_dataset_ids, tie_break)

    def test_cycles_stay_grouped_in_tie_break_order(self) -> None:
        result = order_dataset_dependency_components(
            ("consumer", "cycle_b", "independent", "cycle_a"),
            (
                _edge("cycle_a", "cycle_b"),
                _edge("cycle_b", "cycle_a"),
                _edge("consumer", "cycle_a"),
            ),
        )

        self.assertEqual(
            result.components,
            (("cycle_b", "cycle_a"), ("consumer",), ("independent",)),
        )
        self.assertEqual(
            result.ordered_dataset_ids,
            ("cycle_b", "cycle_a", "consumer", "independent"),
        )

    def test_partial_tie_break_keeps_remaining_input_order(self) -> None:
        result = order_dataset_dependency_components(
            ("later", "first", "middle"),
            (),
            tie_break_order=("first",),
        )

        self.assertEqual(
            result.ordered_dataset_ids,
            ("first", "later", "middle"),
        )

    def test_execution_wrapper_preserves_reviewed_order_semantics(self) -> None:
        datasets = (
            _execution_dataset(
                "consumer",
                dependencies=("support", "outside_selection"),
            ),
            _execution_dataset("independent"),
            _execution_dataset("support"),
        )

        ordered = dependency_ordered_execution_datasets(datasets)

        self.assertEqual(
            tuple(item.dataset for item in ordered),
            ("independent", "support", "consumer"),
        )
        self.assertIs(ordered[0], datasets[1])
        self.assertIs(ordered[1], datasets[2])
        self.assertIs(ordered[2], datasets[0])

    def test_long_dependency_chain_does_not_use_recursive_graph_walks(self) -> None:
        dataset_ids = tuple(f"dataset_{index:04d}" for index in range(1_500))
        edges = tuple(
            _edge(dataset_ids[index], dataset_ids[index - 1])
            for index in range(1, len(dataset_ids))
        )

        result = order_dataset_dependency_components(
            tuple(reversed(dataset_ids)),
            edges,
        )

        self.assertEqual(result.ordered_dataset_ids, dataset_ids)

    def test_duplicate_and_invalid_tie_break_inputs_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate datasets"):
            order_dataset_dependency_components(("one", "one"), ())
        with self.assertRaisesRegex(ValueError, "contains duplicates"):
            order_dataset_dependency_components(
                ("one", "two"),
                (),
                tie_break_order=("one", "one"),
            )
        with self.assertRaisesRegex(ValueError, "unknown datasets"):
            order_dataset_dependency_components(
                ("one", "two"),
                (),
                tie_break_order=("outside",),
            )
        duplicate = _execution_dataset("one")
        with self.assertRaisesRegex(
            ValueError,
            "execution snapshot contains duplicate datasets",
        ):
            dependency_ordered_execution_datasets((duplicate, duplicate))


if __name__ == "__main__":
    unittest.main()
