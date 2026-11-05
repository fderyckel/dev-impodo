"""Walk Stage 3 in an isolated authenticated browser with real saved decisions."""

import os
from pathlib import Path
import re
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.workspace.workbench import OdooConnectionMode
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.mapping_journey import MappingJourneyFixture


@unittest.skipUnless(sync_playwright, "Playwright is required for browser qualification")
class MappingJourneyBrowserTests(unittest.TestCase):
    """Verify Odoo 19/20 local and remote authoring without a live Odoo server."""

    def test_odoo19_local(self):
        self.exercise_journey(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote(self):
        self.exercise_journey(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local(self):
        self.exercise_journey(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote(self):
        self.exercise_journey(20, OdooConnectionMode.REMOTE)

    def exercise_journey(self, major, mode):
        fixture = MappingJourneyFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, selection, schema = fixture.configure(major, mode)
        self.fixture, self.workspace_id = fixture, workspace_id
        self.major, self.mode = major, mode
        self.source_before = fixture.context.queries.get_source_selection(workspace_id)
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            headless=True,
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL") or ("msedge" if sys.platform == "win32" else None),
        )
        self.addCleanup(browser.close)
        browser_context = browser.new_context(viewport={"width": 1440, "height": 1024})
        self.base = f"http://127.0.0.1:{port}"
        browser_context.add_cookies([{
            "name": "impodo_session", "value": session, "url": self.base,
            "httpOnly": True, "sameSite": "Strict",
        }])
        self.page = browser_context.new_page()
        self.page.set_default_timeout(15000)
        self.page_errors, self.console_errors = [], []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.on("console", lambda message: self.console_errors.append(message.text) if message.type == "error" else None)
        self.mapping_url = f"{self.base}/workspaces/{workspace_id}/mapping"
        self.root = self.page.locator("[data-mapping-journey]")

        self._enter_from_stage2()
        self._explicit_locations_without_company()
        self.page.goto(self.mapping_url, wait_until="networkidle")
        self._choose_company()
        self.assertEqual(fixture.context.queries.get(workspace_id).odoo_company, fixture.access.choices[1])
        self.assertIsNone(fixture.context.queries.get_mapping_working_draft(workspace_id))
        self.item_key = next(key for key in fixture.context.queries.get_schema_governance(
            workspace_id
        ).business_keys if key.model == "x.item" and key.key_fields == ("ref",))

        by_id = {dataset.dataset_id: dataset for dataset in selection.datasets}
        ordered = [by_id[dataset_id] for dataset_id in self.page.locator(
            "[data-matching-order-row]"
        ).evaluate_all("rows => rows.map(row => row.dataset.matchingOrderRow)")]
        self.assertEqual(len(ordered), len(selection.datasets))
        for position, dataset in enumerate(ordered):
            try:
                self._author_table(dataset, first=position == 0, last=position == len(ordered) - 1)
            except Exception:
                self._capture("walkthrough-failure.png")
                raise

        self._check_and_fix_required_name(ordered[0])
        self._confirm_rows_and_matches()
        self.assertEqual(fixture.context.queries.get_source_selection(workspace_id), self.source_before)
        self.assertEqual(fixture.context.queries.get_odoo_schema_catalog(workspace_id).odoo_version, f"{major}.0")
        self.assertEqual(schema.odoo_version, f"{major}.0")
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.console_errors, [])

    def _enter_from_stage2(self):
        self.page.goto(f"{self.base}/workspaces/{self.workspace_id}/schema", wait_until="networkidle")
        self.page.get_by_role("button", name="Confirm matching rules", exact=True).click()
        expect(self.root).to_have_attribute("data-step", "company")
        company = self.page.locator("#odoo-operation-company")
        expect(company.get_by_role("heading", name="Company for this load")).to_be_in_viewport()
        expect(company.get_by_role("button", name="Choose Odoo company", exact=True)).to_be_in_viewport()
        self._capture("11q-stage3-choose-company.png")

    def _explicit_locations_without_company(self):
        for step in ("tables", "check"):
            self.page.goto(f"{self.mapping_url}?step={step}", wait_until="networkidle")
            expect(self.root).to_have_attribute("data-step", step)
            self.page.reload(wait_until="networkidle")
            expect(self.root).to_have_attribute("data-step", step)

    def _choose_company(self):
        panel = self.page.locator("#odoo-operation-company")
        panel.get_by_role("button", name="Choose Odoo company", exact=True).click()
        choice = panel.get_by_label("Odoo company", exact=True)
        expect(choice).to_be_in_viewport()
        choice.select_option("105")
        panel.get_by_role("button", name="Use this company and continue", exact=True).click()
        expect(self.root).to_have_attribute("data-step", "tables")
        expect(self.root).to_have_attribute("data-task", "rows")
        expect(self.page.locator("[data-mapping-active-table]")).to_be_visible()

    def _active_table(self):
        return self.page.locator("[data-mapping-active-table]")

    def _task(self, key):
        return self._active_table().locator(f'[data-mapping-task-panel="{key}"]')

    def _go_task(self, key):
        self._active_table().locator(f'[data-mapping-go-task="{key}"]').click()
        expect(self.root).to_have_attribute("data-task", key)
        expect(self._task(key)).to_be_visible()

    def _save_continue(self, *, task=None, step=None):
        with self.page.expect_response(lambda response: "/mapping/save" in response.url and response.request.method == "POST") as pending:
            self.page.locator("[data-mapping-continue]").click()
        response = pending.value
        self.assertEqual(response.status, 200)
        operation_id = dict(response.request.post_data_json["entries"])["operation_id"]
        receipt = self.fixture.context.mapping_workspace.get_mutation_receipt(
            self.workspace_id, operation_id, actor=self.fixture.actor,
        )
        self.assertEqual(receipt.state.value, "COMMITTED")
        if task:
            expect(self.root).to_have_attribute("data-task", task)
        if step:
            expect(self.root).to_have_attribute("data-step", step)
        expect(self.page.locator("[data-mapping-form]")).not_to_have_attribute("aria-busy", "true")
        self._settle_layout()
        if task:
            expect(self._active_table().locator(":scope > .page-heading h2")).to_be_in_viewport()
            expect(self._active_table().locator(".mapping-task-nav")).to_be_in_viewport()
            expect(self._active_table().locator('[data-mapping-go-task][aria-current="step"]')).to_be_in_viewport()

    def _author_table(self, dataset, *, first, last):
        expect(self.root).to_have_attribute("data-step", "tables")
        expect(self.root).to_have_attribute("data-task", "rows")
        expect(self._active_table().get_by_role("heading", name=dataset.name, exact=True)).to_be_visible()
        target = self._task("rows").get_by_role("combobox", name=re.compile("Send these rows to"))
        if target.input_value() != "x.item":
            target.select_option("x.item")
            self.page.wait_for_load_state("networkidle")
            expect(self._task("rows").get_by_role("combobox", name=re.compile("Send these rows to"))).to_have_value("x.item")
        if first:
            rows = self._task("rows")
            rows.get_by_label("Use only rows that match", exact=True).check()
            condition = rows.locator("[data-row-inclusion-condition]:visible")
            source = condition.get_by_role("combobox", name="Source column", exact=True)
            source.select_option(self._column(dataset, "Use"))
            condition.get_by_role("textbox", name=re.compile("Comparison value")).fill("yes")
            self._capture("11r-stage3-choose-rows.png")
        self._save_continue(task="identity")
        identity = self._task("identity")
        identity.get_by_role("listbox", name=re.compile("Unique row identifier")).select_option(self._column(dataset, "Reference"))
        identity.get_by_role("combobox", name="Matching rule", exact=True).select_option(self.item_key.key_id)
        source = identity.get_by_role("combobox", name="Source column", exact=True)
        source.focus()
        source.select_option(self._column(dataset, "Reference"))
        if first:
            expect(identity.get_by_role("heading", name="Identify these records", exact=True)).to_be_in_viewport()
            self._capture("11s-stage3-identify-records.png")
        self._save_continue(task="fields")
        fields = self._task("fields")
        name = fields.locator('[data-scalar-mapping-row][data-target-field="name"]')
        name.locator("[data-value-source]").select_option("" if first else "source")
        if not first:
            self._choose_source(name, self._column(dataset, "Product title"))
        note = fields.locator('[data-scalar-mapping-row][data-target-field="note"]')
        note.locator("[data-value-source]").select_option("constant")
        note.locator("[data-literal-value]").fill(f"Reviewed {dataset.name}")
        if first:
            self.assertLessEqual(fields.locator("[data-scalar-table-scroll]").evaluate(
                "element => element.scrollWidth - element.clientWidth"
            ), 1, "Field names, value choices and previews must fit the desktop editor")
            self._capture("11t-stage3-match-fields.png")
            self._saved_position_and_native_validation(dataset)
            self._formula_recovery(dataset)
        self._save_continue(task="links")
        if first:
            expect(self._task("links").get_by_role("heading", name=re.compile("^Connect linked values"))).to_be_in_viewport()
        # Linked records are optional here. Keep them omitted to exercise normal authoring.
        for row in self._task("links").locator("[data-relation-mapping-row]").all():
            self._open_details(row)
            row.locator("[data-relation-value-source]").select_option("")
        if first:
            self._capture("11u-stage3-linked-values.png")
        self._save_continue(step="check" if last else "tables", task=None if last else "rows")
        draft = self.fixture.context.queries.get_mapping_working_draft(self.workspace_id)
        saved = next(item for item in draft.definition.datasets if item.dataset_id == dataset.dataset_id)
        self.assertEqual(saved.target_model, "x.item")
        self.assertEqual(saved.source_identity_column_keys, (self._column(dataset, "Reference"),))
        self.assertEqual(next(field.literal_value for field in saved.fields if field.target_field == "note"), f"Reviewed {dataset.name}")
        self.assertIsNone(self.fixture.context.queries.get_mapping_revision(self.workspace_id))

    def _saved_position_and_native_validation(self, dataset):
        with self.page.expect_response(lambda response: "/mapping/save" in response.url and response.request.method == "POST"):
            self.page.locator("[data-save-mapping-progress]").click()
        expect(self.page.locator("[data-mapping-save-status]")).to_contain_text("Saved")
        self.page.reload(wait_until="networkidle")
        expect(self.root).to_have_attribute("data-task", "fields")
        note = self._task("fields").locator('[data-scalar-mapping-row][data-target-field="note"]')
        expect(note.locator("[data-literal-value]")).to_have_value(f"Reviewed {dataset.name}")
        self._go_task("identity")
        self.page.go_back(wait_until="domcontentloaded")
        expect(self.root).to_have_attribute("data-task", "fields")
        amount = self._task("fields").locator('[data-scalar-mapping-row][data-target-field="amount"]')
        self._open_details(amount.locator("details.mapping-value-preparation"))
        places = amount.locator("[data-round-places]")
        places.fill("19")
        self._go_task("rows")
        self.page.locator("[data-save-mapping-progress]").click()
        expect(self.root).to_have_attribute("data-task", "fields")
        expect(places).to_be_in_viewport()
        expect(places).to_be_focused()
        self.assertFalse(places.evaluate("element => element.validity.valid"))
        places.fill("2")

    def _formula_recovery(self, dataset):
        note = self._task("fields").locator('[data-scalar-mapping-row][data-target-field="note"]')
        self._open_details(note.locator("details.mapping-value-preparation"))
        self._open_details(note.locator("[data-value-rule-builder]"))
        self._open_details(note.locator("[data-formula-control]"))
        formula = note.locator("[data-rule-formula]")
        formula.fill('value 1= "UNI"')
        formula.blur()
        expect(note.locator("[data-formula-feedback]")).to_be_visible()
        expect(self.page.locator("[data-check-mapping]")).to_be_disabled()
        with self.page.expect_response(lambda response: "/mapping/save" in response.url
                and response.request.method == "POST") as saved:
            self.page.locator("[data-save-mapping-progress]").click()
        self.assertEqual(saved.value.json()["status"], "committed")
        expect(self.page.locator("[data-mapping-save-status]")).to_contain_text("needs attention")
        draft = self.fixture.context.queries.get_mapping_working_draft(self.workspace_id)
        saved_note = next(field for table in draft.definition.datasets
            if table.dataset_id == dataset.dataset_id for field in table.fields
            if field.target_field == "note")
        self.assertEqual(saved_note.transform.formula, 'value 1= "UNI"')
        expect(note.locator("[data-literal-value]")).to_have_value(f"Reviewed {dataset.name}")
        self._capture("11x-stage3-formula-recovery.png")
        formula.fill("")
        formula.blur()
        expect(note.locator("[data-formula-feedback]")).to_be_hidden()
        expect(self.page.locator("[data-check-mapping]")).to_be_enabled()

    def _check_and_fix_required_name(self, dataset):
        with self.page.expect_navigation(wait_until="networkidle"):
            self.page.locator("[data-check-mapping]").click()
        expect(self.root).to_have_attribute("data-step", "check")
        blockers = self.page.locator("#next-step-blockers")
        expect(blockers).to_be_visible()
        expect(blockers).to_be_in_viewport()
        issue = blockers.locator("details.mapping-issue").filter(has_text=f"{dataset.name} · Name")
        self.assertEqual(issue.count(), 1, blockers.inner_text())
        self._open_details(issue)
        self._capture("11v-stage3-check-resolve.png")
        revision = self.fixture.context.queries.get_mapping_revision(self.workspace_id)
        self.assertIsNone(self.fixture.context.queries.get_mapping_submission(self.workspace_id, revision.version))
        with self.page.expect_navigation(wait_until="networkidle"):
            issue.get_by_role("link", name="Match this field", exact=True).click()
        expect(self.root).to_have_attribute("data-task", "fields")
        self._settle_layout()
        row = self._task("fields").locator('[data-scalar-mapping-row][data-target-field="name"]')
        expect(row).to_be_in_viewport()
        row.locator("[data-value-source]").select_option("source")
        self._choose_source(row, self._column(dataset, "Product title"))
        self.page.get_by_role("link", name="Return to checks", exact=True).click()
        expect(self.root).to_have_attribute("data-step", "check")
        with self.page.expect_navigation(wait_until="networkidle"):
            self.page.locator("[data-check-mapping]").click()
        expect(self.root).to_have_attribute("data-step", "confirm")
        expect(self.page.locator("#rows-to-use-review").get_by_role(
            "button", name="Confirm 1 row to use", exact=True,
        )).to_be_visible()
        revision = self.fixture.context.queries.get_mapping_revision(self.workspace_id)
        self.assertIsNone(self.fixture.context.queries.get_mapping_submission(self.workspace_id, revision.version))

    def _confirm_rows_and_matches(self):
        rows = self.page.locator("#rows-to-use-review")
        rows.get_by_role("button", name="Confirm 1 row to use", exact=True).click()
        expect(self.root).to_have_attribute("data-step", "confirm")
        expect(rows).to_contain_text("1 row confirmed")
        self._capture("11w-stage3-confirm-matches.png")
        confirm = self.page.get_by_role("button", name="Confirm field matches", exact=True)
        expect(confirm).to_be_enabled()
        confirm.click()
        self.page.wait_for_url(re.compile(r"/prepare(?:\?.*)?$"))
        expect(self.page.get_by_text("Stage 4 of 6 · Prepare data", exact=True)).to_be_visible()
        revision = self.fixture.context.queries.get_mapping_revision(self.workspace_id)
        self.assertIsNotNone(self.fixture.context.queries.get_mapping_submission(self.workspace_id, revision.version))

    @staticmethod
    def _column(dataset, name):
        return next(column.stable_key for column in dataset.columns if column.source_name == name)

    @staticmethod
    def _choose_source(row, stable_key):
        source = row.locator("[data-source-column]")
        source.focus()
        source.select_option(stable_key)

    @staticmethod
    def _open_details(details):
        if not details.evaluate("element => element.open"):
            details.locator(":scope > summary").click()

    def _capture(self, filename):
        directory = os.environ.get("IMPODO_STAGE3_SCREENSHOT_DIR")
        if directory and self.major == 20 and self.mode is OdooConnectionMode.REMOTE:
            output = Path(directory)
            output.mkdir(parents=True, exist_ok=True)
            self._settle_layout()
            self.page.screenshot(path=str(output / filename), animations="disabled")

    def _settle_layout(self):
        self.assertTrue(self.page.evaluate("""async () => {
            await document.fonts.ready;
            let previous = null, stable = 0;
            for (let frame = 0; frame < 120; frame += 1) {
                await new Promise(requestAnimationFrame);
                const current = [window.scrollX, window.scrollY,
                    document.documentElement.scrollHeight].join(':');
                stable = current === previous ? stable + 1 : 0;
                previous = current;
                if (stable >= 4) return true;
            }
            return false;
        }"""), "Stage 3 layout and scroll must settle before viewport checks")
