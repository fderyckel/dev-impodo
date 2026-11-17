"""Verify the shared action colours through native browser decisions and saves."""

import os
from pathlib import Path
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.e2e import test_stage2_matching_rules as scenarios
from tests.support.stage2_matching_rules import (
    matching_rule_form,
    representative_stage2_selections,
)


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class ButtonRoleBrowserTests(unittest.TestCase):
    def test_odoo19_action_roles(self):
        self.exercise_journey(19)

    def test_odoo20_action_roles(self):
        self.exercise_journey(20)

    def exercise_journey(self, major):
        fixture = scenarios.Stage2MatchingRuleJourneyTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace, schema = fixture._representative_workspace(odoo_major=major)
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            headless=True,
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL") or None,
        )
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        context.add_cookies(
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
        self.page = context.new_page()
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        url = f"{base}/workspaces/{workspace}/schema?step=matching"
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Confirm suggested matching rules and continue")
        expect(self.page.locator("[data-key-card-submit]")).to_have_count(0)
        self.check_narrow_layout(major)
        self.check_local_dialog()
        self.confirm_complete_fixture(fixture, workspace, schema)
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Continue to match data")
        self.edit_and_confirm(fixture, workspace, schema)
        self.assertEqual(errors, [])

    def assert_next(self, label=None):
        active = self.page.locator("[data-next-active]")
        expect(active).to_have_count(1 if label else 0)
        if label:
            expect(active).to_contain_text(label)
            self.assertEqual(
                active.evaluate("element => getComputedStyle(element).backgroundColor"),
                "rgb(201, 60, 53)",
            )

    def check_narrow_layout(self, major):
        for width in (1440, 390):
            self.page.set_viewport_size(
                {
                    "width": width,
                    "height": 1024 if width > 400 else 844,
                }
            )
            next_button = self.page.locator("[data-next-active]")
            next_button.scroll_into_view_if_needed()
            expect(next_button).to_be_in_viewport()
            bounds = next_button.bounding_box()
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertLessEqual(bounds["x"] + bounds["width"], width + 1)
            output = os.environ.get("IMPODO_BUTTON_SCREENSHOT_DIR")
            if output and major == 20:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                name = (
                    "08d-odoo-matching-next-action.png"
                    if width == 1440
                    else "button-roles-mobile.png"
                )
                self.page.screenshot(path=str(directory / name), animations="disabled")
        self.page.set_viewport_size({"width": 1440, "height": 1024})

    def check_local_dialog(self):
        dialog = self.page.locator("[data-read-credential-dialog]")
        dialog.evaluate("element => element.showModal()")
        self.assert_next()
        self.assertEqual(
            dialog.locator("button[type=submit]").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ),
            "rgb(255, 255, 255)",
        )
        dialog.evaluate("element => element.close()")
        self.assert_next("Confirm suggested matching rules and continue")

    def confirm_complete_fixture(self, fixture, workspace, schema):
        with self.page.expect_navigation(wait_until="networkidle"):
            self.page.evaluate(
                """({url, fields}) => {
                const form = document.createElement('form');
                form.method = 'post'; form.action = url;
                for (const [name, value] of Object.entries(fields)) {
                    const input = document.createElement('input');
                    input.type = 'hidden'; input.name = name; input.value = value;
                    form.append(input);
                }
                document.body.append(form); form.submit();
            }""",
                {
                    "url": f"/workspaces/{workspace}/schema/govern",
                    "fields": matching_rule_form(
                        fixture.csrf,
                        schema,
                        representative_stage2_selections(),
                    ),
                },
            )

    def edit_and_confirm(self, fixture, workspace, schema):
        card = self.page.locator("[data-key-decision]").first
        if not card.evaluate("element => element.open"):
            card.locator(":scope > summary").click()
        description = card.locator("[data-key-description]")
        index = int(description.get_attribute("name").rsplit("_", 1)[1])
        description.evaluate("""element => {
            for (let parent = element.parentElement; parent; parent = parent.parentElement)
                if (parent.tagName === 'DETAILS') parent.open = true;
        }""")
        description.fill("Reviewed UI matching rule")
        self.assert_next("Confirm matching rules and continue")
        with self.page.expect_navigation(wait_until="networkidle"):
            self.page.locator("[data-schema-confirm-matching]").click()
        self.assertIn("/mapping", self.page.url)
        governance = fixture.app.state.context.queries.get_schema_governance(workspace)
        saved = {key.model: key for key in governance.business_keys}
        self.assertEqual(set(saved), {model.name for model in schema.models})
        self.assertEqual(
            saved[schema.models[index].name].description, "Reviewed UI matching rule"
        )
