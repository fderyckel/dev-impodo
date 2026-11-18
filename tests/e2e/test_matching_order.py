"""Qualify the authenticated order panel with keyboard, pointer and a delayed check."""

import os
import sys
import unittest
from pathlib import Path
from threading import Event

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.matching_order_workspace import ordering_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class MatchingOrderBrowserTests(unittest.TestCase):
    def test_order_controls_and_refinement_keep_the_active_editor(self):
        f = HierarchyWorkflowFixture("runTest")
        f.setUp()
        self.addCleanup(f.tearDown)
        wid, reads = ordering_workspace(f, partial=True)
        server, thread, port = _start_server(f.app)
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
                    "value": f.client.cookies.get("impodo_session"),
                    "url": base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        page = context.new_page()
        url = (
            base + f"/workspaces/{wid}/mapping?step=tables&task=links&mapping_dataset=1"
        )
        page.goto(url)
        panel = page.get_by_role("region", name="Match supporting tables first")
        details = page.locator(".mapping-journey-order-details")
        details.locator("summary").press("Enter")
        rows = panel.locator("[data-matching-order-row]")
        original = rows.locator(".mapping-order-table strong").all_text_contents()
        self.assertEqual(original, ["categories", "products"])
        expect(panel).to_contain_text("Match after categories")
        self.capture(page, panel, "recommendation")
        toggle = panel.locator("[data-matching-order-toggle]")
        toggle.focus()
        toggle.press("Space")
        rows.first.get_by_role("button", name="Move down").press("Enter")
        expect(rows.first).to_contain_text("products")
        warning = panel.locator(".mapping-order-warnings")
        expect(warning).to_contain_text("categories first")
        self.capture(page, warning, "custom-order-warning")
        expect(toggle).to_be_checked()
        rows.last.drag_to(rows.first, target_position={"x": 20, "y": 4})
        expect(rows.first.locator("strong").first).to_have_text("categories")
        panel.get_by_role("button", name="Save table order").click()
        expect(rows.first.locator("strong").first).to_have_text("categories")
        toggle.press("Space")
        rows.first.get_by_role("button", name="Move down").press("Enter")
        expect(rows.first.locator("strong").first).to_have_text("products")
        self.assertEqual(reads, [])
        active = page.locator("[data-mapping-active-table]").get_attribute(
            "data-mapping-dataset"
        )
        entered, release = Event(), Event()
        reader = f.context.readiness_reader

        def delayed(*args):
            entered.set()
            if not release.wait(30):
                raise RuntimeError("Ordering browser did not release the reader")
            return reader(*args)

        f.context.readiness_reader = delayed
        try:
            panel.get_by_role(
                "button", name="Check identities and update suggestion"
            ).click()
            self.assertTrue(entered.wait(10))
            expect(panel.get_by_role("progressbar")).to_be_visible()
            page.reload()
            expect(panel.get_by_role("progressbar")).to_be_visible()
        finally:
            release.set()
        expect(panel.locator(".mapping-order-summary")).to_contain_text(
            "Partial", timeout=30000
        )
        expect(panel).to_contain_text("unchecked")
        self.capture(
            page, panel.locator(".mapping-order-live-check"), "partial-coverage"
        )
        apply = panel.get_by_role("button", name="Apply recommendation")
        expect(rows.first.locator("strong").first).to_have_text("products")
        apply.press("Enter")
        expect(rows.first.locator("strong").first).to_have_text("categories")
        expect(page.locator("[data-mapping-journey]")).to_have_attribute(
            "data-task", "links"
        )
        expect(page.locator("[data-mapping-active-table]")).to_have_attribute(
            "data-mapping-dataset", active
        )
        self.assertEqual(len(reads), 1)
        confirmation = page.get_by_text("Odoo-refined recommendation applied.", exact=True)
        expect(confirmation).to_be_visible()
        self.capture(page, confirmation, "apply-recommendation")

    def capture(self, page, target, name):
        output = os.environ.get("IMPODO_ORDER_SCREENSHOT_DIRECTORY")
        if output:
            target.scroll_into_view_if_needed()
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"stage3-order-{name}.png"
            page.screenshot(path=str(path))
            print(f"Captured {path.name}", flush=True)
