"""Keep file confirmation edits until the operator confirms or discards them."""

import sys
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class SourceConfirmationGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None,
            headless=True,
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        script = (
            REPOSITORY_ROOT / "src/impodo/web/static/source-workflow.js"
        ).read_text(encoding="utf-8")
        app_script = (REPOSITORY_ROOT / "src/impodo/web/static/app.js").read_text(
            encoding="utf-8"
        )
        self.html = f"""
          <div data-source-review-page data-source-position-key="test"></div>
          <span class="complete" data-source-page-status>2 of 2 files confirmed</span>
          <a href="/schema" id="next-stage">Choose Odoo data</a>
          <a href="#table-choices" id="naming">Name tables</a>
          {self._file_card("first")}
          {self._file_card("second")}
          <form id="freeze" action="/datasets/freeze" data-single-submit>
            <button type="submit">Save source tables</button>
          </form>
          <form id="add-file" action="/sources/files">
            <button type="submit">Add corrected file</button>
          </form>
          <script>{app_script}</script><script>{script}</script>
        """
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route(
            "**/*",
            lambda route: route.fulfill(body=self.html, content_type="text/html"),
        )
        self.page.goto("http://127.0.0.1:12345/sources")

    @staticmethod
    def _file_card(name):
        return f"""
          <section data-source-review-card="{name}" data-source-review-confirmed="true">
            <form id="{name}" data-source-review-form action="/sources/{name}/configure">
              <fieldset>
                <input name="header_row_0" type="number" value="1" aria-label="{name} heading row">
                <span class="complete" data-source-confirmation-status>Confirmed</span>
                <p data-source-confirmation-notice hidden>Confirm or discard changes.</p>
                <button type="submit" data-source-confirmation-action>Update confirmation</button>
                <button type="button" data-source-discard-changes hidden>Discard changes</button>
                <a href="#table-choices" data-source-confirmation-next>Name and save tables</a>
              </fieldset>
            </form>
          </section>
        """

    def _can_leave(self):
        return self.page.evaluate("""() => {
          const event = new Event('beforeunload', {cancelable: true});
          window.dispatchEvent(event);
          return !event.defaultPrevented;
        }""")

    def _open_unconfirmed_files(self):
        self.html = (
            self.html.replace(
                'data-source-review-confirmed="true"',
                'data-source-review-confirmed="false"',
            )
            .replace(
                '<span class="complete" data-source-page-status>2 of 2 files confirmed</span>',
                '<span class="attention" data-source-page-status>0 of 2 files confirmed</span>',
            )
            .replace(
                '<span class="complete" data-source-confirmation-status>Confirmed</span>',
                '<span class="attention" data-source-confirmation-status>Ready to confirm</span>',
            )
            .replace("Update confirmation</button>", "Confirm this file</button>")
        )
        self.page.reload()

    def test_untouched_first_entry_defaults_do_not_block_navigation(self):
        self._open_unconfirmed_files()
        self.assertTrue(self._can_leave())
        self.assertTrue(
            self.page.locator("#first [data-source-discard-changes]").is_hidden()
        )
        self.page.locator("#next-stage").click()
        self.assertIn("/schema", self.page.url)
        self.assertEqual(self.errors, [])

    def test_first_entry_edits_are_protected_before_confirmation(self):
        self._open_unconfirmed_files()
        self.page.get_by_label("first heading row").fill("2")
        self.assertFalse(self._can_leave())
        self.page.locator("#second [data-source-confirmation-action]").click()
        self.page.locator("#next-stage").click()
        self.assertEqual(self.page.url, "http://127.0.0.1:12345/sources")
        self.assertEqual(self.page.get_by_label("first heading row").input_value(), "2")
        self.assertTrue(
            self.page.locator("#first [data-source-confirmation-notice]").is_visible()
        )
        self.assertEqual(self.errors, [])

    def test_dirty_file_blocks_freeze_and_navigation_and_keeps_values(self):
        self.page.get_by_label("first heading row").fill("2")
        self.assertFalse(self._can_leave())
        self.assertEqual(
            self.page.locator("[data-source-page-status]").inner_text(),
            "Changes not confirmed",
        )
        self.assertTrue(
            self.page.locator("#first [data-source-confirmation-next]").is_hidden()
        )
        self.page.locator("#freeze button").click()
        self.assertEqual(self.page.url, "http://127.0.0.1:12345/sources")
        self.assertIsNone(self.page.locator("#freeze").get_attribute("aria-busy"))
        self.assertTrue(
            self.page.locator("#first [data-source-confirmation-notice]").is_visible()
        )
        self.page.locator("#next-stage").click()
        self.assertEqual(self.page.url, "http://127.0.0.1:12345/sources")
        self.assertEqual(self.page.get_by_label("first heading row").input_value(), "2")
        self.assertEqual(self.errors, [])

    def test_other_file_submit_cannot_drop_the_changed_file(self):
        self.page.get_by_label("first heading row").fill("2")
        self.page.locator("#second [data-source-confirmation-action]").click()
        self.assertEqual(self.page.url, "http://127.0.0.1:12345/sources")
        self.assertTrue(
            self.page.locator("#first [data-source-confirmation-action]").evaluate(
                "element => element === document.activeElement"
            )
        )
        self.assertEqual(self.page.get_by_label("first heading row").input_value(), "2")
        self.assertEqual(self.errors, [])

    def test_discard_restores_confirmation_and_allows_file_correction(self):
        self.page.get_by_label("first heading row").fill("2")
        self.page.locator("#add-file button").click()
        self.assertEqual(self.page.url, "http://127.0.0.1:12345/sources")
        self.page.locator("#first [data-source-discard-changes]").click()
        self.assertEqual(self.page.get_by_label("first heading row").input_value(), "1")
        self.assertTrue(self._can_leave())
        self.assertEqual(
            self.page.locator("[data-source-page-status]").inner_text(),
            "2 of 2 files confirmed",
        )
        self.page.locator("#add-file button").click()
        self.assertIn("/sources/files", self.page.url)
        self.assertEqual(self.errors, [])

    def test_confirming_one_changed_file_retains_the_other_file_settings(self):
        self._open_unconfirmed_files()
        self.page.get_by_label("first heading row").fill("2")
        self.page.get_by_label("second heading row").fill("3")
        self.page.locator("#first [data-source-confirmation-action]").click()
        self.assertIn("/sources/first/configure", self.page.url)
        self.assertEqual(
            self.page.get_by_label("second heading row").input_value(), "3"
        )
        self.assertEqual(
            self.page.locator("#second [data-source-confirmation-status]").inner_text(),
            "Changes not confirmed",
        )
        self.assertFalse(self._can_leave())
        self.assertIsNone(
            self.page.evaluate("sessionStorage.getItem('impodo.source.position:test')"),
            "The settings must be restored only for this immediate save response.",
        )
        self.assertEqual(self.errors, [])

    def test_temporary_retention_stores_only_file_reading_and_table_settings(self):
        self._open_unconfirmed_files()
        self.page.evaluate("""() => {
          const form = document.querySelector('#second');
          for (const [name, value] of [['csrf_token', 'csrf-secret'],
            ['api_key', 'credential-secret'], ['record_name', 'Customer value']]) {
            const input = document.createElement('input');
            input.name = name;
            input.value = value;
            input.type = 'hidden';
            form.append(input);
          }
          document.querySelector('#first').addEventListener('submit', (event) => event.preventDefault());
        }""")
        self.page.get_by_label("first heading row").fill("2")
        self.page.get_by_label("second heading row").fill("3")
        self.page.locator("#first [data-source-confirmation-action]").click()
        stored = self.page.evaluate(
            "sessionStorage.getItem('impodo.source.position:test')"
        )
        self.assertIn('"header_row_0"', stored)
        self.assertNotIn("csrf-secret", stored)
        self.assertNotIn("credential-secret", stored)
        self.assertNotIn("Customer value", stored)
        self.assertEqual(self.errors, [])
