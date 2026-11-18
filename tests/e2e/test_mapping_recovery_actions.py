"""Verify recovery action colours against real saved mapping receipts."""

import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.workspace.workbench import OdooConnectionMode
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.mapping_journey import MappingJourneyFixture


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class MappingRecoveryActionBrowserTests(unittest.TestCase):
    def test_odoo19_local(self):
        self.exercise_recovery(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise_recovery(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise_recovery(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise_recovery(20, OdooConnectionMode.REMOTE)

    def exercise_recovery(self, major, mode):
        fixture = MappingJourneyFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace, _, schema = fixture.configure(
            major,
            mode,
            files={
                "products": b"Reference,Product title,Category,Amount,Use\nP001,Workshop drill,Tools,12.50,yes\n"
            },
        )
        self.fixture, self.workspace = fixture, workspace
        sources = fixture.context.queries.get_source_selection(workspace)
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
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
            [{"name": "impodo_session", "value": session, "url": self.base}]
        )
        self.page = context.new_page()
        # Shorten only the test page's wait; keep the real save and receipt handlers.
        self.page.add_init_script(
            """document.addEventListener('DOMContentLoaded', () => {
                const form = document.querySelector('[data-mapping-form]');
                if (form) form.dataset.mutationReceiptWaitMs = '0';
            }, {capture: true, once: true});"""
        )
        self.page.set_default_timeout(15000)
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        self.page.on("dialog", lambda dialog: dialog.accept())
        self.url = f"{self.base}/workspaces/{workspace}/mapping"
        self.page.goto(self.url, wait_until="networkidle")
        company = self.page.locator("#odoo-operation-company")
        company.get_by_role("button", name="Choose Odoo company", exact=True).click()
        company.get_by_label("Odoo company", exact=True).select_option("105")
        company.get_by_role(
            "button", name="Use this company and continue", exact=True
        ).click()
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_function("window.impodoMappingEditor")
        target = self.page.locator(
            '[data-mapping-active-table] [data-mapping-task-panel="rows"]'
        ).get_by_role("combobox", name="Send these rows to")
        if target.input_value() != "x.item":
            with self.page.expect_navigation(wait_until="networkidle"):
                target.select_option("x.item")

        self.page.locator(
            '[data-mapping-active-table] [data-mapping-go-task="fields"]'
        ).click()
        fields = self.page.locator(
            '[data-mapping-active-table] [data-mapping-task-panel="fields"]'
        )
        fields.locator("[data-scalar-field-search]").fill("Internal note")
        fields.locator("[data-field-search-submit]").click()
        self.field = self.page.locator(
            '[data-mapping-active-table] [data-scalar-mapping-row][data-target-field="note"]'
        )
        self.field.locator("[data-value-source]").select_option("constant")
        self.literal = self.field.locator("[data-literal-value]")
        self.literal.fill("First reviewed name")
        self.assert_next("Save and continue")
        self.saved_responses = []
        self.page.route("**/mapping/save*", self.lose_save_response)
        self.page.route("**/mapping/mutation-receipts/*", self.unavailable_receipt)
        self.page.locator("[data-save-mapping-progress]").click()
        self.outcome = self.page.locator("[data-mapping-save-outcome]")
        expect(self.outcome).to_be_visible()
        expect(self.page.locator("[data-mapping-form]")).not_to_have_attribute(
            "aria-busy", "true"
        )
        self.assert_next("Check save outcome")
        self.assert_saved_note("First reviewed name")
        first_draft = fixture.context.queries.get_mapping_working_draft(workspace)
        self.literal.fill("Newer unsaved name")
        self.assert_next("Check save outcome")
        self.check_layout(major, mode)
        self.check_modal_priority()
        self.check_waiting_action()
        expect(self.literal).to_have_value("Newer unsaved name")
        self.assertEqual(
            fixture.context.queries.get_mapping_working_draft(workspace), first_draft
        )

        self.page.unroute("**/mapping/mutation-receipts/*")
        self.page.locator("[data-check-mapping-outcome]").click()
        expect(self.outcome).to_be_hidden()
        self.assert_next("Save and continue")
        expect(self.literal).to_have_value("Newer unsaved name")
        self.assert_saved_note("First reviewed name")
        self.page.unroute("**/mapping/save*")
        self.page.locator("[data-mapping-continue]").click()
        expect(self.page.locator("[data-mapping-journey]")).to_have_attribute(
            "data-task", "links"
        )
        self.assert_saved_note("Newer unsaved name")
        self.page.reload(wait_until="networkidle")
        self.assert_next("Save and review matches")

        self.check_version_conflict()
        self.assertEqual(
            fixture.context.queries.get_source_selection(workspace), sources
        )
        self.assertEqual(
            fixture.context.queries.get_odoo_schema_catalog(workspace), schema
        )
        self.assertIsNone(fixture.context.queries.get_mapping_revision(workspace))
        self.assertEqual(errors, [])

    def lose_save_response(self, route):
        response = route.fetch()
        self.assertEqual(response.status, 200, response.text())
        self.assertEqual(response.json()["status"], "committed")
        self.saved_responses.append(response.json())
        route.fulfill(status=200, content_type="application/json", body="{")

    def unavailable_receipt(self, route):
        route.fulfill(status=503, json={"detail": "Test receipt read interrupted"})

    def assert_next(self, label=None):
        active = self.page.locator("[data-next-active]")
        expect(active).to_have_count(1 if label else 0)
        if label:
            expect(active).to_have_text(label)
            self.assertIn(
                active.evaluate("el => getComputedStyle(el).backgroundColor"),
                {"rgb(201, 60, 53)", "rgb(169, 47, 41)"},
            )

    def assert_saved_note(self, value):
        draft = self.fixture.context.queries.get_mapping_working_draft(self.workspace)
        self.assertEqual(
            [
                field.literal_value
                for table in draft.definition.datasets
                for field in table.fields
                if field.target_field == "note"
            ],
            [value],
        )

    def check_layout(self, major, mode):
        for width, height in ((1440, 1024), (390, 844)):
            self.page.set_viewport_size({"width": width, "height": height})
            action = self.page.locator("[data-check-mapping-outcome]")
            action.scroll_into_view_if_needed()
            expect(action).to_be_in_viewport()
            bounds = action.bounding_box()
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertLessEqual(bounds["x"] + bounds["width"], width + 1)
            self.assert_next("Check save outcome")
            self.assertEqual(
                self.page.locator("[data-mapping-continue]").evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                ),
                "rgb(255, 255, 255)",
            )
            output = os.environ.get("IMPODO_MAPPING_RECOVERY_SCREENSHOTS")
            if output and major == 20 and mode is OdooConnectionMode.REMOTE:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(
                    path=str(directory / f"mapping-recovery-{width}.png"),
                    animations="disabled",
                )
        self.page.set_viewport_size({"width": 1440, "height": 1024})

    def check_modal_priority(self):
        dialog = self.page.locator("[data-read-credential-dialog]")
        if dialog.count():
            dialog.evaluate("el => el.showModal()")
            self.assert_next()
            dialog.evaluate("el => el.close()")
            self.assert_next("Check save outcome")

    def check_waiting_action(self):
        pending = []
        self.page.unroute("**/mapping/mutation-receipts/*")
        self.page.route(
            "**/mapping/mutation-receipts/*", lambda route: pending.append(route)
        )
        check = self.page.locator("[data-check-mapping-outcome]")
        check.click()
        expect(check).to_be_disabled()
        expect(check).to_have_text("Checking outcome...")
        self.assert_next()
        self.page.wait_for_function("true")
        self.assertEqual(len(pending), 1)
        self.unavailable_receipt(pending.pop())
        expect(check).to_be_enabled()
        self.assert_next("Check save outcome")

    def check_version_conflict(self):
        self.page.goto(
            f"{self.url}?step=tables&task=fields&scalar_page_size=10",
            wait_until="networkidle",
        )
        self.literal.fill("Keep these local edits")
        entries = self.page.locator("[data-mapping-form]").evaluate(
            "el => Array.from(new FormData(el).entries())"
        )
        literal_name = self.literal.get_attribute("name")
        entries = [
            [name, "Saved by another tab" if name == literal_name else value]
            for name, value in entries
            if name not in {"action", "operation_id"}
        ]
        entries.extend([["action", "save_progress"], ["operation_id", str(uuid4())]])
        response = self.page.request.post(
            f"{self.url}/save?step=tables&task=fields",
            data={"entries": entries},
            headers={
                "Origin": self.base,
                "X-CSRF-Token": self.page.locator(
                    '[data-mapping-form] input[name="csrf_token"]'
                ).input_value(),
            },
        )
        self.assertEqual(response.status, 200, response.text())
        self.assert_saved_note("Saved by another tab")
        saved = self.fixture.context.queries.get_mapping_working_draft(self.workspace)
        self.page.locator("[data-save-mapping-progress]").click()
        self.assert_next("Reload saved version")
        expect(self.literal).to_have_value("Keep these local edits")
        copy = self.page.locator("[data-copy-mapping-edits]")
        self.assertEqual(
            copy.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(255, 255, 255)",
        )
        self.page.evaluate(
            """() => Object.defineProperty(navigator, 'clipboard', {value: {
                writeText: async text => { window.copiedEdits = text; }
            }})"""
        )
        copy.click()
        expect(self.page.locator("[data-mapping-save-status]")).to_contain_text(
            "Edits copied"
        )
        copied = json.loads(self.page.evaluate("window.copiedEdits"))
        self.assertIn(
            "Keep these local edits", [value for _, value in copied["entries"]]
        )
        self.assert_next("Reload saved version")
        self.assertEqual(
            self.fixture.context.queries.get_mapping_working_draft(self.workspace),
            saved,
        )
        self.page.locator("[data-reload-saved-mapping]").click()
        expect(self.outcome).to_be_hidden()
        self.assert_next("Save and continue")
        expect(self.literal).to_have_value("Saved by another tab")
        self.assert_saved_note("Saved by another tab")
