"""Capture the implemented optional-Many2one blank decision with fictional data."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.e2e.test_many2one_blank_match import Many2oneBlankMatchBrowserTests


def capture():
    fixture = Many2oneBlankMatchBrowserTests()
    try:
        fixture.setUp()
        fixture.test_leave_blank_is_distinct_from_unresolved_and_survives_reopen_and_refresh()
        fixture.page.screenshot(
            path=str(ROOT / "docs/images/user/03c-many2one-leave-blank.png")
        )
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    capture()
