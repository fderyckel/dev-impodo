"""Confirm a fictional company through the authenticated browser without rebuilding a project."""

import os
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_odoo_company as company_scenarios
from tests.integration.web import test_matching_expectations as expectations_scenarios
from tests.support.recipe_lifecycle import metadata_for_schema
from impodo.adapters.odoo.local_stack import (
    LocalStackProfile,
    LocalStackStatus,
    LocalStackCheck,
    ReadinessLevel,
)
from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.domain.shared.models import OdooReadIdentity
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.identity import resolve_local_actor


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class OdooCompanyBrowserTests(unittest.TestCase):
    def test_expectations_save_and_reload_keep_zero_create_limit(self):
        fixture = expectations_scenarios.MatchingExpectationsTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, _, _, _ = fixture.setup_lists()
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(channel="msedge" if sys.platform == "win32" else None, headless=True)
        self.addCleanup(browser.close)
        browser_context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        browser_context.add_cookies([{"name": "impodo_session", "value": session, "url": base,
            "httpOnly": True, "sameSite": "Strict"}])
        page = browser_context.new_page()
        page.goto(f"{base}/workspaces/{workspace_id}/mapping?step=confirm", wait_until="networkidle")
        panel = page.locator("#business-expectations")
        self.assertFalse(panel.evaluate("element => element.open"))
        panel.locator(":scope > summary").click()
        self.assertTrue(panel.evaluate("element => element.open"))
        chain = panel.locator("fieldset.expectation-model").filter(has=page.locator('input[value="x.chain"]'))
        people = panel.locator("fieldset.expectation-model").filter(has=page.locator('input[value="x.person"]'))
        chain.get_by_label("Allowed changes", exact=True).select_option("none")
        chain.get_by_label("New records · maximum", exact=True).fill("0")
        chain.get_by_label("Changed records · maximum", exact=True).fill("0")
        people.get_by_label("Allowed changes", exact=True).select_option("upsert")
        panel.get_by_role("button", name="Save business expectations", exact=True).click()
        expect(panel).to_contain_text("Saved expectations v1")
        page.goto(f"{base}/workspaces/{workspace_id}/mapping?step=confirm", wait_until="networkidle")
        self.assertFalse(panel.evaluate("element => element.open"))
        panel.locator(":scope > summary").click()
        expect(chain.get_by_label("New records · maximum", exact=True)).to_have_value("0")
        expect(people.get_by_label("New records · maximum", exact=True)).to_have_value("")
        expect(chain.get_by_label("Allowed changes", exact=True)).to_have_value("none")
        if capture := os.environ.get("IMPODO_EXPECTATIONS_SCREENSHOT"):
            panel.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -80)")
            page.screenshot(path=capture)

    def test_choose_company_keeps_the_existing_project_and_saved_matches(self):
        fixture = company_scenarios.OdooCompanyWorkflowTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, access, draft, source, schema = fixture.configured()
        context = fixture.app.state.context
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        context.schema_workspace.discover_models(workspace_id,
            context.model_catalog_reader(state, "fixture-key"),
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_identity=OdooReadIdentity(target_hash=schema.connection_target_hash,
                principal_hash=schema.read_principal_hash, permission_hash=schema.read_permission_hash,
                context_hash=schema.read_context_hash, readable_models=("ir.model",),
                observed_at="2026-10-01T00:00:00Z"), actor=actor)
        owner = context.target_credential_workspace(workspace_id, workspace_state=state, actor=actor)
        credential = store_target_credential(fixture.secrets, owner, TargetCredentialRole.READ,
            "re-entered-checking-key", persistent=False)
        context.schema_read_session_factory = None
        context.schema_reader = lambda _state, _key: metadata_for_schema(schema)
        context.read_identity_probe = lambda state, _key, models: OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=state.odoo_company.context_hash,
            readable_models=tuple(models), observed_at="2026-10-01T00:00:00Z")
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
                f"{base}/workspaces/{workspace_id}/mapping", wait_until="networkidle"
            )
            page.get_by_role("button", name="Choose Odoo company", exact=True).click()
            panel = page.locator("#odoo-operation-company")
            expect(panel).to_contain_text("Northwind Group")
            panel.get_by_label("Odoo company", exact=True).select_option("105")
            panel.get_by_role("button", name="Use this company and continue", exact=True).click()
            expect(page.locator("[data-mapping-journey]")).to_have_attribute("data-step", "tables")
            page.locator('.sidebar-subnav-link[href*="step=company"]').click()
            expect(panel).to_be_visible()
            expect(panel).to_contain_text(
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
            page.locator('.sidebar-subnav-link[href*="step=tables"]').click()
            page.locator('[data-mapping-go-task="rows"]').click()
            page.locator('select[name="mode_0"]').select_option("create")
            page.locator('.sidebar-subnav-link[href*="step=check"]').click()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("button[data-check-mapping]").click()
            expect(page.locator("body")).not_to_contain_text("The checking key setup no longer matches")
            confirm = page.get_by_role("button", name="Confirm field matches", exact=True)
            expect(page.locator("[data-mapping-journey]")).to_have_attribute("data-step", "confirm")
            expect(confirm).to_be_enabled()
            expect(confirm).to_be_in_viewport()
            warnings = page.locator('input[name="warning_acknowledgement"]')
            next_control = warnings.first if warnings.count() else confirm
            expect(next_control).to_be_focused()
            self.assertEqual(page.locator('input[name="warning_acknowledgement"]:checked').count(), 0)
            page.reload(wait_until="networkidle")
            expect(next_control).to_be_focused()
            expect(next_control).to_be_in_viewport()
            page.locator('.sidebar-subnav-link[href*="step=company"]').click()
            expect(panel).to_be_visible()
            expect(panel).to_contain_text("Northwind Factory")
            catalogue_calls = len(fixture.model_catalog_calls)
            page.locator('.sidebar-subnav-link[href*="step=check"]').click()
            with page.expect_navigation(wait_until="networkidle"):
                page.locator("button[data-check-mapping]").click()
            expect(page.locator("[data-mapping-journey]")).to_have_attribute("data-step", "confirm")
            expect(confirm).to_be_enabled()
            self.assertEqual(len(fixture.model_catalog_calls), catalogue_calls)
        context = fixture.app.state.context
        self.assertEqual(context.queries.get_source_selection(workspace_id), source)
        revision = context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        self.assertEqual(revision.definition.datasets[0].mode.value, "create")
        self.assertEqual(
            context.queries.get(workspace_id).odoo_company, access.choices[1]
        )
        self.assertEqual(errors, [])
        checked = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertIsNone(checked.pending_refresh)
        self.assertEqual(checked.read_credential_binding_hash, credential.binding_hash)
        catalogue = context.queries.get_odoo_model_catalog(workspace_id)
        self.assertEqual(catalogue.read_credential_binding_hash, credential.binding_hash)
        self.assertEqual(catalogue.read_context_hash, access.choices[1].context_hash)


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class CompanyButtonRoleTests(unittest.TestCase):
    def test_odoo19_remote(self):
        self.exercise(19, OdooConnectionMode.REMOTE)

    def test_odoo20_remote(self):
        self.exercise(20, OdooConnectionMode.REMOTE)

    def test_odoo19_local(self):
        self.exercise(19, OdooConnectionMode.LOCAL)

    def test_odoo20_local(self):
        self.exercise(20, OdooConnectionMode.LOCAL)

    def assert_next(self, label):
        active = self.page.locator(".button[data-next-active]")
        expect(active).to_have_count(1)
        expect(active).to_have_text(label)
        expect(active).to_have_css("background-color", "rgb(201, 60, 53)")
        for width in (1440, 390):
            self.page.set_viewport_size(
                {"width": width, "height": 1024 if width == 1440 else 844}
            )
            active.scroll_into_view_if_needed()
            expect(active).to_be_in_viewport()
            bounds = active.bounding_box()
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertLessEqual(bounds["x"] + bounds["width"], width + 1)
        self.page.set_viewport_size({"width": 1440, "height": 1024})

    def local_access(self, fixture, workspace_id, access):
        state = fixture.app.state.context.queries.get(workspace_id)
        root = Path(fixture.temporary.name)
        profile = LocalStackProfile(
            root / "odoo.conf",
            root,
            "127.0.0.1",
            5432,
            "fixture",
            "127.0.0.1",
            8069,
            state.odoo_base_url,
            state.odoo_database,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        status = LocalStackStatus(
            str(profile.config_path),
            profile.base_url,
            profile.database_hint,
            (
                LocalStackCheck("odoo", "Odoo", ReadinessLevel.READY, "Fictional Odoo"),
                LocalStackCheck(
                    "api", "Metadata", ReadinessLevel.READY, "Fictional metadata"
                ),
            ),
            profile,
        )
        self.enterContext(
            patch.object(
                fixture.app.state.context.local_stack, "get", return_value=status
            )
        )
        reader = fixture.local_odoo_reader.get_company_access
        reader.return_value = access
        return reader

    def exercise(self, major, mode):
        fixture = company_scenarios.OdooCompanyWorkflowTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, access, draft, source, schema = fixture.configured(
            major=major, mode=mode
        )
        context = fixture.app.state.context
        if mode is OdooConnectionMode.LOCAL:
            reader = self.local_access(fixture, workspace_id, access)
        else:
            reader = self.enterContext(
                patch(
                    "impodo.web.routers.odoo_company.read_company_access",
                    return_value=access,
                )
            )
        before_reads = (len(fixture.schema_calls), len(fixture.readiness_calls))
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
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
        self.page = browser_context.new_page()
        self.page.set_default_timeout(15000)
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        url = f"{base}/workspaces/{workspace_id}/mapping?step=company"
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Choose Odoo company")
        reader.side_effect = ConnectorTransportError(
            "The company list is temporarily unavailable"
        )
        self.page.get_by_role("button", name="Choose Odoo company", exact=True).click()
        expect(self.page.locator("body")).to_contain_text(
            "The company list is temporarily unavailable"
        )
        self.assert_next("Choose Odoo company")
        self.assertIsNone(context.queries.get(workspace_id).odoo_company)
        reader.side_effect = None
        self.page.get_by_role("button", name="Choose Odoo company", exact=True).click()
        self.assert_next("Use this company and continue")
        select = self.page.get_by_label("Odoo company", exact=True)
        select.select_option("105")
        reader.return_value = replace(access, choices=(access.choices[0],))
        self.page.get_by_role(
            "button", name="Use this company and continue", exact=True
        ).click()
        expect(self.page.locator("body")).to_contain_text(
            "Choose a company available to this API user"
        )
        self.assert_next("Choose Odoo company")
        self.assertIsNone(context.queries.get(workspace_id).odoo_company)
        reader.return_value = access
        self.page.get_by_role("button", name="Choose Odoo company", exact=True).click()
        select.select_option("105")
        if (
            major == 20
            and mode is OdooConnectionMode.REMOTE
            and (output := os.environ.get("IMPODO_COMPANY_ACTION_SCREENSHOTS"))
        ):
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            self.page.locator("[data-next-active]").scroll_into_view_if_needed()
            self.page.screenshot(path=directory / "11q-stage3-choose-company.png")
        self.page.get_by_role(
            "button", name="Use this company and continue", exact=True
        ).click()
        expect(self.page.locator("[data-mapping-journey]")).to_have_attribute(
            "data-step", "tables"
        )
        self.assertEqual(
            context.queries.get(workspace_id).odoo_company, access.choices[1]
        )
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Continue to matching tables")
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to matching tables")
        self.page.get_by_role("button", name="Choose Odoo company", exact=True).click()
        expect(select).to_have_value("105")
        expect(self.page.locator("[data-mapping-journey]")).to_have_attribute(
            "data-step", "tables"
        )
        # Keep the existing lookup redirect, then reveal its retained company controls.
        self.page.evaluate("window.location.hash = 'odoo-operation-company'")
        expect(select).to_be_visible()
        self.assert_next("Continue to matching tables")
        check = self.page.get_by_role(
            "button", name="Check company-specific values", exact=True
        )
        expect(check).to_have_css("background-color", "rgb(255, 255, 255)")
        select.select_option("80")
        self.assert_next("Use this company and continue")
        select.select_option("105")
        self.assert_next("Continue to matching tables")
        self.assertEqual(
            context.queries.get(workspace_id).odoo_company, access.choices[1]
        )
        self.page.get_by_role(
            "link", name="Continue to matching tables", exact=True
        ).click()
        expect(self.page.locator("[data-mapping-journey]")).to_have_attribute(
            "data-step", "tables"
        )
        self.page.goto(url, wait_until="networkidle")
        self.page.get_by_role("button", name="Choose Odoo company", exact=True).click()
        self.page.evaluate("window.location.hash = 'odoo-operation-company'")
        expect(select).to_be_visible()
        select.select_option("80")
        self.page.get_by_role(
            "button", name="Use this company and continue", exact=True
        ).click()
        expect(self.page.locator("[data-mapping-journey]")).to_have_attribute(
            "data-step", "tables"
        )
        self.page.goto(url, wait_until="networkidle")
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to matching tables")
        self.assertEqual(
            context.queries.get(workspace_id).odoo_company, access.choices[0]
        )
        self.assertEqual(context.queries.get_source_selection(workspace_id), source)
        self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), schema)
        self.assertEqual(context.queries.get_mapping_working_draft(workspace_id), draft)
        self.assertEqual(
            (len(fixture.schema_calls), len(fixture.readiness_calls)), before_reads
        )
        self.assertEqual(errors, [])
