"""Qualify protected group review without contacting a live Odoo service.

IMPODO_IDENTITY_GROUP_SCREENSHOT_DIRECTORY captures the 1440 by 1024 workflow
images from this isolated authenticated fixture.
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
from tests.support.identity_group_review import duplicate_identity_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class IdentityGroupJourneyTests(unittest.TestCase):
    def test_group_paging_keyboard_and_narrow_layout_keep_the_checked_population(self):
        fixture = HierarchyWorkflowFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, dataset, check, reads = duplicate_identity_workspace(fixture)
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
            output = os.environ.get("IMPODO_IDENTITY_GROUP_SCREENSHOT_DIRECTORY")
            if output:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / name))

        page.goto(
            base + f"/workspaces/{workspace_id}/mapping", wait_until="networkidle"
        )
        card = page.locator(".identity-health-card")
        card.scroll_into_view_if_needed()
        capture("11j-mapping-duplicate-actions.png")
        review = card.get_by_role("link", name="Review repeated source identities")
        review.focus()
        review.press("Enter")
        expect(page.get_by_role("dialog", name="Compare duplicate rows")).to_be_visible()
        expect(page.get_by_role("dialog").get_by_role("alert")).to_contain_text("unique source identifier")
        page.get_by_role("button", name="Close duplicate review").click()
        page.goto(base + review.get_attribute("href"), wait_until="networkidle")
        expect(
            page.get_by_role("heading", name="Review duplicate identity groups")
        ).to_be_visible()
        expect(
            page.get_by_role("status").filter(has_text="group members")
        ).to_contain_text("of 105")
        expect(page.locator("tbody tr")).to_have_count(100)
        page.get_by_role("link", name="Open complete group").first.click()
        expect(page.get_by_role("link", name="Show all groups")).to_be_visible()
        page.get_by_role("link", name="Next 100").click()
        expect(page.locator("tbody tr")).to_have_count(5)
        expect(
            page.get_by_text("Included source position 105", exact=True)
        ).to_be_visible()
        page.get_by_role("link", name="Previous 100").click()
        expect(page.locator("tbody tr")).to_have_count(100)
        page.evaluate("window.scrollTo(0, 0)")
        capture("11k-identity-group-review.png")
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        region = page.get_by_role("region", name="Identity review rows")
        region.focus()
        region.press("ArrowRight")
        page.wait_for_timeout(300)
        self.assertGreater(region.evaluate("element => element.scrollLeft"), 0)
        self.assertNotIn("92100001", page.locator("body").inner_text())
        self.assertEqual(len(reads), 1)
        self.assertEqual(errors, [])
        self.assertEqual(
            fixture.context.matching_order._preferences.get_current_check(workspace_id),
            check,
        )
