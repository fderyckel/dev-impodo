from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OptionalButtonSemanticsTests(unittest.TestCase):
    def test_optional_rule_review_and_exports_use_secondary_buttons(self) -> None:
        template = (
            ROOT
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_transformation_impact.html"
        ).read_text(encoding="utf-8")

        for label in (
            "Prepare preview",
            "Download matching rows (.csv)",
            "Download all affected rows (.csv)",
        ):
            self.assertRegex(
                template,
                rf'class="button secondary"[^>]*>{re.escape(label)}<',
            )

    def test_review_workbook_actions_use_secondary_buttons(self) -> None:
        template = (
            ROOT
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_summary.html"
        ).read_text(encoding="utf-8")

        for label in (
            "Download review workbook",
            "Create review workbook",
            "Recreate review workbook",
        ):
            matches = re.findall(
                rf'class="button ([^"]+)"[^>]*>{re.escape(label)}<',
                template,
            )
            self.assertTrue(matches, label)
            self.assertTrue(
                all("secondary" in classes.split() for classes in matches),
                label,
            )


if __name__ == "__main__":
    unittest.main()
