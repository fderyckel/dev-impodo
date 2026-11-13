"""Exercise capture-plan field shortcuts without opening an Impodo session."""

import os
import re
import sys
import unittest

from jinja2 import Environment

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT

STATIC_ROOT = REPOSITORY_ROOT / "src/impodo/web/static"


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class CaptureFieldSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL")
            or ("msedge" if sys.platform == "win32" else None),
            headless=True,
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1988, "height": 1440})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.requests = []
        self.page.on("request", lambda request: self.requests.append(request.method))
        template = (
            REPOSITORY_ROOT
            / "src/impodo/web/templates/workspace_odoo_capture_selection.html"
        ).read_text(encoding="utf-8")
        fieldset = re.search(
            r"<fieldset\b[^>]*\bdata-capture-fields\b[^>]*>.*?</fieldset>",
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(fieldset)
        advanced_options = re.search(
            r"<details\b[^>]*\bdata-capture-advanced-options\b[^>]*>.*?</details>",
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(advanced_options)
        advanced_html = (
            Environment(autoescape=True)
            .from_string(advanced_options.group())
            .render(
                dataset_name_default="odoo_assets",
                capture_page_sizes=(10, 100, 500),
                current=None,
                error=None,
            )
        )
        self.fields_html = (
            Environment(autoescape=True)
            .from_string(fieldset.group())
            .render(
                selected_model={"label": "Depreciation Models for Assets"},
                fields=[
                    {"name": "active", "label": "Active", "type": "boolean"},
                    {"name": "name", "label": "Name", "type": "char"},
                    {"name": "duration", "label": "Duration", "type": "float"},
                ],
                selected_field_names=["name"],
            )
        )
        styles = "\n".join(
            (STATIC_ROOT / name).read_text(encoding="utf-8")
            for name in (
                "tokens.css",
                "layout.css",
                "components.css",
                "workflow-pages.css",
                "source-capture-fields.css",
            )
        )
        scripts = "\n".join(
            (STATIC_ROOT / name).read_text(encoding="utf-8")
            for name in ("app.js", "source-workflow.js")
        )
        html = f"""
          <!doctype html><html><head><meta name="viewport"
            content="width=device-width, initial-scale=1"><style>{styles}</style>
          </head><body><main style="max-width:1240px;margin:auto;padding:16px">
            <form data-linked-data-form>
              <label>Linked choice<select name="related_actions">
                <option value="keep">Keep linked value</option>
                <option value="transfer">Transfer related records</option>
              </select></label>
              <p data-linked-data-unsaved-notice tabindex="-1" hidden>Save linked choices.</p>
            </form>
            <form id="capture" data-capture-plan-form data-single-submit>
              {advanced_html}
              <label><input type="checkbox" name="include_archived" checked>Include archived</label>
              <label><input type="checkbox" name="remove_filter">Remove saved filter</label>
              {self.fields_html}
              <button type="submit">Save fields and continue</button>
            </form>
            <form id="other" data-capture-plan-form>{self.fields_html}</form>
          </main><script>{scripts}</script></body></html>
        """
        self.page.route(
            "**/*",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        self.page.goto("http://127.0.0.1:12345/sources")
        self.form = self.page.locator("#capture")
        self.count = self.form.locator("[data-capture-field-count]")
        self.select_all = self.form.get_by_role("button", name="Select all fields")
        self.clear = self.form.get_by_role("button", name="Clear selection")

    def selected_fields(self, form_id="capture"):
        return self.page.locator(f"#{form_id}").evaluate(
            'form => new FormData(form).getAll("field_names")'
        )

    def test_saved_selection_stays_until_operator_chooses_all_or_clear(self):
        self.assertEqual(self.selected_fields(), ["name"])
        self.assertEqual(self.count.inner_text(), "1 of 3 fields selected")
        self.select_all.click()
        self.assertEqual(self.selected_fields(), ["active", "name", "duration"])
        self.assertEqual(self.count.inner_text(), "3 of 3 fields selected")
        self.assertTrue(self.select_all.is_disabled())
        self.clear.click()
        self.assertEqual(self.selected_fields(), [])
        self.assertEqual(self.count.inner_text(), "0 of 3 fields selected")
        self.assertTrue(self.clear.is_disabled())
        self.assertEqual(self.selected_fields("other"), ["name"])
        self.assertTrue(
            self.form.get_by_role("checkbox", name="Include archived").is_checked()
        )
        self.assertFalse(
            self.form.get_by_role("checkbox", name="Remove saved filter").is_checked()
        )
        self.assertEqual(self.requests, ["GET"])
        self.assertIsNone(self.form.get_attribute("aria-busy"))
        self.assertEqual(self.errors, [])

    def test_disabled_fields_and_disabled_fieldsets_are_left_unchanged(self):
        self.form.evaluate(
            """form => {
              form.querySelector('[value="active"]').disabled = true;
              form.querySelector('[value="duration"]').outerHTML =
                '<fieldset disabled><input type="checkbox" name="field_names" value="duration" checked></fieldset>';
            }"""
        )
        self.select_all.click()
        self.assertFalse(self.form.locator('[value="active"]').is_checked())
        self.assertTrue(self.form.locator('[value="duration"]').is_checked())
        self.assertEqual(self.selected_fields(), ["name"])
        self.assertEqual(self.count.inner_text(), "1 of 1 fields selected")
        self.clear.click()
        self.assertTrue(self.form.locator('[value="duration"]').is_checked())
        self.assertEqual(self.selected_fields(), [])
        self.assertEqual(self.errors, [])

    def test_manual_changes_and_reset_keep_the_count_current(self):
        self.form.get_by_role("checkbox", name=re.compile(r"^Active\b")).check()
        self.assertEqual(self.count.inner_text(), "2 of 3 fields selected")
        self.form.get_by_role("checkbox", name=re.compile(r"^Name\b")).uncheck()
        self.assertEqual(self.count.inner_text(), "1 of 3 fields selected")
        self.select_all.click()
        self.form.evaluate("form => form.reset()")
        self.assertEqual(self.selected_fields(), ["name"])
        self.assertEqual(self.count.inner_text(), "1 of 3 fields selected")
        self.assertTrue(self.select_all.is_enabled())
        self.assertTrue(self.clear.is_enabled())
        self.assertEqual(self.errors, [])

    def test_save_keeps_the_linked_choice_guard_and_exact_field_payload(self):
        self.select_all.click()
        self.form.get_by_role("checkbox", name=re.compile(r"^Duration\b")).uncheck()
        self.page.get_by_role("combobox", name="Linked choice").select_option(
            "transfer"
        )
        self.form.get_by_role("button", name="Save fields and continue").click()
        self.assertTrue(
            self.page.locator("[data-linked-data-unsaved-notice]").is_visible()
        )
        self.assertEqual(self.selected_fields(), ["active", "name"])
        self.assertEqual(self.count.inner_text(), "2 of 3 fields selected")
        self.assertIsNone(self.form.get_attribute("aria-busy"))
        self.assertEqual(self.requests, ["GET"])
        self.assertEqual(self.errors, [])

    def test_invalid_advanced_name_opens_its_options_for_correction(self):
        advanced = self.form.locator("[data-capture-advanced-options]")
        self.assertIsNone(advanced.get_attribute("open"))
        advanced.locator("summary").click()
        name = advanced.get_by_role("textbox", name="Dataset name")
        name.fill("Invalid name")
        advanced.locator("summary").click()
        self.form.get_by_role("button", name="Save fields and continue").click()
        self.assertIsNotNone(advanced.get_attribute("open"))
        self.assertTrue(name.is_visible())
        self.assertEqual(name.input_value(), "Invalid name")
        self.assertTrue(name.evaluate("input => input === document.activeElement"))
        self.assertEqual(self.requests, ["GET"])
        self.assertEqual(self.errors, [])

    def test_bulk_controls_are_visible_at_desktop_and_narrow_widths(self):
        for width in (1988, 375):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 1440})
                self.assertTrue(self.select_all.is_visible())
                self.assertTrue(self.clear.is_visible())
                for control in (self.select_all, self.clear, self.count):
                    bounds = control.bounding_box()
                    self.assertGreaterEqual(bounds["x"], 0)
                    self.assertLessEqual(bounds["x"] + bounds["width"], width)
                if width == 1988:
                    self.assertEqual(
                        self.select_all.bounding_box()["y"],
                        self.clear.bounding_box()["y"],
                    )
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
