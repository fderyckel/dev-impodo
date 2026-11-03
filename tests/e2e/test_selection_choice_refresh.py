"""Verify language refresh in authenticated fictional Stage 3 and Recipe screens."""

import json
import os
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy, DatasetMapping, IdentityComponentMapping,
    ScalarFieldMapping, ValueMapping,
)
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_selection_choice_refresh as stage_fixtures
from tests.integration.web import test_recipe_selection_refresh as recipe_fixtures
from tests.support.identity import resolve_local_actor


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class SelectionChoiceRefreshBrowserTests(unittest.TestCase):
    def browser(self, fixture, path):
        session = fixture.client.cookies.get("impodo_session")
        app = fixture.app if hasattr(fixture, "app") else fixture.fixture.app
        fixture.client.close()
        server, thread, port = _start_server(app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(channel="msedge" if sys.platform == "win32" else None, headless=True)
        self.addCleanup(browser.close)
        browser_context = browser.new_context(viewport={"width": 1440, "height": 1024}, locale="en-GB")
        browser_context.add_cookies([{"name": "impodo_session", "value": session,
            "url": f"http://127.0.0.1:{port}", "httpOnly": True, "sameSite": "Strict"}])
        page = browser_context.new_page()
        page.set_default_timeout(10_000)
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{port}{path}", wait_until="networkidle")
        return page

    def test_stage3_retains_edits_flags_removed_language_and_saves_current_list(self):
        self._stage3_refresh_and_repeated_save(stage_fixtures.SelectionChoiceRefreshTests)

    def test_odoo20_stage3_refresh_and_repeated_save(self):
        self._stage3_refresh_and_repeated_save(stage_fixtures.SelectionChoiceRefreshOdoo20Tests)

    def _stage3_refresh_and_repeated_save(self, fixture_type):
        fixture = fixture_type(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.configure()
        context, workspace_id = fixture.context, fixture.workspace_id
        old_schema = fixture.schema
        models = tuple(replace(model, fields=tuple(replace(field, name="lang", label="Language")
            if field.name == "field_0000" else field for field in model.fields)) for model in old_schema.models)
        schema = replace(old_schema, models=models)
        governance = context.queries.get_schema_governance(workspace_id)
        actor = resolve_local_actor(context)
        context.mapping_workspace.schemas.save_odoo_schema_catalog(workspace_id, schema, actor=actor)
        context.schema_workspace.govern(workspace_id, business_keys=governance.business_keys, actor=actor)
        mapping = DatasetMapping(dataset_id=fixture.dataset.dataset_id, target_model="res.partner",
            source_identity_column_keys=(fixture.dataset.columns[0].stable_key,),
            target_identity=(IdentityComponentMapping((fixture.dataset.columns[0].stable_key,), ("ref",)),),
            fields=(ScalarFieldMapping("lang", fixture.dataset.columns[1].stable_key,
                categorical_policy=CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
                value_mappings=(ValueMapping("fr", "fr_FR"), ValueMapping("de", "de_DE"))),))
        context.mapping_workspace.save_working_draft(workspace_id, datasets=(mapping,), expected_version=None, actor=actor)
        choices = [("fr_FR", "French / Français"), ("it_IT", "Italian / Italiano")]
        context.readiness_reader = lambda _state, _requests, _records: (
            stage_fixtures.selection_snapshot(schema, "res.partner", "lang", tuple(choices)), None)
        with patch("impodo.web.routers.mapping._source_value_choices", return_value=(
            {"value": "fr", "count": 12}, {"value": "de", "count": 4})):
            page = self.browser(fixture, f"/workspaces/{workspace_id}/mapping")
            row = page.locator('[data-scalar-mapping-row][data-target-field="lang"]')
            row.evaluate("row => { for (let node = row; node; node = node.parentElement) if (node.tagName === 'DETAILS') node.open = true; }")
            row.locator("[data-open-value-match]").click()
            dialog = page.locator("[data-value-match-dialog]")
            expect(dialog.get_by_label("Odoo choice for fr", exact=True)).to_have_value("fr_FR")
            expect(dialog.get_by_role("button", name="Refresh languages from Odoo", exact=True)).to_be_visible()
            with page.expect_response(lambda response: response.url.endswith("/mapping/value-choices") and response.request.method == "POST"):
                dialog.get_by_role("button", name="Refresh languages from Odoo", exact=True).click()
            removed = dialog.get_by_label("Odoo choice for de", exact=True)
            expect(removed).to_have_value("")
            expect(removed).to_have_attribute("aria-invalid", "true")
            expect(dialog.get_by_label("Odoo choice for fr", exact=True)).to_have_value("fr_FR")
            expect(dialog).to_contain_text("German — de_DE · unavailable in Odoo")
            expect(dialog.get_by_role("button", name="Use complete matches", exact=True)).to_have_count(0)
            capture = os.environ.get("IMPODO_LANGUAGE_REFRESH_SCREENSHOT")
            if capture:
                page.screenshot(path=capture)
            removed.focus()
            removed.select_option("it_IT")
            dialog.get_by_role("button", name="Use refreshed languages and matches", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/mapping/save") and response.request.method == "POST") as response:
                page.get_by_role("button", name="Save progress", exact=True).click()
            self.assertEqual(response.value.status, 200)
            # Save progress keeps this document open. Its accepted refresh must
            # not be submitted again against the newly saved draft version.
            with page.expect_response(lambda response: response.url.endswith("/mapping/save") and response.request.method == "POST") as repeated:
                page.get_by_role("button", name="Save progress", exact=True).click()
            self.assertEqual(repeated.value.status, 200, repeated.value.text())
            self.assertEqual(json.loads(page.locator('[name="selection_refreshes"]').input_value()), [])
            row.locator("[data-open-value-match]").click()
            expect(dialog.get_by_label("Odoo choice for de", exact=True)).to_have_value("it_IT")
            dialog.get_by_role("button", name="Use complete matches", exact=True).click()
            with page.expect_response(lambda response: response.url.endswith("/mapping/save") and response.request.method == "POST") as reopened:
                page.get_by_role("button", name="Save progress", exact=True).click()
            self.assertEqual(reopened.value.status, 200, reopened.value.text())
            page.reload(wait_until="networkidle")
            row.evaluate("row => { for (let node = row; node; node = node.parentElement) if (node.tagName === 'DETAILS') node.open = true; }")
            row.locator("[data-open-value-match]").click()
            expect(dialog.get_by_label("Odoo choice for de", exact=True)).to_have_value("it_IT")
            expect(dialog.get_by_label("Odoo choice for fr", exact=True)).to_have_value("fr_FR")
            self.assertEqual(self.errors, [])

    def test_recipe_refresh_keeps_unsaved_decision_and_supports_retry(self):
        fixture = recipe_fixtures.RecipeSelectionRefreshTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        page = self.browser(fixture.fixture.fixture, fixture.url)
        choice = page.locator('.target-match-form select[name^="match_"]')
        choice.select_option("en")
        page.get_by_role("button", name="Refresh languages from Odoo", exact=True).click()
        expect(page.locator('[data-selection-refresh-status]')).to_contain_text("Choices refreshed")
        expect(choice).to_have_value("en")
        self.assertTrue(json.loads(page.locator('[name="selection_refreshes"]').input_value()))
        fixture.context.readiness_reader = lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable"))
        # A controlled failed request leaves the current form and its edits intact.
        page.route("**/target-matches/refresh", lambda route: route.fulfill(status=422, json={"detail": "Connection unavailable. Try again."}))
        page.get_by_role("button", name="Refresh languages from Odoo", exact=True).click()
        expect(page.locator('[data-selection-refresh-status]')).to_contain_text("Connection unavailable")
        expect(choice).to_have_value("en")
        self.assertEqual(self.errors, [])
