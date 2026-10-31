"""Confirm a fictional company through the authenticated browser without rebuilding a project."""

import os
import sys
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_odoo_company as company_scenarios


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class OdooCompanyBrowserTests(unittest.TestCase):
    def test_choose_company_keeps_the_existing_project_and_saved_matches(self):
        fixture = company_scenarios.OdooCompanyWorkflowTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, access, draft, source, _schema = fixture.configured()
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None, headless=True
        )
        self.addCleanup(browser.close)
        browser_context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
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
        with patch(
            "impodo.web.routers.odoo_company.read_company_access", return_value=access
        ):
            page.goto(
                f"{base}/workspaces/{workspace_id}/target", wait_until="networkidle"
            )
            page.get_by_role("button", name="Choose Odoo company", exact=True).click()
            panel = page.locator("#odoo-operation-company")
            expect(panel).to_contain_text("Northwind Group")
            panel.get_by_label("Odoo company", exact=True).select_option("105")
            panel.get_by_role("button", name="Use this company", exact=True).click()
            expect(page.locator("[data-operation-company]")).to_contain_text(
                "Northwind Factory"
            )
            page.reload(wait_until="networkidle")
            expect(panel).to_contain_text("Northwind Factory")
            page.get_by_role("button", name="Choose Odoo company", exact=True).click()
            expect(panel.get_by_label("Odoo company", exact=True)).to_have_value("105")
            if capture := os.environ.get("IMPODO_COMPANY_SCREENSHOT"):
                panel.scroll_into_view_if_needed()
                page.evaluate("window.scrollBy(0, -80)")
                page.screenshot(path=capture)
        context = fixture.app.state.context
        self.assertEqual(context.queries.get_source_selection(workspace_id), source)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            draft,
        )
        self.assertEqual(
            context.queries.get(workspace_id).odoo_company, access.choices[1]
        )
        self.assertEqual(errors, [])
