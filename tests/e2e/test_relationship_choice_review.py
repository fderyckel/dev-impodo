"""Qualify protected relationship review with an isolated authenticated browser.

IMPODO_RELATIONSHIP_CHOICE_SCREENSHOT_DIRECTORY captures the current Stage 3
decision and review at 1440 by 1024 without contacting a live Odoo service.
"""

import os
from pathlib import Path
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.relationship_choice_review import relationship_choice_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class RelationshipChoiceJourneyTests(unittest.TestCase):
    def test_review_paging_keyboard_and_mobile_layout_preserve_the_checked_choices(
        self,
    ):
        fixture = HierarchyWorkflowFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, owner, _parent, check, reads = relationship_choice_workspace(
            fixture
        )
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            headless=True,
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL")
            or ("msedge" if sys.platform == "win32" else None),
        )
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        context.add_cookies(
            [
                {
                    "name": "impodo_session",
                    "value": fixture.client.cookies.get("impodo_session"),
                    "url": base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def capture(name):
            output = os.environ.get("IMPODO_RELATIONSHIP_CHOICE_SCREENSHOT_DIRECTORY")
            if output:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / name))

        page.goto(
            base + f"/workspaces/{workspace_id}/mapping", wait_until="networkidle"
        )
        card = page.locator(".relationship-health-card").filter(has_text="Category")
        card.scroll_into_view_if_needed()
        capture("11l-mapping-relationship-actions.png")
        expect(card.get_by_role("link", name="Change matching rule")).to_be_visible()
        review = card.get_by_role("link", name="Review unresolved choices")
        review.focus()
        review.press("Enter")
        expect(
            page.get_by_role("heading", name="Review unresolved choices")
        ).to_be_visible()
        expect(
            page.get_by_role("status").filter(has_text="choice occurrences")
        ).to_contain_text("111 choice occurrences")
        expect(page.locator("tbody tr")).to_have_count(100)
        expect(
            page.get_by_role("combobox", name="Linked field", exact=True)
        ).to_have_value("category_id")
        expect(page.get_by_role("combobox", name="Table", exact=True)).to_have_value(
            owner.dataset_id
        )
        page.get_by_role("link", name="Next 100").click()
        expect(page.locator("tbody tr")).to_have_count(11)
        expect(
            page.get_by_role("status").filter(has_text="choice occurrences")
        ).to_contain_text("Showing 101–111")
        page.get_by_role("link", name="Previous 100").click()
        expect(page.locator("tbody tr")).to_have_count(100)
        page.get_by_role("combobox", name="Reason", exact=True).select_option(
            "case_only"
        )
        page.get_by_role("button", name="Apply filters").click()
        expect(page.locator("tbody tr")).to_have_count(2)
        expect(
            page.get_by_role("status").filter(has_text="choice occurrences")
        ).to_contain_text("2 choice occurrences")
        self.assertIn(check.check_id, page.url)
        page.evaluate("window.scrollTo(0, 0)")
        capture("11m-relationship-choice-review.png")
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        region = page.get_by_role("region", name="Unresolved relationship choices")
        region.focus()
        region.press("ArrowRight")
        page.wait_for_timeout(300)
        self.assertGreater(region.evaluate("element => element.scrollLeft"), 0)
        self.assertNotIn("79000001", page.locator("body").inner_text())
        self.assertEqual(len(reads), 1)
        self.assertEqual(errors, [])
        self.assertEqual(
            fixture.context.matching_order._preferences.get_current_check(workspace_id),
            check,
        )
