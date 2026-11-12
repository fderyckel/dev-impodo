"""Find and download the Stage 5 correction report in an authenticated browser."""

import os
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.hierarchy_cases import CATEGORY_ROWS
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


@skipUnless(sync_playwright, "Playwright is required for browser qualification")
class AttentionExportBrowserTests(TestCase):
    def test_correction_download_is_visible_and_works_before_comparison(self):
        fixture = HierarchyWorkflowFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        workspace_id, _ = fixture.hierarchy(
            major=20,
            mode="LOCAL",
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, ("C400", "", "")),
        )
        fixture.prepare(workspace_id)
        session = fixture.client.cookies.get("impodo_session")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, channel=os.environ.get("IMPODO_BROWSER_CHANNEL") or None
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1024})
            base = "http://testserver"
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

            def serve(route):
                # Exercise the real authenticated ASGI app without requiring a
                # listening socket in restricted verification environments.
                request = route.request
                url = urlsplit(request.url)
                if url.netloc != "testserver":
                    route.abort()
                    return
                response = fixture.client.request(
                    request.method,
                    url.path + ("?" + url.query if url.query else ""),
                    headers=dict(request.headers),
                    content=request.post_data_buffer,
                    follow_redirects=False,
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers={
                        key: value
                        for key, value in response.headers.items()
                        if key.lower()
                        not in {
                            "content-length",
                            "content-encoding",
                            "transfer-encoding",
                        }
                    },
                )

            page.route("**/*", serve)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(
                f"{base}/workspaces/{workspace_id}/summary", wait_until="networkidle"
            )
            action = page.get_by_role(
                "link", name="Download rows needing attention", exact=True
            )
            expect(action).to_be_visible()
            box = action.bounding_box()
            self.assertIsNotNone(box)
            self.assertLess(box["y"] + box["height"], 1024)
            output = os.environ.get("IMPODO_ATTENTION_SCREENSHOT")
            if output:
                Path(output).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=output, full_page=False)
            with page.expect_download() as download:
                action.click()
            self.assertTrue(
                download.value.suggested_filename.endswith("-needs-attention.xlsx")
            )
            self.assertIsNone(download.value.failure())
            self.assertEqual(errors, [])
            context.close()
            browser.close()
