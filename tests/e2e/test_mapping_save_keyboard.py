"""Navigate save recovery by keyboard against an isolated authenticated editor."""

import unittest
from urllib.parse import urlsplit
from uuid import uuid4

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from tests.integration.web.test_mapping_command_performance import MappingCommandFixture
from tests.support.browser_scenarios import POST_HEADERS


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class MappingSaveKeyboardTests(unittest.TestCase):
    def test_missing_receipt_and_explicit_retry_by_keyboard(self):
        fixture = MappingCommandFixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace, _ = fixture.command_fixture(20)
        mapping = f"/workspaces/{workspace}/mapping"
        attempts, reads = [], []

        def serve(route):
            request = route.request
            url = urlsplit(request.url)
            path = url.path + ("?" + url.query if url.query else "")
            if url.path == mapping + "/save":
                attempts.append(request.post_data_json)
                if len(attempts) == 1:
                    route.abort("connectionreset")
                    return
                response = fixture.client.post(
                    path,
                    json=attempts[-1],
                    headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
                )
            else:
                if "/mutation-receipts/" in path:
                    reads.append(path)
                response = fixture.client.get(path, follow_redirects=False)
            content = response.content
            if url.path == mapping:
                content = content.replace(
                    b'data-mutation-receipt-wait-ms="180000"',
                    b'data-mutation-receipt-wait-ms="0"',
                )
            route.fulfill(
                status=response.status_code,
                body=content,
                content_type=response.headers.get("content-type", "text/plain"),
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.route("**/*", serve)
                page.goto("http://testserver" + mapping + "?step=check")
                save = page.get_by_role("button", name="Save progress", exact=True)
                save.focus()
                page.keyboard.press("Enter")
                alert = page.locator("[data-mapping-save-outcome]")
                expect(alert).to_have_attribute("role", "alert")
                expect(alert).to_be_visible()
                expect(alert).to_be_focused()
                expect(alert).to_contain_text("Impodo has no receipt yet")
                page.keyboard.press("Tab")
                expect(
                    page.get_by_role("button", name="Check save outcome")
                ).to_be_focused()
                page.keyboard.press("Enter")
                expect(alert).to_be_focused()
                page.keyboard.press("Tab")
                page.keyboard.press("Tab")
                expect(
                    page.get_by_role("button", name="Retry original action")
                ).to_be_focused()
                page.keyboard.press("Enter")
                expect(alert).to_be_hidden()
                expect(page.locator("[data-mapping-save-status]")).to_contain_text(
                    "Saved"
                )
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0], attempts[1])
                op = dict(attempts[0]["entries"])["operation_id"]
                self.assertEqual(reads, [f"{mapping}/mutation-receipts/{op}"] * 2)
                self.assertEqual(
                    fixture.app.state.context.queries.get_mapping_working_draft(
                        workspace
                    ).version,
                    2,
                )

                newer = fixture.changed_entries(
                    attempts[0]["entries"],
                    operation_id=str(uuid4()),
                    expected_working_draft_version="2",
                )
                advanced = fixture.post_command(workspace, newer)
                self.assertEqual(advanced.json()["expected_working_draft_version"], 3)
                save.focus()
                page.keyboard.press("Enter")
                expect(alert).to_be_visible()
                expect(alert).to_be_focused()
                page.keyboard.press("Tab")
                expect(page.get_by_role("button", name="Copy my edits")).to_be_focused()
                page.keyboard.press("Tab")
                expect(
                    page.get_by_role("button", name="Reload saved version")
                ).to_be_focused()
                page.keyboard.press("Enter")
                expect(
                    page.locator('input[name="expected_working_draft_version"]')
                ).to_have_value("3")
            finally:
                browser.close()
