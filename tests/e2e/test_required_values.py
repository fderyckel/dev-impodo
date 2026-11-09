"""Qualify checklist correction links and the existing grouped-default journey.

IMPODO_REQUIRED_VALUES_SCREENSHOT_DIRECTORY captures two current fictional
1440 by 1024 images without a live Odoo connection.
"""

import os
import sys
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.required_values import required_values_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class RequiredValuesJourneyTests(unittest.TestCase):
    def test_checklist_keyboard_links_filters_and_grouped_default_recovery(self):
        fixture = ProjectSetupBrowserTestCase("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace, _dataset, _revision, _validation = required_values_workspace(fixture)
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
            output = os.environ.get("IMPODO_REQUIRED_VALUES_SCREENSHOT_DIRECTORY")
            if output:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / name))

        page.goto(base + f"/workspaces/{workspace}/mapping", wait_until="networkidle")
        page.get_by_role("link", name="Check and resolve", exact=False).click()
        page.locator("#required-values-summary > summary").click()
        capture("11n-mapping-required-values-entry.png")
        link = page.get_by_role("link", name="Review required values", exact=True)
        link.focus()
        link.press("Enter")
        expect(
            page.get_by_role("heading", name="Required values", exact=True)
        ).to_be_visible()
        expect(page.locator("[data-required-field]")).to_have_count(6)
        expect(page.locator("[data-required-field=field_0001]")).to_contain_text(
            "Fixed value"
        )
        expect(page.locator("[data-required-field=relation_0000]")).to_contain_text(
            "Odoo-managed"
        )
        expect(page.get_by_role("region", name="Table coverage rows")).to_contain_text(
            "Chosen · not checked"
        )
        page.locator("#required-checklist-title").evaluate(
            "element => element.scrollIntoView({block: 'start'})"
        )
        page.evaluate("window.scrollBy(0, -24)")
        capture("11o-required-values-checklist.png")
        page.locator("[data-required-field=field_0001]").get_by_role(
            "link", name="Review field decision"
        ).click()
        expect(page.locator("#scalar-0-field_0001")).to_be_visible()
        self.assertTrue(page.url.endswith("#scalar-0-field_0001"))
        page.locator("[data-scalar-field-search]").fill("Field 0003")
        page.locator("[data-field-search-submit]").click()
        expect(page.locator("#scalar-0-field_0003")).to_be_visible()
        self.assertNotIn("scalar_target=", page.url)
        page.goto(
            base + f"/workspaces/{workspace}/mapping/required-values",
            wait_until="networkidle",
        )
        page.get_by_role("combobox", name="Status", exact=True).select_option("missing")
        page.get_by_role("button", name="Apply filters").click()
        expect(page.locator("[data-required-field]")).to_have_count(2)
        page.get_by_role("link", name="Review checked defaults", exact=True).click()
        expect(page.locator("#next-step-blockers")).to_be_visible()
        page.get_by_role("button", name="Use 1 Odoo default", exact=True).click()
        expect(page.get_by_text("Review 1 Odoo default", exact=True)).to_have_count(0)
        page.goto(
            base + f"/workspaces/{workspace}/mapping/required-values?state=missing",
            wait_until="networkidle",
        )
        expect(page.locator("[data-required-field]")).to_have_count(1)
        expect(page.locator("[data-required-field=field_0003]")).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        region = page.get_by_role("region", name="Required field rows", exact=True)
        region.focus()
        region.press("ArrowRight")
        page.wait_for_timeout(300)
        self.assertGreater(region.evaluate("element => element.scrollLeft"), 0)
        self.assertEqual(errors, [])
