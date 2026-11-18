"""Verify Stage 2 button priority against saved choices and real recovery routes."""

import os
from dataclasses import replace
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


@skipUnless(sync_playwright, "Playwright is required for browser checks")
class RecordChoiceButtonTests(HierarchyWorkflowFixture):
    def test_odoo19_remote(self):
        self.remote_journey(19)

    def test_odoo20_remote(self):
        self.remote_journey(20)

    def test_odoo19_local(self):
        self.local_journey(19)

    def test_odoo20_local(self):
        self.local_journey(20)

    def fresh_workspace(self, major, mode):
        workspace_id = self.freeze_files({"contacts": b"Reference,Name\nC1,Aster\n"})
        state = self.context.queries.get(workspace_id)
        state = self.context.workspace_states.update_target(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode,
            odoo_base_url="https://odoo.example.test"
            if mode == "REMOTE"
            else "http://127.0.0.1:8069",
            odoo_database=f"odoo{major}_local",
            intended_applications=(),
            intended_models=(),
        )
        for name in ("schema_reader", "model_catalog_reader", "connection_tester"):
            reader = getattr(self.context, name)

            def qualified(*args, reader=reader):
                result = reader(*args)
                fingerprint = getattr(result, "fingerprint", result)
                fingerprint = replace(
                    fingerprint,
                    odoo_version=f"{major}.0",
                    module_versions={"base": f"{major}.0.1.0"},
                )
                return (
                    replace(result, fingerprint=fingerprint)
                    if hasattr(result, "fingerprint")
                    else fingerprint
                )

            setattr(self.context, name, qualified)
        if mode == "REMOTE":
            owner = self.context.target_credential_workspace(
                workspace_id, workspace_state=state, actor=self.actor
            )
            store_target_credential(
                self.secrets,
                owner,
                TargetCredentialRole.READ,
                "fictional-key",
                persistent=False,
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

    def records(self, workspace_id):
        self.page.goto(
            f"{self.base}/workspaces/{workspace_id}/schema?step=records",
            wait_until="networkidle",
        )

    def remote_journey(self, major):
        workspace_id = self.fresh_workspace(major, "REMOTE")
        selection = self.context.queries.get_source_selection(workspace_id)
        self.start_browser()
        self.records(workspace_id)
        self.assert_next("Show available Odoo data")
        self.page.get_by_role(
            "button", name="Show available Odoo data", exact=True
        ).click()
        self.assert_next("Save record types")
        picker = self.page.locator("[data-model-picker]")
        picker.locator('[name="permitted_models"][value="res.partner"]').check()
        self.page.get_by_role("button", name="Save record types", exact=True).click()
        expect(self.page.locator('[data-schema-step-panel="related"]')).to_be_visible()
        self.assertIn(
            "res.partner", self.context.queries.get(workspace_id).intended_models
        )
        self.records(workspace_id)
        self.assert_next("Continue to related values")
        self.edit_and_revert()
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to related values")
        output = os.environ.get("IMPODO_RECORD_BUTTON_SCREENSHOTS")
        if output and major == 20:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            self.page.locator("[data-next-active]").scroll_into_view_if_needed()
            self.page.screenshot(path=directory / "08e-odoo-record-choices.png")
        self.retry_failed_read(workspace_id)
        self.confirm_changed_details(workspace_id, major)
        self.assertEqual(
            self.context.queries.get_source_selection(workspace_id), selection
        )
        self.assertEqual(self.errors, [])

    def edit_and_revert(self):
        picker = self.page.locator("[data-model-picker]")
        extra = picker.locator('[name="permitted_models"][value="product.template"]')
        extra.check()
        self.assert_next("Save record types")
        extra.uncheck()
        self.assert_next("Continue to related values")
        picker.locator("[data-model-search]").fill("Contact")
        self.assert_next("Continue to related values")
        picker.locator("[data-model-search]").fill("")
        self.page.locator("details.schema-capture-details > summary").click()
        expect(
            self.page.get_by_role("button", name="Check for Odoo changes", exact=True)
        ).to_have_css("background-color", "rgb(255, 255, 255)")
        self.assert_next("Continue to related values")

    def retry_failed_read(self, workspace_id):
        previous = self.context.queries.get_odoo_schema_catalog(workspace_id)
        self.page.locator("details.schema-capture-details > summary").click()
        with patch.object(
            self.context,
            "schema_reader",
            side_effect=ConnectorTransportError("The field request failed"),
        ):
            self.page.get_by_role(
                "button", name="Check for Odoo changes", exact=True
            ).click()
            self.assert_next("Try loading details again")
        extra = self.page.locator('[name="permitted_models"][value="product.template"]')
        extra.check()
        self.assert_next("Try loading details again")
        extra.uncheck()
        self.assertEqual(
            self.context.queries.get_odoo_schema_catalog(workspace_id), previous
        )
        self.page.get_by_role(
            "button", name="Try loading details again", exact=True
        ).click()
        self.records(workspace_id)
        self.assert_next("Continue to related values")

    def confirm_changed_details(self, workspace_id, major):
        state = self.context.queries.get(workspace_id)
        snapshot = self.context.schema_reader(state, "fictional-key")
        model = snapshot.models["res.partner"]
        snapshot = replace(
            snapshot,
            fingerprint=replace(
                snapshot.fingerprint, module_versions={"base": f"{major}.0.2.0"}
            ),
            models={
                **snapshot.models,
                "res.partner": replace(
                    model,
                    fields={
                        **model.fields,
                        "name": replace(model.fields["name"], required=False),
                    },
                ),
            },
        )
        with patch.object(self.context, "schema_reader", return_value=snapshot):
            self.page.locator("details.schema-capture-details > summary").click()
            self.page.get_by_role(
                "button", name="Check for Odoo changes", exact=True
            ).click()
            self.assert_next("Use updated Odoo details")
        extra = self.page.locator('[name="permitted_models"][value="product.template"]')
        extra.check()
        self.assert_next("Use updated Odoo details")
        extra.uncheck()
        self.page.locator('[name="confirm_schema_refresh"][type="checkbox"]').check()
        self.page.get_by_role(
            "button", name="Use updated Odoo details", exact=True
        ).click()
        self.records(workspace_id)
        self.assertIsNone(
            self.context.queries.get_odoo_schema_catalog(workspace_id).pending_refresh
        )
        self.assert_next("Continue to related values")

    def local_journey(self, major):
        missing = self.fresh_workspace(major, "LOCAL")
        saved, _, _ = self._mapping_ready_workspace(
            scalar_field_count=1,
            odoo_major=major,
            connection_mode=OdooConnectionMode.LOCAL,
        )
        self.start_browser()
        self.records(missing)
        self.assert_next("Find local Odoo")
        expect(
            self.page.get_by_role("button", name="Show available Odoo data", exact=True)
        ).to_be_disabled()
        self.records(saved)
        self.assert_next("Continue to related values")
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to related values")
        self.assertEqual(self.errors, [])
