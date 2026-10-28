"""Protect the production import graph's architectural invariants."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.architecture_inventory import (
    ImportEdge,
    build_snapshot,
    discover_modules,
    imports_from_source,
    module_location,
)


class ArchitectureInventoryTests(unittest.TestCase):
    def test_current_production_graph_satisfies_architecture_invariants(self):
        self.assertTrue(discover_modules())

        snapshot = build_snapshot()
        self.assertEqual(snapshot["unclassified_modules"], [])
        self.assertEqual(snapshot["runtime_cycles"], [])
        self.assertEqual(snapshot["application_to_adapter_edges"], [])

    def test_relative_and_type_only_imports_are_resolved(self):
        known = {
            "impodo",
            "impodo.adapters",
            "impodo.adapters.duckdb",
            "impodo.application",
            "impodo.application.ports",
            "impodo.application.service",
            "impodo.domain",
        }
        source = """
from .ports import PreparationPort
from .. import domain
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from impodo.adapters.duckdb import PreparationRepository
"""

        self.assertEqual(
            imports_from_source(
                module="impodo.application.service",
                source=source,
                is_package=False,
                known_modules=known,
            ),
            (
                ImportEdge(
                    "impodo.application.service",
                    "impodo.adapters.duckdb",
                    True,
                ),
                ImportEdge(
                    "impodo.application.service",
                    "impodo.application.ports",
                    False,
                ),
                ImportEdge(
                    "impodo.application.service",
                    "impodo.domain",
                    False,
                ),
            ),
        )

    def test_unknown_nested_production_package_is_not_silently_classified(self):
        self.assertEqual(
            module_location(Path("application/__init__.py")),
            "application",
        )
        self.assertEqual(
            module_location(Path("unowned_capability/service.py")),
            "unclassified",
        )


if __name__ == "__main__":
    unittest.main()
