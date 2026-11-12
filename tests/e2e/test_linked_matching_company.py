"""Keep unsaved identity choices while checking the company and retrying discovery."""

import unittest
from dataclasses import replace
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.workspace.workbench import OdooConnectionMode
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_linked_matching_fields as linked_fields


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class LinkedMatchingCompanyBrowserTests(unittest.TestCase):
    def test_choose_company_then_retry_discovery_and_reopen_saved_identity(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.exercise(major, mode)

    def exercise(self, major, mode):
        fixture = linked_fields.LinkedMatchingFieldTests(methodName="runTest")
        fixture.setUp()
        server = thread = None
        try:
            workspace_id, columns = fixture.configure_link(
                major,
                mode,
                "mrp.bom.line",
                "bom_id",
                "mrp.bom",
                "x_bom_reference",
                "Legacy BoM reference",
                choose_company=False,
            )
            fixture.local_odoo_reader.get_model_metadata.side_effect = (
                lambda state, profile, models: replace(
                    fixture.metadata,
                    models={name: fixture.metadata.models[name] for name in models},
                )
            )
            fixture.context.schema_workspace.govern(
                workspace_id,
                actor=fixture.actor,
                business_keys=(
                    BusinessKeyDefinition(
                        "line:reference",
                        "mrp.bom.line",
                        ("x_order",),
                        scope_fields=("bom_id",),
                        status=BusinessKeyStatus.CONFIRMED,
                    ),
                ),
            )
            session = fixture.client.cookies.get("impodo_session")
            fixture.client.close()
            server, thread, port = _start_server(fixture.app)
            base = f"http://127.0.0.1:{port}"
            mapping_url = f"{base}/workspaces/{workspace_id}/mapping"
            with (
                patch(
                    "impodo.web.routers.odoo_company.read_company_access",
                    return_value=fixture.access,
                ),
                sync_playwright() as playwright,
            ):
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
                    page.goto(mapping_url, wait_until="networkidle")
                    company = page.locator("#odoo-operation-company")
                    company.get_by_role(
                        "button", name="Choose Odoo company", exact=True
                    ).click()
                    company.get_by_label("Odoo company", exact=True).select_option("1")
                    company.get_by_role(
                        "button", name="Use this company and continue", exact=True
                    ).click()
                    page.wait_for_load_state("networkidle")
                    page.goto(
                        mapping_url + "?step=tables&task=identity",
                        wait_until="networkidle",
                    )
                    page.locator('[name="source_identity_0"]').select_option(
                        columns["Reference"]
                    )
                    page.locator('[name="identity_source_0_0"]').select_option(
                        columns["Reference"]
                    )
                    row = page.locator(
                        '[data-identity-matching-field][data-target-field="bom_id"]'
                    )
                    source = row.locator('[name="identity_source_0_1"]')
                    source.select_option(columns["ItemID"])
                    row.locator("[data-relationship-origin]").select_option(
                        "target_catalog"
                    )
                    key = row.locator("[data-linked-matching-key]")
                    previous_key = key.input_value()
                    status = row.locator("[data-linked-matching-field-status]")

                    def fail_once(route):
                        route.fulfill(
                            status=422,
                            content_type="application/json",
                            body='{"detail":"Odoo is temporarily unavailable. Retry."}',
                        )

                    page.route("**/mapping/linked-matching-fields", fail_once, times=1)
                    row.get_by_role(
                        "button", name="Load Odoo matching fields", exact=True
                    ).click()
                    expect(status).to_contain_text("temporarily unavailable")
                    expect(source).to_have_values([columns["ItemID"]])
                    expect(key).to_have_value(previous_key)
                    row.get_by_role(
                        "button", name="Retry loading Odoo matching fields", exact=True
                    ).click()
                    expect(
                        key.locator('option[value="odoo-field:x_bom_reference"]')
                    ).to_be_attached()
                    expect(source).to_have_values([columns["ItemID"]])
                    expect(key).to_have_value(previous_key)
                    expect(
                        row.get_by_role(
                            "button", name="Load Odoo matching fields", exact=True
                        )
                    ).to_be_enabled()
                    key.select_option("odoo-field:x_bom_reference")
                    with page.expect_response(
                        lambda response: (
                            "/mapping/save" in response.url
                            and response.request.method == "POST"
                        )
                    ):
                        page.locator("[data-save-mapping-progress]").click()
                    expect(page.locator("[data-mapping-save-status]")).to_contain_text(
                        "Saved"
                    )
                    page.reload(wait_until="networkidle")
                    expect(source).to_have_values([columns["ItemID"]])
                    expect(key).to_have_value("odoo-field:x_bom_reference")
                    state = fixture.context.queries.get(workspace_id)
                    schema = fixture.context.queries.get_odoo_schema_catalog(
                        workspace_id
                    )
                    self.assertEqual(state.odoo_company, fixture.access.choices[0])
                    self.assertEqual(
                        schema.read_context_hash, state.odoo_company.context_hash
                    )
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            if server is not None:
                _stop_server(server, thread)
            fixture.tearDown()
