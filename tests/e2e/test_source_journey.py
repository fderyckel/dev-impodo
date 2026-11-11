"""Exercise a linear Stage 1 journey against isolated authenticated repositories."""

import os
from pathlib import Path
import re
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.source_journey import SourceJourneyFixture


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class SourceJourneyBrowserTests(unittest.TestCase):
    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")

    def test_file_confirmation_moves_to_the_next_required_choice(self):
        fixture = SourceJourneyFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.fixture, self.major, self.mode = fixture, 20, "LOCAL"
        self.workspace_id = fixture.inspect_files()
        catalogs = fixture.context.queries.get_source_catalogs(self.workspace_id)
        self._start_browser(fixture)
        self.page.goto(
            f"{self.base}/workspaces/{self.workspace_id}/sources",
            wait_until="networkidle",
        )
        for index, catalog in enumerate(catalogs):
            card = self.page.locator(f'[data-source-review-card="{catalog.file_id}"]')
            card.get_by_role("button", name="Confirm this file", exact=True).click()
            expect(
                card.locator("[data-source-confirmation-status]").first
            ).to_contain_text("Confirmed")
            onward = card.locator("[data-source-confirmation-next]")
            discard = card.locator("[data-source-discard-changes]")
            expect(discard).to_be_hidden()
            expect(onward).to_be_in_viewport()
            if index == 0:
                expect(onward).to_have_text("Confirm next file")
                self._capture("04-source-inspection.png")
                header = card.get_by_role(
                    "spinbutton", name=re.compile("Which row contains")
                )
                header.fill("2")
                expect(discard).to_be_visible()
                expect(onward).to_be_hidden()
                discard.click()
                expect(header).to_have_value("1")
                expect(discard).to_be_hidden()
                expect(onward).to_be_visible()
            else:
                expect(onward).to_have_text("Name and save tables")
            onward.click()
            if index + 1 < len(catalogs):
                next_card = self.page.locator(
                    f'[data-source-review-card="{catalogs[index + 1].file_id}"]'
                )
                expect(
                    next_card.get_by_role(
                        "heading", name=catalogs[index + 1].display_name, exact=True
                    )
                ).to_be_in_viewport()
        expect(
            self.page.get_by_role(
                "heading", name="Save the tables for this data version", exact=True
            )
        ).to_be_in_viewport()
        self.page.get_by_role(
            "button", name="Save tables for this data version", exact=True
        ).click()
        expect(
            self.page.get_by_role("heading", name="Saved source tables", exact=True)
        ).to_be_in_viewport()
        self.assertEqual(
            len(
                fixture.context.queries.get_source_selection(self.workspace_id).datasets
            ),
            2,
        )
        self.assertEqual(self.errors, [])

    def exercise(self, major, mode):
        fixture = SourceJourneyFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.fixture, self.major, self.mode = fixture, major, mode
        self.workspace_id, self.selection = fixture.configure(major, mode)
        self.products = next(
            item for item in self.selection.datasets if item.name == "products"
        )
        self.details = next(
            item for item in self.selection.datasets if item.name == "details"
        )
        self.columns = {
            column.source_name: column.stable_key for column in self.products.columns
        }
        self._start_browser(fixture)
        try:
            self._overview()
            self._combine()
            self._hierarchy()
            self._lookup_with_failed_save()
            self._split_and_continue()
            self._reopen_and_narrow_layout()
        except Exception:
            self._capture("stage1-failure.png", force=True)
            raise
        self.assertEqual(self.errors, [])
        self.assertEqual(
            fixture.context.queries.get_source_selection(self.workspace_id),
            self.selection,
        )

    def _start_browser(self, fixture):
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
        self.path = f"/workspaces/{self.workspace_id}/derived-entities"
        self.root = self.page.locator("[data-source-journey]")
        self.next = self.page.locator("[data-source-next]")
        self.state = self.page.locator("[data-source-state]")

    def _overview(self):
        self.page.goto(
            f"{self.base}/workspaces/{self.workspace_id}/datasets",
            wait_until="networkidle",
        )
        expect(
            self.page.get_by_role("heading", name="Saved source tables")
        ).to_be_in_viewport()
        for dataset in self.selection.datasets:
            expect(
                self.page.locator("main").get_by_text(dataset.name, exact=True)
            ).to_be_visible()
        self._capture("05-frozen-tables.png")
        self.page.get_by_role(
            "link", name="Separate combined information", exact=True
        ).click()
        expect(self.root).to_be_visible()
        pairwise = self.page.get_by_role(
            "button", name="Combine two tables", exact=True
        )
        expect(pairwise).to_be_hidden()
        self._capture("06i-stage1-choice.png")
        combined = self._choose("Combine tables", "combine-source-tables")
        advanced = combined.locator("[data-source-combination-advanced]")
        expect(pairwise).to_be_hidden()
        advanced.locator("summary").focus()
        self.page.keyboard.press("Enter")
        expect(pairwise).to_be_visible()
        pairwise.click()
        expect(self.root).to_have_attribute("data-source-active", "combine-two-tables")
        expect(self.page.locator("[data-exact-join-form]")).to_be_visible()
        self.page.locator("[data-source-add-another]").click()

    def _choose(self, name, operation):
        self.page.get_by_role("button", name=name, exact=True).click()
        expect(self.root).to_have_attribute("data-source-active", operation)
        panel = self.page.locator(f"#{operation}")
        expect(panel).to_be_visible()
        self.assertEqual(
            self.page.locator("[data-source-operation]:visible").count(), 1
        )
        return panel

    def _review(self, panel):
        self.next.click()
        expect(panel.locator("[data-source-review]")).to_be_visible()
        expect(self.state).to_have_text("Ready to use")
        title = panel.locator("[data-source-review] h2").first
        expect(title).to_be_focused()
        self.assertEqual(
            title.evaluate("element => getComputedStyle(element).outlineStyle"), "none"
        )
        expect(self.next).to_be_in_viewport()
        bounds = self.next.bounding_box()
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertLessEqual(
            bounds["x"] + bounds["width"], self.page.viewport_size["width"]
        )
        self.assertGreaterEqual(bounds["y"], 0)
        self.assertLessEqual(
            bounds["y"] + bounds["height"], self.page.viewport_size["height"] + 1
        )
        self.assertTrue(
            self.next.evaluate(
                "element => element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1"
            ),
            "The complete Continue label must fit inside its visible button",
        )
        self.assertTrue(
            self.next.evaluate(
                "element => { const r = element.getBoundingClientRect(); return element.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); }"
            )
        )

    def _plan(self):
        return self.fixture.context.queries.get_derived_entity_plan(self.workspace_id)

    def _combine(self):
        panel = self._choose("Combine tables", "combine-source-tables")
        panel.get_by_label("Combined table name", exact=True).fill("complete_products")
        panel.get_by_role("combobox", name=re.compile("^Primary table")).select_option(
            self.products.dataset_id
        )
        panel.locator('[name="primary_key_field_1"]').select_option(
            f"{self.products.dataset_id}|{self.columns['Id']}"
        )
        panel.get_by_role("checkbox", name="Add details", exact=True).check()
        key = next(
            column.stable_key
            for column in self.details.columns
            if column.source_name == "Id"
        )
        panel.get_by_role("combobox", name="details key 1", exact=True).select_option(
            f"{self.details.dataset_id}|{key}"
        )
        expect(self.state).to_have_text("Unsaved changes")
        self.page.get_by_role("button", name="Build a hierarchy", exact=True).click()
        expect(self.root).to_have_attribute(
            "data-source-active", "combine-source-tables"
        )
        expect(self.page.locator("[data-source-notice]")).to_be_visible()
        self.assertIsNone(self._plan())
        self._review(panel)
        table = panel.locator("[data-source-review] .table-scroll").first
        self.assertLessEqual(
            table.evaluate("element => element.scrollWidth - element.clientWidth"), 1
        )
        first_table = table.locator("tbody th").first
        original_name = first_table.inner_text()
        first_table.evaluate(
            "element => { element.textContent = 'ecranproduitslance'; }"
        )
        self.assertLessEqual(
            table.evaluate("element => element.scrollWidth - element.clientWidth"), 1
        )
        first_table.evaluate(
            "(element, text) => { element.textContent = text; }", original_name
        )
        self._capture("06j-stage1-combination-review.png")
        # Changing the editor must invalidate the older review and its save action.
        panel.get_by_role("button", name="Change selections", exact=True).click()
        panel.get_by_label("Combined table name", exact=True).fill(
            "complete_products_checked"
        )
        expect(panel.locator("[data-source-review]")).to_be_hidden()
        expect(self.next).to_have_text("Review result")
        self._review(panel)
        panel.get_by_role("button", name="Use this table", exact=True).click()
        self.page.wait_for_url("**/derived-entities#join-rule-*")
        expect(self.state).to_have_text("Saved")
        expect(self.next).to_be_in_viewport()
        self.assertEqual(self._plan().version, 1)
        self.assertEqual(
            self._plan().rules[-1].output_dataset_name, "complete_products_checked"
        )
        self._capture("06l-stage1-saved-changes.png")
        self.page.locator("[data-source-add-another]").click()
        expect(self.page.locator("[data-source-choices]")).to_be_in_viewport()

    def _hierarchy(self):
        panel = self._choose("Build a hierarchy", "hierarchy-extraction")
        source = panel.locator(
            f'[data-source-editor][data-source-source-id="{self.products.dataset_id}"]'
        )
        source.evaluate("form => { form.closest('details').open = true; }")
        source.get_by_role(
            "combobox", name=re.compile("^Hierarchy level 1")
        ).select_option(self.columns["Group"])
        source.get_by_role(
            "combobox", name=re.compile("^Hierarchy level 2")
        ).select_option(self.columns["Subgroup"])
        source.get_by_label("Name shown in Impodo", exact=True).fill(
            "product_categories"
        )
        source.get_by_label("Type of Odoo record", exact=True).fill("product.category")
        self._capture("06k-stage1-hierarchy.png")
        self._review(panel)
        self._capture("06m-stage1-hierarchy-review.png")
        # Adding another operation saves the reviewed result first.
        self.page.locator("[data-source-add-another]").click()
        expect(self.page.locator("[data-source-choices]")).to_be_in_viewport()
        expect(self.state).to_have_text("Saved")
        self.assertEqual(self._plan().version, 2)

    def _lookup_with_failed_save(self):
        panel = self._choose("Create a related table", "lookup-extraction")
        editor = panel.locator("[data-source-editor]")
        editor.get_by_role(
            "combobox", name=re.compile("^Use values from")
        ).select_option(f"{self.products.dataset_id}|{self.columns['Group']}")
        editor.get_by_role("textbox", name=re.compile("^Name shown in Impodo")).fill(
            "product_groups"
        )
        editor.get_by_label("Type of Odoo record", exact=True).fill("product.category")
        self._review(panel)
        # A lost response must retain selections and must never repeat a mutation automatically.
        self.page.route(f"**{self.path}/save", lambda route: route.abort("failed"))
        panel.get_by_role("button", name="Use this table", exact=True).click()
        expect(self.state).to_have_text("Save not confirmed")
        expect(editor).to_have_attribute("inert", "")
        expect(
            panel.get_by_role("button", name="Change selections", exact=True)
        ).to_be_disabled()
        expect(
            self.page.get_by_role("button", name="Check saved changes", exact=True)
        ).to_be_in_viewport()
        self.page.set_viewport_size({"width": 390, "height": 844})
        check = self.page.get_by_role("button", name="Check saved changes", exact=True)
        expect(check).to_be_in_viewport()
        bounds = check.bounding_box()
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
        self.assertLessEqual(bounds["y"] + bounds["height"], 845)
        self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        expect(check).to_be_in_viewport()
        expect(self.page.locator(".site-footer")).to_be_in_viewport()
        self.page.wait_for_function("""() => {
          const root = document.querySelector('[data-source-journey]');
          const bar = root.querySelector('[data-source-action-bar]');
          const footer = document.querySelector('.site-footer');
          return bar.getBoundingClientRect().bottom <= footer.getBoundingClientRect().top;
        }""")
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth - innerWidth"), 1
        )
        self.page.set_viewport_size({"width": 1440, "height": 1024})
        self.assertEqual(self._plan().version, 2)
        expect(
            panel.locator('[data-source-save] [name="output_dataset_name"]')
        ).to_have_value("product_groups")
        self.page.unroute(f"**{self.path}/save")
        self.page.get_by_role("button", name="Check saved changes", exact=True).click()
        expect(self.state).to_have_text("Ready to use")
        self.page.locator("[data-source-add-another]").click()
        expect(self.state).to_have_text("Saved")
        self.assertEqual(self._plan().version, 3)

    def _split_and_continue(self):
        panel = self._choose("Split a table", "create-related-datasets")
        editor = panel.locator(
            f'[data-source-editor][data-source-source-id="{self.products.dataset_id}"]'
        )
        editor.evaluate("form => { form.closest('details').open = true; }")
        editor.locator('[name="parent_dataset_name"]').fill("source_groups")
        editor.locator('[name="child_dataset_name"]').fill("source_items")
        editor.locator('[name="parent_key_column_key"]').select_option(
            self.columns["Group"]
        )
        editor.locator('[name="child_key_column_key"]').select_option(
            self.columns["Id"]
        )
        self._review(panel)
        expect(self.next).to_have_text("Save and review changes")
        self.next.click()
        self.page.wait_for_url(f"**{self.path}#saved-source-changes")
        summary = self.page.get_by_role("heading", name="Saved changes", exact=True)
        expect(summary).to_be_in_viewport()
        expect(summary).to_be_focused()
        expect(self.state).to_have_text("Saved")
        expect(self.page.locator("[data-source-saved]")).to_have_count(4)
        expect(self.next).to_have_text("Continue to Odoo data")
        expect(self.next).to_be_in_viewport()
        self._capture("06l-stage1-saved-changes.png")
        self.assertEqual(self._plan().version, 4)
        effective = self.fixture.context.queries.get_mapping_source_selection(
            self.workspace_id
        )
        self.assertTrue(
            {
                "complete_products_checked",
                "product_categories",
                "product_groups",
                "source_groups",
                "source_items",
            }.issubset({item.name for item in effective.datasets})
        )
        self.next.click()
        self.page.wait_for_url(f"**/workspaces/{self.workspace_id}/schema")
        self.assertEqual(self._plan().version, 4)

    def _reopen_and_narrow_layout(self):
        self.page.goto(f"{self.base}{self.path}", wait_until="networkidle")
        expect(self.state).to_have_text("Saved")
        self.assertEqual(self.page.locator("[data-source-saved]").count(), 4)
        self.page.reload(wait_until="networkidle")
        self.assertEqual(self._plan().version, 4)
        self.page.set_viewport_size({"width": 390, "height": 844})
        expect(self.next).to_be_in_viewport()
        bounds = self.next.bounding_box()
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
        choices = self.page.locator("[data-source-composition-actions] button")
        tops = choices.evaluate_all(
            "buttons => buttons.map(button => button.getBoundingClientRect().top)"
        )
        self.assertEqual(len(set(tops)), 1)
        self.assertEqual(self.errors, [])

    def _capture(self, name, *, force=False):
        output = os.environ.get("IMPODO_SOURCE_SCREENSHOTS")
        if not output or (not force and (self.major, self.mode) != (20, "LOCAL")):
            return
        path = Path(output) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=path)
