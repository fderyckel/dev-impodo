"""Guard the advisory Python documentation inventory used during review."""

from pathlib import Path
import unittest

from scripts.code_documentation_inventory import (
    inspect_package,
    render_missing,
    render_summary,
    undocumented_modules,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "impodo"


class CodeDocumentationInventoryTests(unittest.TestCase):
    """Prevent module-orientation regression without enforcing docstring quotas."""

    def test_every_package_module_has_an_orientation_docstring(self) -> None:
        modules = inspect_package(PACKAGE_ROOT)

        self.assertTrue(modules)
        self.assertEqual(undocumented_modules(modules), ())

    def test_advisory_reports_remain_deterministic(self) -> None:
        modules = inspect_package(PACKAGE_ROOT)
        reversed_modules = tuple(reversed(modules))

        self.assertEqual(render_summary(modules), render_summary(reversed_modules))
        self.assertEqual(render_missing(modules), render_missing(reversed_modules))
        self.assertIn("| **Total** |", render_summary(modules))


if __name__ == "__main__":
    unittest.main()
