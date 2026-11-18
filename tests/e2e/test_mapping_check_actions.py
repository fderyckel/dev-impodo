"""Follow formula corrections through the unchanged Stage 3 save and check flow."""

import os
import unittest
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.mapping_journey import MappingJourneyFixture


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class MappingCheckActionBrowserTests(unittest.TestCase):
    def test_odoo19_local(self):
        self.exercise_check(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise_check(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise_check(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise_check(20, OdooConnectionMode.REMOTE)

    def exercise_check(self, major, mode):
        fixture = MappingJourneyFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.fixture = fixture
        self.major, self.mode = major, mode
        workspace, selection, schema = fixture.configure(
            major,
            mode,
            files={
                "products": b"Reference,Product title,Category,Amount,Use\nP001,Workshop drill,Tools,12.50,yes\n"
            },
        )
        self.workspace = workspace
        self.seed_draft(selection.datasets[0])
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
        self.page.set_default_timeout(15000)
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        self.page.on("dialog", lambda dialog: dialog.accept())
        self.url = f"{self.base}/workspaces/{workspace}/mapping"
        self.root = self.page.locator("[data-mapping-journey]")
        self.summary = self.page.locator("[data-formula-issue-summary]")
        self.check = self.page.locator("[data-check-mapping]")
        self.review = self.page.locator("[data-review-mapping-confirmation]")

        self.page.goto(f"{self.url}?step=check", wait_until="networkidle")
        self.assert_next("Check matches")
        self.check.click()
        expect(self.root).to_have_attribute("data-step", "confirm")
        checked = fixture.context.queries.get_mapping_revision(workspace)
        self.assertIsNotNone(checked)
        self.page.goto(f"{self.url}?step=check", wait_until="networkidle")
        self.assert_next("Review confirmation")

        self.page.goto(
            f"{self.url}?step=tables&task=fields&scalar_page_size=10",
            wait_until="networkidle",
        )
        self.row = self.page.locator(
            '[data-mapping-active-table] [data-scalar-mapping-row][data-target-field="note"]'
        )
        self.literal = self.row.locator("[data-literal-value]")
        self.open_formula()
        self.formula = self.row.locator("[data-rule-formula]")
        before = fixture.context.queries.get_mapping_working_draft(workspace)
        self.literal.fill("Latest unsaved note")
        self.formula.fill('value 1= "UNI"')
        self.formula.blur()
        expect(self.row.locator("[data-formula-feedback]")).to_contain_text("Must fix")
        expect(self.check).to_be_disabled()
        self.assert_next("Go to issue")
        self.assertEqual(
            fixture.context.queries.get_mapping_working_draft(workspace), before
        )
        self.go_check()
        self.assert_next("Go to issue")
        self.assert_supporting(self.page.locator("[data-save-mapping-progress]"))
        self.assert_supporting(self.review)
        self.page.locator("[data-go-to-formula-issue]").click()
        expect(self.root).to_have_attribute("data-step", "tables")
        expect(self.root).to_have_attribute("data-task", "fields")
        expect(self.formula).to_be_focused()
        expect(self.literal).to_have_value("Latest unsaved note")

        with self.page.expect_response(
            lambda response: (
                "/mapping/save" in response.url and response.request.method == "POST"
            )
        ) as saved:
            self.page.locator("[data-save-mapping-progress]").click()
        self.assertEqual(saved.value.json()["status"], "committed")
        self.assert_saved_formula('value 1= "UNI"')
        self.capture_saved_formula()
        self.page.goto(f"{self.url}?step=check", wait_until="networkidle")
        self.assert_next("Go to issue")
        expect(self.check).to_be_disabled()
        self.check_layout(major, mode)
        self.check_modal()

        self.page.locator("[data-go-to-formula-issue]").click()
        expect(self.formula).to_be_focused()
        self.hold_formula_check()
        self.quick_check_failure()
        self.go_check()
        self.assert_next("Check matches")
        expect(self.summary).to_be_hidden()
        self.assert_supporting(self.review)
        self.check.click()
        expect(self.root).to_have_attribute("data-step", "confirm")
        revision = fixture.context.queries.get_mapping_revision(workspace)
        self.assertGreater(revision.version, checked.version)
        self.assert_saved_formula("value + '!'", revision=revision)
        self.page.goto(f"{self.url}?step=check", wait_until="networkidle")
        self.assert_next("Review confirmation")
        self.page.reload(wait_until="networkidle")
        self.assert_next("Review confirmation")
        self.assertEqual(
            fixture.context.queries.get_source_selection(workspace), sources
        )
        current_schema = fixture.context.queries.get_odoo_schema_catalog(workspace)
        self.assertEqual(current_schema.models, schema.models)
        self.assertEqual(current_schema.odoo_version, schema.odoo_version)
        self.assertIsNone(
            fixture.context.queries.get_mapping_submission(workspace, revision.version)
        )
        self.assertEqual(errors, [])

    def seed_draft(self, dataset):
        context = self.fixture.context
        state = context.queries.get(self.workspace)
        context.workspace_states.select_odoo_company(
            self.workspace,
            actor=self.fixture.actor,
            expected_revision=state.revision,
            selection=self.fixture.access.choices[1],
        )
        columns = {column.source_name: column.stable_key for column in dataset.columns}
        context.mapping_workspace.save_working_draft(
            self.workspace,
            actor=self.fixture.actor,
            expected_version=None,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="x.item",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(columns["Reference"],),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(columns["Reference"],),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping("name", columns["Product title"]),
                        ScalarFieldMapping(
                            "note",
                            value_source=ScalarValueSource.CONSTANT,
                            literal_value="Reviewed note",
                        ),
                    ),
                ),
            ),
        )

    def open_formula(self):
        for selector in (
            "details.mapping-value-preparation",
            "[data-value-rule-builder]",
            "[data-formula-control]",
        ):
            details = self.row.locator(selector)
            if details.get_attribute("open") is None:
                details.locator(":scope > summary").click()

    def go_check(self):
        self.page.locator('.sidebar-subnav-link[href*="step=check"]').click()
        expect(self.root).to_have_attribute("data-step", "check")

    def assert_next(self, label=None):
        active = self.page.locator("[data-next-active]")
        expect(active).to_have_count(1 if label else 0)
        if label:
            expect(active).to_have_text(label)
            self.assertIn(
                active.evaluate("el => getComputedStyle(el).backgroundColor"),
                {"rgb(201, 60, 53)", "rgb(169, 47, 41)"},
            )

    def assert_supporting(self, button):
        self.assertEqual(
            button.evaluate("el => getComputedStyle(el).backgroundColor"),
            "rgb(255, 255, 255)",
        )

    def assert_saved_formula(self, formula, *, revision=None):
        saved = revision or self.fixture.context.queries.get_mapping_working_draft(
            self.workspace
        )
        note = next(
            field
            for table in saved.definition.datasets
            for field in table.fields
            if field.target_field == "note"
        )
        self.assertEqual(note.transform.formula, formula)
        self.assertEqual(note.literal_value, "Latest unsaved note")

    def check_layout(self, major, mode):
        for width, height in ((1440, 1024), (390, 844)):
            self.page.set_viewport_size({"width": width, "height": height})
            action = self.page.locator("[data-go-to-formula-issue]")
            action.scroll_into_view_if_needed()
            expect(action).to_be_in_viewport()
            bounds = action.bounding_box()
            self.assertGreaterEqual(bounds["x"], 0)
            self.assertLessEqual(bounds["x"] + bounds["width"], width + 1)
            self.assert_next("Go to issue")
            output = os.environ.get("IMPODO_CHECK_ACTION_SCREENSHOTS")
            if output and major == 20 and mode is OdooConnectionMode.REMOTE:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(
                    path=str(directory / f"check-formula-{width}.png"),
                    animations="disabled",
                )
        self.page.set_viewport_size({"width": 1440, "height": 1024})

    def capture_saved_formula(self):
        output = os.environ.get("IMPODO_CHECK_ACTION_SCREENSHOTS")
        if output and self.major == 20 and self.mode is OdooConnectionMode.REMOTE:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            self.settle_layout()
            self.formula.evaluate(
                "el => el.scrollIntoView({block: 'center', behavior: 'instant'})"
            )
            self.settle_layout()
            expect(self.formula).to_be_in_viewport()
            expect(self.row.locator("[data-formula-feedback]")).to_be_in_viewport()
            expect(self.page.locator("[data-go-to-formula-issue]")).to_be_in_viewport()
            self.page.screenshot(
                path=str(directory / "11x-stage3-formula-recovery.png"),
                animations="disabled",
            )

    def settle_layout(self):
        self.assertTrue(
            self.page.evaluate("""async () => {
                await document.fonts.ready;
                let previous = null, stable = 0;
                for (let frame = 0; frame < 120; frame += 1) {
                    await new Promise(requestAnimationFrame);
                    const current = [scrollX, scrollY,
                        document.documentElement.scrollHeight].join(':');
                    stable = current === previous ? stable + 1 : 0;
                    previous = current;
                    if (stable >= 4) return true;
                }
                return false;
            }""")
        )

    def check_modal(self):
        dialog = self.page.locator("[data-read-credential-dialog]")
        if dialog.count():
            dialog.evaluate("el => el.showModal()")
            self.assert_next()
            dialog.evaluate("el => el.close()")
            self.assert_next("Go to issue")

    def hold_formula_check(self):
        pending = []
        self.page.route(
            "**/mapping/formula-validation", lambda route: pending.append(route)
        )
        self.formula.fill("value")
        self.formula.blur()
        expect(self.check).to_be_disabled()
        self.go_check()
        self.assert_next()
        self.assert_supporting(self.review)
        self.assertEqual(len(pending), 1)
        self.assert_saved_formula('value 1= "UNI"')
        request = pending.pop()
        request.fulfill(response=request.fetch())
        expect(self.check).to_be_enabled()
        self.assert_next("Check matches")
        self.page.unroute("**/mapping/formula-validation")

    def quick_check_failure(self):
        self.page.locator('[data-mapping-go-step="tables"]:visible').first.click()
        self.page.route(
            "**/mapping/formula-validation",
            lambda route: route.fulfill(
                status=503, json={"detail": "Test interruption"}
            ),
        )
        self.formula.fill("value + '!'")
        self.formula.blur()
        expect(self.row.locator("[data-formula-feedback]")).to_contain_text(
            "Could not check this formula"
        )
        expect(self.literal).to_have_value("Latest unsaved note")
        self.assert_saved_formula('value 1= "UNI"')
        self.page.unroute("**/mapping/formula-validation")
