"""Choose one payable account and retain its company when the page reopens."""

import os
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.workspace.workbench import OdooConnectionMode
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_company_dependent_lookup as scenarios


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class CompanyDependentLookupBrowserTests(unittest.TestCase):
    def test_choose_save_and_reopen_payable_account_for_both_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self.exercise(major)

    def exercise(self, major):
        fixture = scenarios.CompanyDependentLookupTests(methodName="runTest")
        fixture.setUp()
        server = thread = None
        try:
            workspace_id, payload, schema = fixture.configure(
                major, OdooConnectionMode.REMOTE
            )
            context = fixture.app.state.context
            context.readiness_reader = fixture.reader(schema, [])
            session = fixture.client.cookies.get("impodo_session")
            fixture.client.close()
            server, thread, port = _start_server(fixture.app)
            base = f"http://127.0.0.1:{port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    browser_context = browser.new_context(
                        viewport={"width": 1440, "height": 1024}
                    )
                    browser_context.add_cookies(
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
                    page = browser_context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    url = f"{base}/workspaces/{workspace_id}/mapping?step=tables&task=links"
                    page.goto(url, wait_until="networkidle")
                    row = page.locator(
                        '[data-relation-mapping-row][data-target-field="property_account_payable_id"]'
                    )
                    row.evaluate(
                        "row => { for (let node = row; node; node = node.parentElement) if (node instanceof HTMLDetailsElement) node.open = true; }"
                    )
                    row.locator("[data-relation-value-source]").select_option(
                        "constant_existing"
                    )
                    expect(row.locator("[data-linked-company]")).to_contain_text(
                        "Example Wiltz"
                    )
                    key = row.locator("[data-constant-business-key]")
                    expect(
                        key.locator('option[data-scope-fields="company_id"]')
                    ).to_have_count(0)
                    key.select_option(payload["business_key_id"])
                    value = row.locator("[data-constant-component-value]").first
                    expect(
                        row.locator("[data-constant-component-label]").first
                    ).to_have_text("Exact code")
                    expect(
                        row.locator("[data-constant-scope-choice]:not([disabled])")
                    ).to_have_count(0)
                    value.fill("440000")
                    row.locator("[data-check-constant-record]").click()
                    choice = row.locator("[data-constant-existing-choice]")
                    expect(choice.locator('option[value="440000"]')).to_have_text(
                        "Suppliers (440000)"
                    )
                    row.locator("[data-constant-choice-search]").fill("suppliers")
                    choice.select_option("440000")
                    if major == 20 and (
                        capture := os.environ.get("IMPODO_PAYABLE_LOOKUP_SCREENSHOT")
                    ):
                        row.scroll_into_view_if_needed()
                        page.evaluate("window.scrollBy(0, -65)")
                        Path(capture).parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=capture)
                    page.locator('[name="source_identity_0"]').select_option(
                        "column:ref"
                    )
                    page.locator('[name="identity_source_0_0"]').select_option(
                        "column:ref"
                    )
                    with page.expect_response(
                        lambda response: response.url.endswith("/mapping/save")
                    ) as saved:
                        page.locator("[data-save-mapping-progress]").click()
                    self.assertEqual(saved.value.status, 200)
                    page.goto(url, wait_until="networkidle")
                    row = page.locator(
                        '[data-relation-mapping-row][data-target-field="property_account_payable_id"]'
                    )
                    row.evaluate(
                        "row => { for (let node = row; node; node = node.parentElement) if (node instanceof HTMLDetailsElement) node.open = true; }"
                    )
                    expect(
                        row.locator("[data-constant-component-value]").first
                    ).to_have_value("440000")
                    expect(row.locator("[data-linked-company]")).to_contain_text(
                        "Example Wiltz"
                    )
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            if server is not None:
                _stop_server(server, thread)
            fixture.tearDown()
