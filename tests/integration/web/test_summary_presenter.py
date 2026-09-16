from __future__ import annotations

from pathlib import Path
import unittest

from impodo.web.presenters.summary import (
    _preparation_limit_message,
    _summary_page_size,
)


class SummaryPageSizeTests(unittest.TestCase):
    def test_accepts_only_the_four_bounded_display_sizes(self) -> None:
        self.assertEqual(
            tuple(_summary_page_size(str(size)) for size in (10, 20, 50, 100)),
            (10, 20, 50, 100),
        )
        self.assertEqual(_summary_page_size(None), 20)
        self.assertEqual(_summary_page_size("5000"), 20)
        self.assertEqual(_summary_page_size("invalid"), 20)


class PreparationLimitCopyTests(unittest.TestCase):
    def test_direct_limit_stays_in_source_and_field_rule_language(self) -> None:
        message = _preparation_limit_message(
            bounded_direct=True,
            supported_limit=50_000,
        )

        self.assertEqual(
            message,
            "With this source setup and these field rules, Impodo can safely "
            "prepare up to 50,000 rows in one project.",
        )
        self.assertNotIn("Polars", message)
        self.assertNotIn("Python", message)

    def test_related_limit_explains_the_lower_boundary(self) -> None:
        message = _preparation_limit_message(
            bounded_direct=False,
            supported_limit=25_000,
        )

        self.assertEqual(
            message,
            "This setup includes related or grouped source data, so Impodo "
            "can safely prepare up to 25,000 rows in one project.",
        )
        self.assertNotIn("DuckDB", message)
        self.assertNotIn("Parquet", message)


class SummaryCardLinkContractTests(unittest.TestCase):
    def test_each_card_targets_its_matching_filtered_row_section(self) -> None:
        template_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_summary.html"
        )
        template = template_path.read_text(encoding="utf-8")

        self.assertEqual(template.count('id="quality-rows"'), 1)
        self.assertEqual(template.count('id="readiness-rows"'), 1)
        for status in ("ready", "review", "quarantined", "blocked"):
            self.assertIn(
                f'href="?quality_status={status}#quality-rows"',
                template,
            )
        for status in ("create", "update", "unchanged", "attention"):
            self.assertIn(
                f'href="?status={status}#readiness-rows"',
                template,
            )


if __name__ == "__main__":
    unittest.main()
