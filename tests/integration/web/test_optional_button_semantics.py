from __future__ import annotations

import re
import unittest

from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


class OptionalButtonSemanticsTests(unittest.TestCase):
    def test_combined_information_action_is_visible_and_secondary(self) -> None:
        template = (
            ROOT
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_datasets.html"
        ).read_text(encoding="utf-8")

        self.assertIn('<section class="optional-workflow"', template)
        self.assertIn('<p class="eyebrow">Optional</p>', template)
        self.assertRegex(
            template,
            r"<h3[^>]*>Does one table combine different kinds of information\?</h3>",
        )
        self.assertNotIn(
            "<summary>Does one table combine different kinds of information?</summary>",
            template,
        )
        self.assertRegex(
            template,
            r'class="button secondary"[^>]*>Separate combined information<',
        )

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
