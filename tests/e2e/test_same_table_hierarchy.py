"""Keep an existing parent/child table selectable and saved in the real browser."""

import os
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.same_table_hierarchy import CATEGORY_CSV, same_table_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class SameTableHierarchyBrowserTests(unittest.TestCase):
    def test_odoo19(self):
        self._check(19)

    def test_odoo20(self):
        self._check(20)

    def test_zero_root_recovery_odoo19(self):
        self._check(19, zero_root=True)

    def test_zero_root_recovery_odoo20(self):
        self._check(20, zero_root=True)

    def _check(self, major, *, zero_root=False):
        fixture = HierarchyWorkflowFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        content = CATEGORY_CSV.replace(b"5637144576,Materials,\n", b"5637144576,Materials,0\n") if zero_root else CATEGORY_CSV
        workspace_id, source = same_table_workspace(fixture, major=major, content=content)
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
        mapping = f"{base}/workspaces/{workspace_id}/mapping"
        parent_warning = page.get_by_text(
            "1 included record(s) refer to a parent", exact=False
        )
        if zero_root:
            page.goto(mapping + "?step=check", wait_until="networkidle")
            page.locator("#required-values-summary > summary").click()
            expect(parent_warning).to_be_visible()
        page.goto(mapping + "?step=tables&task=identity", wait_until="networkidle")

        def save_progress():
            with page.expect_response(
                lambda response: (
                    "/mapping/save" in response.url and response.request.method == "POST"
                )
            ) as saved:
                page.get_by_role("button", name="Save progress", exact=True).click()
            self.assertEqual(saved.value.status, 200, saved.value.text())

        columns = {column.source_name: column.stable_key for column in source.columns}
        expect(page.locator('[name="source_identity_0"]')).to_have_values(
            [columns["RecId"]]
        )
        expect(page.locator('[name="identity_source_0_0"]')).to_have_value(
            columns["Name"]
        )
        parent_source = page.locator('[name="identity_source_0_1"]')
        expect(parent_source).to_have_values([columns["ParentCategory"]])
        parent_table = page.locator('[name="identity_dataset_0_1"]')
        expect(
            parent_table.get_by_role(
                "option", name="categories (this table)", exact=True
            )
        ).to_have_count(1)
        parent_table.select_option("")
        parent_table.select_option(source.dataset_id)

        zero_checkbox = page.get_by_label(
            "A parent value of 0 means no parent", exact=False
        )
        if zero_root:
            origin = page.locator('[name="identity_origin_0_1"]')
            origin.select_option("target_then_dataset")
            expect(zero_checkbox).to_be_disabled()
            parent_source.select_option([])
            save_progress()
            page.reload(wait_until="networkidle")
            expect(parent_source).to_be_visible()
            expect(
                parent_source.locator("option", has_text="ParentCategory")
            ).to_have_count(1)
            expect(zero_checkbox).to_be_disabled()
            origin.select_option("dataset")
            expect(zero_checkbox).to_be_disabled()
            parent_source.select_option(columns["ParentCategory"])
            parent_table.select_option("")
            expect(zero_checkbox).to_be_disabled()
            parent_table.select_option(source.dataset_id)
            zero_checkbox.check()

        save_progress()
        page.reload(wait_until="networkidle")
        expect(page.locator('[name="identity_dataset_0_1"]')).to_have_value(
            source.dataset_id
        )
        expect(
            page.get_by_text("For a parent in this table", exact=False)
        ).to_be_visible()
        if zero_root:
            expect(zero_checkbox).to_be_checked()
            page.get_by_role("link", name="Check and resolve", exact=False).click()
            page.locator('[data-check-mapping]').click()
            page.wait_for_load_state("networkidle")
            expect(parent_warning).to_have_count(0)
            page.goto(mapping + "?step=tables&task=identity", wait_until="networkidle")
            expect(parent_source).to_have_values([columns["ParentCategory"]])
            expect(zero_checkbox).to_be_checked()
        self.assertEqual(errors, [])
        output = os.environ.get("IMPODO_SAME_TABLE_HIERARCHY_SCREENSHOT")
        if output and major == 20:
            page.locator('[name="identity_dataset_0_1"]').scroll_into_view_if_needed()
            page.screenshot(path=output)
