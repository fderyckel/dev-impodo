"""Select and save one account parent using a typed exact business key."""

import os
from pathlib import Path
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.shared.models import TargetRecord
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_constant_record_lookup as scenarios


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class ConstantRecordLookupBrowserTests(unittest.TestCase):
    def test_select_and_save_parent_above_the_model_browse_limit_for_both_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self.exercise_parent(major)

    def exercise_parent(self, major):
        fixture = scenarios.ConstantRecordLookupTests(methodName="runTest")
        fixture.setUp()
        server = thread = None
        try:
            workspace_id, lookup_form, schema = fixture.configure(major)
            context = fixture.app.state.context
            exact = (
                TargetRecord(
                    "account.account", 1729, {"code": "401100", "name": "Customers"}
                ),
            )
            large = tuple(
                TargetRecord(
                    "account.account", i + 1, {"code": str(i), "name": "Other"}
                )
                for i in range(2_001)
            )
            context.readiness_reader = fixture.reader(schema, exact, large)
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
                    page.goto(
                        f"{base}/workspaces/{workspace_id}/mapping",
                        wait_until="networkidle",
                    )
                    parent = page.locator(
                        '[data-relation-mapping-row][data-target-field="parent_id"]'
                    )
                    parent.evaluate("""row => {
                        for (let node = row; node; node = node.parentElement) {
                            if (node instanceof HTMLDetailsElement) node.open = true;
                        }
                    }""")
                    parent.locator("[data-relation-value-source]").select_option(
                        "constant_existing"
                    )
                    parent.locator("[data-constant-business-key]").select_option(
                        lookup_form["business_key_id"]
                    )
                    parent.locator("[data-check-constant-record]").click()
                    expect(
                        parent.locator("[data-constant-choice-status]")
                    ).to_contain_text("Enter a matching value")
                    value = parent.locator("[data-constant-component-value]").first
                    value.fill("401100")
                    parent.locator("[data-check-constant-record]").click()
                    choice = parent.locator("[data-constant-existing-choice]")
                    expect(choice.locator('option[value="401100"]')).to_have_text(
                        "Customers (401100)"
                    )
                    choice.select_option("401100")
                    expect(value).to_have_value("401100")
                    if capture := os.environ.get("IMPODO_PARENT_LOOKUP_SCREENSHOT"):
                        if major == 20:
                            parent.scroll_into_view_if_needed()
                            page.evaluate("window.scrollBy(0, -65)")
                            Path(capture).parent.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=capture)
                    source = context.queries.get_mapping_source_selection(
                        workspace_id
                    ).datasets[0]
                    page.locator('[name="source_identity_0"]').select_option(
                        source.columns[0].stable_key
                    )
                    identity = page.locator('[name="identity_source_0_0"]')
                    identity.focus()
                    identity.select_option(source.columns[0].stable_key)
                    with page.expect_response(
                        lambda response: response.url.endswith("/mapping/save")
                    ) as saved:
                        page.locator("[data-save-mapping-progress]").click()
                    self.assertEqual(saved.value.status, 200)
                    draft = context.queries.get_mapping_working_draft(workspace_id)
                    self.assertEqual(
                        draft.definition.datasets[0]
                        .relationships[0]
                        .constant_reference.key_values[0]
                        .value,
                        "401100",
                    )
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            if server is not None:
                _stop_server(server, thread)
            fixture.tearDown()
