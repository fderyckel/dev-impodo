"""Exercise the Related values action handoff through real save and conflict routes."""

import os
import re
from dataclasses import replace
from pathlib import Path
from unittest import skipUnless
from uuid import uuid4

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.supporting_review import SupportingChoice
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support import browser_scenarios as scenarios
from tests.support.identity import resolve_local_actor


@skipUnless(sync_playwright, "Playwright is required for browser checks")
class RelatedValueButtonTests(scenarios.ProjectSetupBrowserTestCase):
    def test_odoo19_local(self):
        self.exercise(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise(20, OdooConnectionMode.REMOTE)

    def workspace(self, major, mode, *, required=True):
        workspace_id, _, _ = self._mapping_ready_workspace(
            scalar_field_count=1, odoo_major=major, connection_mode=mode
        )
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        country = SchemaField(
            "country_id",
            "Country",
            "many2one",
            required,
            False,
            "res.country",
            None,
            (),
        )
        birth = replace(
            country, name="birth_country_id", label="Country of birth", required=False
        )
        schema = replace(
            schema,
            content_hash="sha256:" + "9" * 64,
            models=(
                replace(
                    schema.models[0],
                    label="Contact",
                    fields=(*schema.models[0].fields, country, birth),
                ),
            ),
        )
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=self.actor
        )
        state = self.context.queries.get(workspace_id)
        catalogue = scenarios._browser_model_catalog(state)
        country_record = TargetRecord(
            "ir.model",
            500,
            {
                "name": "Country",
                "model": "res.country",
                "abstract": False,
                "transient": False,
                "modules": "base",
                "state": "base",
            },
        )
        catalogue = replace(
            catalogue,
            fingerprint=replace(
                catalogue.fingerprint,
                odoo_version=f"{major}.0",
                module_versions={"base": f"{major}.0.1.0"},
            ),
            records={"ir.model": (*catalogue.records["ir.model"], country_record)},
        )
        self.context.schema_workspace.discover_models(
            workspace_id,
            catalogue,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_identity=self.context.read_identity_probe(
                state, "read-secret", ("ir.model",)
            ),
            actor=self.actor,
        )
        return workspace_id

    def start_browser(self):
        session = self.client.cookies.get("impodo_session")
        self.client.close()
        server, thread, port = _start_server(self.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            headless=True, channel=os.environ.get("IMPODO_BROWSER_CHANNEL") or None
        )
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 1024})
        self.base = f"http://127.0.0.1:{port}"
        context.add_cookies(
            [
                {
                    "name": "impodo_session",
                    "value": session,
                    "url": self.base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        self.page = context.new_page()
        self.page.set_default_timeout(15000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

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

    def choice(self, label):
        return self.page.get_by_role(
            "group",
            name=re.compile(
                rf"^Contact\s*·\s*{re.escape(label)}\s+(?:Required|Optional)$"
            ),
        ).get_by_role("combobox", name="Where should this value come from?")

    def current(self):
        return self.context.schema_workspace.supporting.current(
            self.workspace_id, actor=self.actor
        )

    def connector_calls(self):
        return tuple(
            map(
                len,
                (
                    self.connection_calls,
                    self.model_catalog_calls,
                    self.schema_calls,
                    self.read_identity_calls,
                    self.readiness_calls,
                ),
            )
        )

    def exercise(self, major, mode):
        self.workspace_id = self.workspace(major, mode)
        self.optional_workspace_id = self.workspace(major, mode, required=False)
        selection = self.context.queries.get_source_selection(self.workspace_id)
        before_calls = self.connector_calls()
        self.start_browser()
        self.page.goto(
            f"{self.base}/workspaces/{self.workspace_id}/schema?step=related",
            wait_until="networkidle",
        )
        self.assert_next("Save choices")
        self.choice("Country").select_option("REUSE")
        self.assert_next("Save choices")
        self.page.get_by_role("button", name="Save choices", exact=True).click()
        self.assert_next("Continue to matching rules")
        self.assertEqual(self.current().version, 1)
        check = self.page.get_by_role(
            "button", name="Check existing Odoo values", exact=True
        )
        expect(check).to_have_css("background-color", "rgb(255, 255, 255)")
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to matching rules")
        self.page.locator(".supporting-review-optional > summary").click()
        birth = self.choice("Country of birth")
        birth.select_option("SKIP_OPTIONAL")
        self.assert_next("Save choices")
        birth.select_option("")
        self.assert_next("Continue to matching rules")
        self.page.locator(".supporting-review-optional > summary").click()
        self.assert_next("Continue to matching rules")
        output = os.environ.get("IMPODO_RELATED_BUTTON_SCREENSHOTS")
        if output and (major, mode) == (20, OdooConnectionMode.REMOTE):
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            self.page.locator("[data-next-active]").scroll_into_view_if_needed()
            self.page.screenshot(path=directory / "08f-odoo-related-values.png")
        self.recover_stale_save()
        self.page.get_by_role(
            "link", name="Continue to matching rules", exact=True
        ).click()
        expect(self.page.locator('[data-schema-step-panel="matching"]')).to_be_visible()
        self.assertEqual(
            self.context.queries.get_source_selection(self.workspace_id), selection
        )
        self.check_optional_entry()
        self.assertEqual(self.connector_calls(), before_calls)
        self.assertEqual(self.errors, [])

    def recover_stale_save(self):
        self.page.locator(".supporting-review-optional > summary").click()
        birth = self.choice("Country of birth")
        birth.select_option("SKIP_OPTIONAL")
        service = self.context.schema_workspace.supporting
        packet = self.context.schema_workspace.supporting_review_inputs(
            self.workspace_id, actor=self.actor
        )
        run_local_operation_sync(
            service.save,
            packet,
            expected_binding_hash=packet.binding_hash,
            expected_version=1,
            operation_id=str(uuid4()),
            choices=(
                SupportingChoice("res.partner", "country_id", "REUSE"),
                SupportingChoice("res.partner", "birth_country_id", "REUSE"),
            ),
            actor=self.actor,
        )
        self.assertEqual(self.current().version, 2)
        self.page.get_by_role("button", name="Save choices", exact=True).click()
        expect(self.page.locator("[data-supporting-save-error]")).to_be_visible()
        self.page.locator(".supporting-review-optional > summary").click()
        expect(self.choice("Country of birth")).to_have_value("SKIP_OPTIONAL")
        self.assert_next("Save choices")
        self.assertEqual(self.current().version, 2)
        self.page.get_by_role("button", name="Save choices", exact=True).click()
        self.assert_next("Continue to matching rules")
        self.assertEqual(self.current().version, 3)
        self.assertIn(
            ("birth_country_id", "SKIP_OPTIONAL"),
            {
                (choice.field_name, choice.action.value)
                for choice in self.current().choices
            },
        )
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to matching rules")

    def check_optional_entry(self):
        self.page.goto(
            f"{self.base}/workspaces/{self.optional_workspace_id}/schema?step=related",
            wait_until="networkidle",
        )
        self.assert_next("Continue to matching rules")
        expect(
            self.page.get_by_role("button", name="Save choices", exact=True)
        ).to_have_css("background-color", "rgb(255, 255, 255)")
        self.page.locator(".supporting-review-optional > summary").click()
        country = self.choice("Country")
        country.select_option("REUSE")
        self.assert_next("Save choices")
        country.select_option("")
        self.assert_next("Continue to matching rules")
        self.assertIsNone(
            self.context.schema_workspace.supporting.current(
                self.optional_workspace_id, actor=self.actor
            )
        )
