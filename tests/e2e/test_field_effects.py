"""Qualify field-effect links, edit freshness, and narrow browser layout."""

import os
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.field_effects import field_effects_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class FieldEffectsJourneyTests(unittest.TestCase):
    def test_exact_links_live_edit_freshness_and_catalog_search(self):
        fixture = ProjectSetupBrowserTestCase("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace, _dataset, _revision, _snapshot = field_effects_workspace(
            fixture, major=20, remote=True
        )
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(headless=True)
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
        url = base + f"/workspaces/{workspace}/mapping?step=tables&task=fields"
        page.goto(url, wait_until="networkidle")
        summary = page.locator("#scalar-0-field_0000 [data-field-effects]")
        expect(summary).to_be_visible()
        expect(summary).to_have_attribute("data-field-effects-state", "Current")
        expect(summary).to_contain_text("1 changed")
        summary.scroll_into_view_if_needed()
        output = os.environ.get("IMPODO_FIELD_EFFECTS_SCREENSHOT_DIRECTORY")
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(output) / "11p-field-rule-effects.png"))
        link = summary.get_by_role("link", name="Review this field’s rule effects")
        link.focus()
        link.press("Enter")
        expect(
            page.get_by_role("heading", name="Review rule effects", exact=True)
        ).to_be_visible()
        expect(page.locator('select[name="field"]')).to_have_value("field_0000")
        page.goto(url, wait_until="networkidle")
        edited_field = page.locator("#scalar-0-field_0001")
        edited_field.locator("details.mapping-value-preparation > summary").click()
        edited_field.locator("[data-value-rule-builder] > summary").click()
        edited_field.locator("[data-transform-case]").select_option("uppercase")
        expect(page.locator("[data-field-effects-state='Current']")).to_have_count(0)
        expect(
            page.locator("[data-field-effects-state='Needs refresh']")
        ).to_have_count(3)
        page.goto(url, wait_until="networkidle")
        search = page.locator("[data-scalar-field-search]")
        search.fill("field_0005")
        page.get_by_role("button", name="Search", exact=True).click()
        expect(
            page.locator("#scalar-0-field_0005 [data-field-effects]")
        ).to_contain_text("1 unchanged")
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertEqual(errors, [])
