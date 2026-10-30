"""Prepare two combined outputs from an authenticated large fictional delivery."""

import os
import sys
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_mixed_join_preparation as fixtures
from tests.integration.web import test_join_lookup_preparation as lookup_fixtures


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class MixedPreparationBrowserTests(unittest.TestCase):
    def test_large_job_has_advisory_and_opens_review(self):
        self._exercise(fixtures.MixedJoinPreparationTests, 9506)

    def test_join_and_generated_list_keep_optional_blank_links_through_review(self):
        self._exercise(lookup_fixtures.JoinLookupPreparationTests, 4756)

    def _exercise(self, fixture_class, expected_ready):
        fixture = fixture_class(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        workspace_id, _ = fixture._setup()
        client = fixture.fixture.client
        session = client.cookies.get("impodo_session")
        client.close()
        server, thread, port = _start_server(fixture.fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None,
            headless=True,
        )
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        context.add_cookies(
            [
                dict(
                    name="impodo_session",
                    value=session,
                    url=base,
                    httpOnly=True,
                    sameSite="Strict",
                )
            ]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/workspaces/{workspace_id}/prepare", wait_until="networkidle")
        advisory = page.get_by_text(
            "Preparing 65,299 source rows across your selected tables.", exact=False
        )
        expect(advisory).to_be_visible()
        expect(
            page.get_by_text("Reduce the source size before preparing")
        ).to_have_count(0)
        capture = os.environ.get("IMPODO_MIXED_SCREENSHOT")
        if capture:
            page.get_by_role(
                "heading", name="Start preparation"
            ).scroll_into_view_if_needed()
            page.screenshot(path=str(Path(capture)))
        page.get_by_role("button", name="Prepare data", exact=True).click()
        page.wait_for_url("**/normalization", timeout=120000)
        expect(
            page.get_by_role("heading", name="Review what Impodo prepared")
        ).to_be_visible()
        self.assertEqual(errors, [])
        quality = fixture.context.quality.current_summary(workspace_id)
        self.assertEqual(quality.ready_count, expected_ready)
        self.assertEqual(quality.blocked_count, 0)
        page.get_by_role("button", name="Approve all prepared data", exact=True).click()
        page.locator("[data-normalization-approve-confirm]").click()
        page.wait_for_url("**/summary", timeout=30000)
        expect(
            page.get_by_role("heading", name="Compare the approved data with Odoo")
        ).to_be_visible()
