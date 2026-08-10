from __future__ import annotations

import unittest

from impodo.web.presenters.common import _plain_ui_error


class CommonPresenterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
