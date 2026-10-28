"""Keep test paths and browser evidence focused after Phase 4."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "tests"
EVIDENCE_ROOTS = {
    "application",
    "architecture",
    "domain",
    "e2e",
    "integration",
    "performance",
    "platform",
}


class TestOrganizationTests(unittest.TestCase):
    def test_every_discovered_test_names_its_evidence_level(self) -> None:
        misplaced = []
        for path in sorted(TEST_ROOT.rglob("test*.py")):
            relative = path.relative_to(TEST_ROOT)
            if len(relative.parts) < 2 or relative.parts[0] not in EVIDENCE_ROOTS:
                misplaced.append(relative.as_posix())
        self.assertEqual(misplaced, [])

    def test_historical_browser_monolith_cannot_return(self) -> None:
        self.assertFalse((TEST_ROOT / "test_web_app.py").exists())
        oversized = {
            path.relative_to(TEST_ROOT).as_posix(): len(
                path.read_text(encoding="utf-8").splitlines()
            )
            for path in sorted((TEST_ROOT / "integration" / "web").glob("test_*.py"))
            if len(path.read_text(encoding="utf-8").splitlines()) > 2_000
        }
        self.assertEqual(oversized, {})

    def test_browser_support_is_not_a_discovered_test_module(self) -> None:
        support = TEST_ROOT / "support" / "browser_scenarios.py"
        self.assertTrue(support.exists())
        self.assertLessEqual(
            len(support.read_text(encoding="utf-8").splitlines()),
            1_600,
        )

    def test_architecture_tests_are_host_independent_and_hermetic(self) -> None:
        violations: list[str] = []
        repository_tmp = "ROOT / " + '".tmp"'
        repository_tmp_single = "ROOT / " + "'.tmp'"
        for path in sorted((TEST_ROOT / "architecture").glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(ROOT).as_posix()
            if repository_tmp in source or repository_tmp_single in source:
                violations.append(f"{relative}: repository-local .tmp")

            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and (node.value.id, node.attr)
                    in {("os", "name"), ("sys", "platform")}
                ):
                    violations.append(f"{relative}:{node.lineno}: host platform branch")

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
