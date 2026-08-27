"""Protect the deterministic Phase 0 production import inventory."""

from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

import json
from pathlib import Path
import unittest

from scripts.architecture_inventory import (
    ImportEdge,
    build_snapshot,
    imports_from_source,
    module_location,
)


ROOT = REPOSITORY_ROOT
BASELINE = ROOT / "tests" / "architecture" / "phase0_baseline.json"


class ArchitectureInventoryTests(unittest.TestCase):
    def test_current_production_graph_matches_reviewed_phase_zero_baseline(self):
        expected = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(build_snapshot(), expected)

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
