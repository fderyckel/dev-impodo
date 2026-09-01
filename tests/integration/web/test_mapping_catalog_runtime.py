"""Focused runtime evidence for bounded mapping-catalogue work."""

from __future__ import annotations

import asyncio
import unittest

from impodo.web.mapping_catalog_runtime import (
    MappingCatalogCapacityError,
    MappingCatalogProjectionCache,
    MappingCatalogSearchCoordinator,
)


class MappingCatalogProjectionCacheTests(unittest.TestCase):
    def test_projection_is_reused_and_the_cache_remains_bounded(self) -> None:
        cache = MappingCatalogProjectionCache[str](maximum_entries=2)
        builds: list[str] = []

        def build(value: str) -> str:
            builds.append(value)
            return value

        first, first_hit = cache.get_or_create("one", lambda: build("one"))
        repeated, repeated_hit = cache.get_or_create(
            "one",
            lambda: build("unexpected"),
        )
        cache.get_or_create("two", lambda: build("two"))
        cache.get_or_create("three", lambda: build("three"))

        self.assertEqual(first, "one")
        self.assertEqual(repeated, "one")
        self.assertFalse(first_hit)
        self.assertTrue(repeated_hit)
        self.assertEqual(builds, ["one", "two", "three"])
        self.assertEqual(cache.entry_count, 2)


class MappingCatalogSearchCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_newest_waiting_generation_runs(self) -> None:
        coordinator = MappingCatalogSearchCoordinator(maximum_editors=4)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        operations: list[int] = []

        async def first_operation() -> int:
            operations.append(1)
            first_started.set()
            await release_first.wait()
            return 1

        async def operation(generation: int) -> int:
            operations.append(generation)
            return generation

        first = asyncio.create_task(
            coordinator.run_latest("editor", 1, first_operation)
        )
        await first_started.wait()
        second = asyncio.create_task(
            coordinator.run_latest("editor", 2, lambda: operation(2))
        )
        await asyncio.sleep(0)
        third = asyncio.create_task(
            coordinator.run_latest("editor", 3, lambda: operation(3))
        )
        await asyncio.sleep(0)
        release_first.set()

        self.assertEqual(
            await asyncio.gather(first, second, third),
            [None, None, 3],
        )
        self.assertEqual(operations, [1, 3])

    async def test_editor_tracking_is_bounded(self) -> None:
        coordinator = MappingCatalogSearchCoordinator(maximum_editors=2)

        async def operation() -> str:
            return "done"

        for editor in ("one", "two", "three"):
            self.assertEqual(
                await coordinator.run_latest(editor, 1, operation),
                "done",
            )

        self.assertEqual(coordinator.editor_count, 2)

    async def test_editors_share_one_workspace_projection_gate(self) -> None:
        coordinator = MappingCatalogSearchCoordinator(maximum_editors=4)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        operations: list[str] = []

        async def first_operation() -> str:
            operations.append("first")
            first_started.set()
            await release_first.wait()
            return "first"

        async def second_operation() -> str:
            operations.append("second")
            return "second"

        first = asyncio.create_task(
            coordinator.run_latest(
                "editor-one",
                1,
                first_operation,
                work_key="workspace",
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            coordinator.run_latest(
                "editor-two",
                1,
                second_operation,
                work_key="workspace",
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(operations, ["first"])
        release_first.set()

        self.assertEqual(await asyncio.gather(first, second), ["first", "second"])
        self.assertEqual(operations, ["first", "second"])

    async def test_active_editor_capacity_is_rejected_instead_of_growing(self) -> None:
        coordinator = MappingCatalogSearchCoordinator(maximum_editors=1)
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first_operation() -> str:
            first_started.set()
            await release_first.wait()
            return "first"

        first = asyncio.create_task(
            coordinator.run_latest("editor-one", 1, first_operation)
        )
        await first_started.wait()

        with self.assertRaises(MappingCatalogCapacityError):
            await coordinator.run_latest(
                "editor-two",
                1,
                lambda: asyncio.sleep(0, result="second"),
            )

        self.assertEqual(coordinator.editor_count, 1)
        release_first.set()
        self.assertEqual(await first, "first")


if __name__ == "__main__":
    unittest.main()
