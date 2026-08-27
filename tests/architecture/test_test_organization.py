"""Keep test paths and browser evidence focused after Phase 4."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = ROOT / "tests"
EVIDENCE_ROOTS = {
    "application",
    "architecture",
    "domain",
    "e2e",
    "integration",
    "performance",
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


if __name__ == "__main__":
    unittest.main()
