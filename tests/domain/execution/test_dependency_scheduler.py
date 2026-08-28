from __future__ import annotations

import unittest

from impodo.domain.execution.dependency_scheduler import (
    dependency_component_pages,
    DependencyEdge,
    DependencyNode,
    ScheduleBlocker,
    schedule_dependencies,
)


class DependencySchedulerTests(unittest.TestCase):
    def test_component_pages_are_bounded_and_never_mix_levels(self):
        pages = tuple(
            dependency_component_pages(
                (
                    (0, (f"first-{index}" for index in range(501))),
                    (1, ("second",)),
                ),
                max_rows=500,
            )
        )

        self.assertEqual(
            tuple(
                (page.component_sequence, page.page_sequence, len(page.row_ids))
                for page in pages
            ),
            ((0, 0, 500), (0, 1, 1), (1, 0, 1)),
        )
        self.assertEqual(pages[-1].row_ids, ("second",))

    def test_hierarchy_places_each_parent_in_an_earlier_component(self):
        nodes = tuple(
            DependencyNode(f"row-{index}", index)
            for index in range(4)
        )
        edges = tuple(
            DependencyEdge(
                dependency_row_id=f"row-{index - 1}",
                owner_row_id=f"row-{index}",
                owner_field="parent_id",
                strength="deferrable",
            )
            for index in range(1, 4)
        )

        schedule = schedule_dependencies(reversed(nodes), reversed(edges))

        self.assertEqual(
            schedule.ordered_row_ids,
            ("row-0", "row-1", "row-2", "row-3"),
        )
        self.assertEqual(
            schedule.components,
            (("row-0",), ("row-1",), ("row-2",), ("row-3",)),
        )
        self.assertEqual(schedule.deferred_edges, ())

    def test_optional_cycle_cuts_one_exact_deterministic_edge(self):
        nodes = (DependencyNode("first", 0), DependencyNode("second", 1))
        edges = (
            DependencyEdge("second", "first", "second_id", "deferrable"),
            DependencyEdge("first", "second", "first_id", "deferrable"),
        )

        schedule = schedule_dependencies(nodes, edges)
        permuted = schedule_dependencies(reversed(nodes), reversed(edges))

        self.assertEqual(schedule, permuted)
        self.assertEqual(schedule.ordered_row_ids, ("first", "second"))
        self.assertEqual(
            schedule.deferred_edges,
            (DependencyEdge("second", "first", "second_id", "deferrable"),),
        )

    def test_hard_cycle_blocks_every_member_without_a_schedule(self):
        schedule = schedule_dependencies(
            (DependencyNode("first", 0), DependencyNode("second", 1)),
            (
                DependencyEdge("second", "first", "second_id", "hard"),
                DependencyEdge("first", "second", "first_id", "hard"),
            ),
        )

        self.assertEqual(schedule.ordered_row_ids, ())
        self.assertEqual(
            tuple(item.row_id for item in schedule.blockers),
            ("first", "second"),
        )
        self.assertTrue(
            all(item.code == "HARD_DEPENDENCY_CYCLE" for item in schedule.blockers)
        )

    def test_blocker_propagates_to_transitive_consumers(self):
        schedule = schedule_dependencies(
            tuple(DependencyNode(f"row-{index}", index) for index in range(3)),
            (
                DependencyEdge("row-0", "row-1", "parent_id", "hard"),
                DependencyEdge("row-1", "row-2", "parent_id", "hard"),
            ),
            (ScheduleBlocker("row-0", "MISSING_INCOMING_ROW"),),
        )

        self.assertEqual(schedule.ordered_row_ids, ())
        self.assertEqual(
            tuple((item.row_id, item.code) for item in schedule.blockers),
            (
                ("row-0", "MISSING_INCOMING_ROW"),
                ("row-1", "BLOCKED_DEPENDENCY"),
                ("row-2", "BLOCKED_DEPENDENCY"),
            ),
        )

    def test_long_chain_uses_iterative_component_discovery(self):
        row_count = 2_000
        schedule = schedule_dependencies(
            tuple(
                DependencyNode(f"row-{index:04d}", index)
                for index in range(row_count)
            ),
            tuple(
                DependencyEdge(
                    f"row-{index - 1:04d}",
                    f"row-{index:04d}",
                    "parent_id",
                    "hard",
                )
                for index in range(1, row_count)
            ),
        )

        self.assertEqual(len(schedule.ordered_row_ids), row_count)
        self.assertEqual(len(schedule.components), row_count)
        self.assertEqual(schedule.blockers, ())


if __name__ == "__main__":
    unittest.main()
