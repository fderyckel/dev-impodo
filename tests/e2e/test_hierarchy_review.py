"""Show the actual four-record BoM consequence group in an authenticated browser."""

import os
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_bom import bom_workspace
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class HierarchyReviewBrowserTests(unittest.TestCase):
    def test_direct_cause_opens_the_complete_parent_and_sibling_group(self):
        fixture = HierarchyWorkflowFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, _, _ = bom_workspace(fixture)
        _, quality = fixture.prepare(workspace_id)
        page = fixture.context.quality.quality.get_quality_review_page(
            workspace_id, quality.run_id, include_run_cause_counts=True
        )
        cause = next(
            cause
            for cause in page.run_cause_counts
            if cause.reason_code == "INCOMING_RELATIONSHIP_MISSING"
        )
        session = fixture.client.cookies.get("impodo_session")
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
                    "value": session,
                    "url": base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(
            f"{base}/workspaces/{workspace_id}/summary/quality-causes/{cause.cause_key}",
            wait_until="networkidle",
        )
        expect(
            page.get_by_role("heading", name="Records behind this finding")
        ).to_be_visible()
        expect(page.get_by_text("Records 1-4 of 4", exact=True)).to_be_visible()
        expect(page.get_by_text("Direct finding", exact=True)).to_be_visible()
        expect(page.get_by_text("Affected parent", exact=True)).to_be_visible()
        expect(page.get_by_text("Affected sibling", exact=True)).to_have_count(2)
        self.assertEqual(errors, [])
        output = os.environ.get("IMPODO_HIERARCHY_REVIEW_SCREENSHOT")
        if output:
            page.screenshot(path=output)
