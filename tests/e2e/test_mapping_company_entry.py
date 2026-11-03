"""Keep the load-company decision visible when entering Match data."""

from dataclasses import replace
import os
from pathlib import Path
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.workbench import OdooConnectionMode
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class MappingCompanyEntryBrowserTests(unittest.TestCase):
    def test_first_entry_return_from_odoo_data_and_explicit_company_anchor(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self.exercise_entry(major)

    def test_first_entry_with_saved_position_and_no_referrer(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.exercise_entry(major, suppress_referrer=True, mode=mode)

    def exercise_entry(
        self, major, *, suppress_referrer=False, mode=OdooConnectionMode.REMOTE
    ):
        fixture = ProjectSetupBrowserTestCase(methodName="runTest")
        fixture.setUp()
        server = thread = None
        try:
            workspace_id, dataset, key = fixture._mapping_ready_workspace(
                scalar_field_count=1,
                connection_mode=mode,
            )
            context = fixture.app.state.context
            actor = resolve_local_actor(context)
            schema = replace(
                context.queries.get_odoo_schema_catalog(workspace_id),
                odoo_version=f"{major}.0",
            )
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                workspace_id, schema, actor=actor
            )
            context.schema_workspace.govern_complete(
                workspace_id,
                business_keys=(key,),
                expected_catalog_hash=schema.content_hash,
                actor=actor,
            )
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
                    page_errors = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    mapping_path = f"/workspaces/{workspace_id}/mapping"
                    schema_url = f"{base}/workspaces/{workspace_id}/schema"
                    page.goto(schema_url, wait_until="networkidle")
                    if suppress_referrer:
                        page.evaluate(
                            """({path, datasetId}) => {
                                sessionStorage.setItem(`impodo.mapping.position:${path}`, JSON.stringify({
                                    targetField: 'field_0000', datasetId, targetOffset: 180,
                                    scrollY: 2500, horizontal: [], focusName: '', focusValue: ''
                                }));
                            }""",
                            {"path": mapping_path, "datasetId": dataset.dataset_id},
                        )
                    if suppress_referrer:
                        link = page.get_by_role(
                            "link", name="Continue to match data", exact=True
                        )
                        link.evaluate(
                            "element => element.setAttribute('rel', 'noreferrer')"
                        )
                        link.click()
                    else:
                        page.get_by_role(
                            "button", name="Confirm matching rules", exact=True
                        ).click()
                    page.wait_for_url(f"{base}{mapping_path}")
                    page.wait_for_load_state("networkidle")
                    if suppress_referrer:
                        self.assertEqual(page.evaluate("document.referrer"), "")
                    company = page.locator("#odoo-operation-company")
                    expect(company).to_be_visible()
                    expect(
                        company.get_by_role("heading", name="Company for this load")
                    ).to_be_in_viewport()
                    choice_button = company.get_by_role(
                        "button", name="Choose Odoo company"
                    )
                    expect(choice_button).to_be_in_viewport()
                    expect(choice_button).to_be_focused()
                    expect(company).to_contain_text("Choose a company before loading")
                    self.assertIsNone(
                        context.queries.get_mapping_working_draft(workspace_id)
                    )
                    if major == 20 and (
                        capture := os.environ.get("IMPODO_COMPANY_ENTRY_SCREENSHOT")
                    ):
                        Path(capture).parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=capture)

                    def remember_field():
                        page.evaluate(
                            """({path, datasetId}) => {
                            sessionStorage.setItem(`impodo.mapping.position:${path}`, JSON.stringify({
                                targetField: 'field_0000', datasetId, targetOffset: 180,
                                scrollY: 2500, horizontal: [], focusName: '', focusValue: ''
                            }));
                        }""",
                            {"path": mapping_path, "datasetId": dataset.dataset_id},
                        )

                    remember_field()
                    page.goto(schema_url, wait_until="networkidle")
                    page.get_by_role(
                        "link", name="Continue to match data", exact=True
                    ).click()
                    page.wait_for_url(f"{base}{mapping_path}")
                    page.wait_for_timeout(150)
                    expect(
                        company.get_by_role("heading", name="Company for this load")
                    ).to_be_in_viewport()

                    state = context.queries.get(workspace_id)
                    context.workspace_states.select_odoo_company(
                        workspace_id,
                        actor=actor,
                        expected_revision=state.revision,
                        selection=OdooCompanySelection(
                            schema.connection_target_hash, 105, "Example Factory"
                        ),
                    )
                    remember_field()
                    page.goto(schema_url, wait_until="networkidle")
                    page.goto(
                        f"{base}{mapping_path}#odoo-operation-company",
                        wait_until="networkidle",
                    )
                    page.wait_for_timeout(150)
                    expect(
                        company.get_by_role("heading", name="Company for this load")
                    ).to_be_in_viewport()
                    expect(company).to_contain_text("Example Factory")

                    remember_field()
                    page.goto(f"{base}/projects", wait_until="networkidle")
                    page.goto(f"{base}{mapping_path}", wait_until="networkidle")
                    page.wait_for_timeout(150)
                    self.assertGreater(page.evaluate("window.scrollY"), 500)
                    self.assertEqual(page_errors, [])
                finally:
                    browser.close()
        finally:
            if server is not None:
                _stop_server(server, thread)
            fixture.tearDown()
