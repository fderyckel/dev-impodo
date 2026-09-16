"""Exercise constant relationship authoring in a browser with fictional data.

Run with Playwright installed: python -m unittest
tests.e2e.test_constant_relationship_editor. Uses Edge on Windows and Chromium
elsewhere. All HTTP requests are intercepted; no running project is accessed.
"""

import sys
import unittest

from starlette.datastructures import FormData

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from impodo.web.presenters.mapping_view import _mapping_dataset_views
from tests.integration.web import test_mapping_form_roundtrip as fixtures
from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class ConstantRelationshipEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None, headless=True
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.fixture = fixtures.MappingFormRoundTripTests()
        self.fixture.setUp()
        views = _mapping_dataset_views(
            self.fixture.selection, self.fixture.schema, self.fixture.governance,
            self.fixture.definition.datasets, derived_links=self.fixture.links,
            active_dataset_index=0,
        )
        html = '<form data-mapping-form action="/mapping" method="post">'
        html += '<input type="hidden" name="editable_dataset_id" value="derived:categories">'
        html += "".join(self.fixture.template.render(
            view=view, workspace_id="workspace:test", formula_authoring_issues_by_dataset={},
        ) for view in views)
        html += '<button id="save" name="action" value="save_progress">Save</button></form>'
        html += """<script>
          window.choiceResult = {ok: false, detail: "Check the connection and fields in Odoo data again."};
          window.fetch = async (url, options) => {
            if (url.endsWith('/value-choices')) {
              const result = window.choiceResult;
              return {ok: result.ok, json: async () => result};
            }
            window.sent = JSON.parse(options.body).entries;
            return new Promise(() => {});
          };
        </script>"""
        for name in (
            "mapping-save-recovery.js",
            "mapping-editor.js",
            "mapping-relation-row.js",
        ):
            script = (REPOSITORY_ROOT / "src/impodo/web/static" / name).read_text(encoding="utf-8")
            html += f"<script>{script}</script>"
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
        self.page.goto("http://127.0.0.1:12345/mapping")
        self.page.wait_for_function("window.impodoMappingEditor !== undefined")
        self.row = self.page.locator('[data-relation-mapping-row][data-target-field="parent_id"]')
        self.row.evaluate("""row => {
          for (let node = row; node; node = node.parentElement) {
            if (node instanceof HTMLDetailsElement) node.open = true;
          }
        }""")
        self.provider = self.row.locator("[data-relation-value-source]")
        self.key = self.row.locator("[data-constant-business-key]")
        self.value = self.row.locator("[data-constant-component-value]").first
        self.provider.select_option("constant_existing")
        self.key.select_option("key:category")

    def submit(self):
        self.page.evaluate("document.querySelector('form').requestSubmit(document.querySelector('#save'))")

    def saved_reference(self):
        self.page.wait_for_function("window.sent !== undefined")
        datasets = self.fixture.parse(FormData(self.page.evaluate("window.sent")))
        self.assertEqual(self.errors, [])
        relation = datasets[0].relationships[0]
        self.assertEqual(relation.source_column_keys, ())
        return relation.constant_reference.key_values[0].value

    def test_blank_value_opens_collapsed_cards_and_manual_value_can_save(self):
        for blank in ("", "   "):
            self.value.fill(blank)
            self.row.evaluate("""row => {
              row.open = false;
              row.closest('.related-records').open = false;
            }""")
            self.submit()
            self.assertFalse(self.page.evaluate("window.sent !== undefined"))
            self.assertTrue(self.value.evaluate("input => document.activeElement === input"))
            self.assertIn("Enter x_code", self.value.evaluate("input => input.validationMessage"))
        self.value.fill("PCE")
        self.submit()
        self.assertEqual(self.saved_reference(), "PCE")

    def test_lookup_failure_preserves_edits_and_does_not_leave_old_choices(self):
        button = self.row.locator("[data-check-constant-record]")
        status = self.row.locator("[data-constant-choice-status]")
        button.click()
        self.assertIn("Odoo data again", status.inner_text())
        self.submit()
        self.assertFalse(self.page.evaluate("window.sent !== undefined"))
        self.page.evaluate("window.choiceResult = {ok: true, target_choices: [{value: 'PCE', label: 'PCE'}]}")
        button.click()
        self.assertEqual(self.value.input_value(), "")
        self.assertIn("Choose a record to fill the matching value", status.inner_text())
        choice = self.row.locator("[data-constant-existing-choice]")
        choice.select_option("PCE")
        self.assertEqual(self.value.input_value(), "PCE")
        self.page.evaluate("window.choiceResult = {ok: false, detail: 'Check the connection and fields in Odoo data again.'}")
        button.click()
        self.assertIn("Odoo data again", status.inner_text())
        self.assertEqual(choice.locator("option").count(), 1)
        self.assertEqual(self.value.input_value(), "PCE")
        self.submit()
        self.assertEqual(self.saved_reference(), "PCE")

    def test_required_components_follow_the_selected_rule_and_provider(self):
        self.key.evaluate("""select => {
          const option = new Option('Code and name', 'key:composite');
          option.dataset.keyFields = 'x_code';
          option.dataset.scopeFields = 'name';
          select.append(option);
        }""")
        self.key.select_option("key:composite")
        self.value.fill("PCE")
        second = self.row.locator("[data-constant-component-value]").nth(1)
        self.assertTrue(second.evaluate("input => input.required && !input.checkValidity()"))
        self.key.select_option("key:category")
        self.assertTrue(second.evaluate("input => input.disabled && !input.required && input.checkValidity()"))
        self.value.fill(" ")
        self.provider.select_option("source")
        self.assertTrue(self.value.evaluate("input => !input.required && input.checkValidity()"))
        self.provider.select_option("constant_existing")
        self.key.select_option("")
        self.assertFalse(self.key.evaluate("select => select.checkValidity()"))
        self.assertEqual(self.errors, [])

    def test_scoped_choice_fills_name_and_company(self):
        self.key.evaluate("""select => {
          const option = new Option('Name within Company', 'key:scoped');
          option.dataset.keyFields = 'name';
          option.dataset.scopeFields = 'company_id';
          select.append(option);
        }""")
        self.key.select_option("key:scoped")
        self.page.evaluate("""window.choiceResult = {
          ok: true,
          target_choices: [{
            value: '["Standard 40 hours/week","United Caps Wiltz"]',
            label: 'Standard 40 hours/week (United Caps Wiltz)'
          }]
        }""")
        self.row.locator("[data-check-constant-record]").click()
        self.row.locator("[data-constant-existing-choice]").select_option(
            '["Standard 40 hours/week","United Caps Wiltz"]'
        )
        values = self.row.locator("[data-constant-component-value]")
        self.assertEqual(values.nth(0).input_value(), "Standard 40 hours/week")
        self.assertEqual(values.nth(1).input_value(), "United Caps Wiltz")
        self.assertEqual(self.errors, [])

    def test_constant_value_survives_catalogue_row_replacement(self):
        original_row = self.row.evaluate("row => row.outerHTML")
        self.value.fill("PCE")
        self.row.evaluate("""(row, original) => {
          const container = document.createElement('div');
          container.innerHTML = original;
          const replacement = container.firstElementChild;
          delete replacement.dataset.relationRowInitialized;
          row.replaceWith(replacement);
          window.impodoMappingEditor.restoreRelationRow(replacement);
          window.impodoMappingEditor.initializeRelationRow(replacement);
        }""", original_row)
        self.assertEqual(self.value.input_value(), "PCE")
        self.assertTrue(self.value.evaluate("input => input.required && input.checkValidity()"))
        self.submit()
        self.assertEqual(self.saved_reference(), "PCE")


if __name__ == "__main__":
    unittest.main()
