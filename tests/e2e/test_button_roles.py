"""Verify the shared action colours through native browser decisions and saves."""

import os
import re
from pathlib import Path
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.e2e import test_stage2_matching_rules as scenarios
from tests.support.browser_scenarios import OdooConnectionMode
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

    def test_odoo19_local_action_roles(self):
        self.exercise_journey(19, OdooConnectionMode.LOCAL)

    def test_odoo20_local_action_roles(self):
        self.exercise_journey(20, OdooConnectionMode.LOCAL)

    def exercise_journey(self, major, mode=OdooConnectionMode.REMOTE):
        fixture = scenarios.Stage2MatchingRuleJourneyTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace, schema = fixture._representative_workspace(
            odoo_major=major, connection_mode=mode
        )
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
        self.page.set_default_timeout(15000)
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        url = f"{base}/workspaces/{workspace}/schema?step=matching"
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Confirm suggested matching rules and continue")
        expect(self.page.locator("[data-key-card-submit]")).to_have_count(0)
        self.check_narrow_layout(major)
        if mode is OdooConnectionMode.REMOTE:
            self.check_read_key_dialog()
        self.confirm_complete_fixture(fixture, workspace, schema)
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Continue to match data")
        self.revert_edits(fixture, workspace, major, mode)
        self.recover_rejected_draft(fixture, workspace, schema)
        self.edit_and_confirm(fixture, workspace, schema)
        self.page.goto(url, wait_until="networkidle")
        self.assert_next("Continue to match data")
        expect(self.page.locator("[data-key-description]").first).to_have_value(
            "Reviewed UI matching rule"
        )
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to match data")
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

    def check_read_key_dialog(self):
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

    def confirm_complete_fixture(self, fixture, workspace, schema, selections=None):
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
                        selections or representative_stage2_selections(),
                    ),
                },
            )

    def open_description(self, card):
        description = card.locator("[data-key-description]")
        description.evaluate("""element => {
            for (let parent = element.parentElement; parent; parent = parent.parentElement)
                if (parent.tagName === 'DETAILS') parent.open = true;
        }""")
        return description

    def revert_edits(self, fixture, workspace, major, mode):
        original = fixture.app.state.context.queries.get_schema_governance(workspace)
        cards = self.page.locator("[data-key-decision]")
        first, second = cards.nth(0), cards.nth(1)
        first_value = self.open_description(first).input_value()
        self.open_description(first).fill("Temporary first rule")
        second_value = self.open_description(second).input_value()
        self.open_description(second).fill("Temporary second rule")
        self.assert_next("Confirm matching rules and continue")
        self.open_description(first).fill(first_value)
        self.assert_next("Confirm matching rules and continue")
        self.open_description(second).fill(second_value)
        self.assert_next("Continue to match data")

        self.open_description(first)
        matching = first.locator("[data-primary-key-field]")
        saved_key = matching.input_value()
        alternative = matching.locator("option:enabled").evaluate_all(
            "(options, saved) => options.find(option => option.value && option.value !== saved).value",
            saved_key,
        )
        matching.select_option(alternative)
        self.assert_next("Confirm matching rules and continue")
        matching.select_option(saved_key)
        self.open_description(first).fill(first_value)
        self.assert_next("Continue to match data")

        first.locator("[data-scope-disclosure] > summary").click()
        first.locator("[data-scope-field]:enabled:not(:checked)").first.check()
        self.assert_next("Confirm matching rules and continue")
        first.locator("[data-scope-remove]").first.click()
        self.assert_next("Continue to match data")
        expect(first.locator("[data-schema-rule-summary]")).to_have_text(
            first.locator("[data-key-selection-summary]").inner_text()
        )
        expect(self.page.locator(".has-unconfirmed-changes")).to_have_count(0)
        expect(first.locator("[data-key-draft-selection]")).to_be_hidden()
        self.assertIsNone(
            self.page.locator("[data-schema-confirm-matching]").evaluate(
                "button => button.form.getAttribute('data-next-dirty')"
            )
        )
        self.assertEqual(
            fixture.app.state.context.queries.get_schema_governance(workspace), original
        )
        for card in cards.all():
            card.evaluate("element => { element.open = false; }")
        for width in (1440, 390):
            self.page.set_viewport_size(
                {"width": width, "height": 1024 if width == 1440 else 844}
            )
            active = self.page.locator("[data-next-active]")
            active.scroll_into_view_if_needed()
            expect(active).to_be_in_viewport()
            self.assert_next("Continue to match data")
            output = os.environ.get("IMPODO_BUTTON_SCREENSHOT_DIR")
            if (
                output
                and major == 20
                and mode is OdooConnectionMode.REMOTE
                and width == 1440
            ):
                self.page.screenshot(
                    path=str(Path(output) / "08g-odoo-matching-restored.png")
                )
        self.page.set_viewport_size({"width": 1440, "height": 1024})
        self.page.reload(wait_until="networkidle")
        self.assert_next("Continue to match data")

    def recover_rejected_draft(self, fixture, workspace, schema):
        original = fixture.app.state.context.queries.get_schema_governance(workspace)
        selections = representative_stage2_selections()
        selections["product.template"] = (
            ("x_legacy_code",),
            (),
            "Pending product rule",
        )
        selections["x.asset"] = (("code",), ("code",), "Conflicting scope")
        self.confirm_complete_fixture(fixture, workspace, schema, selections)
        expect(self.page.locator("[data-key-server-error]").first).to_be_visible()
        self.assert_next("Confirm matching rules and continue")
        product_index = next(
            index
            for index, model in enumerate(schema.models)
            if model.name == "product.template"
        )
        product = self.page.locator("[data-key-decision]").nth(product_index)
        description = self.open_description(product)
        description.fill("Edited pending product rule")
        description.fill("Pending product rule")
        expect(product).to_have_class(re.compile(r"\bhas-unconfirmed-changes\b"))
        self.assert_next("Confirm matching rules and continue")
        self.assertEqual(
            fixture.app.state.context.queries.get_schema_governance(workspace), original
        )
        self.page.goto(
            f"{self.page.url.split('/schema', 1)[0]}/schema?step=matching",
            wait_until="networkidle",
        )
        self.assert_next("Continue to match data")

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
