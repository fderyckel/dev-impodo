from __future__ import annotations

import unittest

from impodo.web.presenters.common import _plain_ui_error


class CommonPresenterTests(unittest.TestCase):
    def test_stored_quality_failure_has_a_safe_recovery_message(self) -> None:
        raw = "Stored quality evidence is invalid"

        plain, support = _plain_ui_error(raw)

        self.assertEqual(
            plain,
            "Impodo could not reopen the saved prepared-data review. "
            "Nothing was sent to Odoo and your saved project was not changed. "
            "Restart Impodo and try Compare with Odoo again. If it still fails, "
            "contact support.",
        )
        self.assertEqual(support, raw)

    def test_snapshot_publication_failure_is_not_presented_as_stale_source(self) -> None:
        raw = (
            "Impodo could not create the immutable source snapshot: "
            "ARTIFACT_PATH_TOO_LONG path_units=276 portable_limit=259"
        )

        plain, support = _plain_ui_error(raw)

        self.assertEqual(
            plain,
            "Impodo could not save the protected copy of these tables. "
            "Your source files and saved project are unchanged. "
            "Contact support before trying again.",
        )
        self.assertEqual(support, raw)

    def test_snapshot_empty_header_failure_identifies_the_source_and_cell(self) -> None:
        detail = (
            "Source file 'PLW-Article.xlsx', sheet 'PLW', has an empty column "
            "header at G1. Add a name in G1, or remove the column if it is "
            "unused, then replace the file in Source review."
        )
        raw = (
            "Impodo could not create the immutable source snapshot: "
            f"{detail}"
        )

        plain, support = _plain_ui_error(raw)

        self.assertEqual(plain, detail)
        self.assertEqual(support, raw)


if __name__ == "__main__":
    unittest.main()
