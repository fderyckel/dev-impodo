"""Exercise the linked-data unsaved-changes guard in a real browser."""

import sys
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class SourceWorkflowLinkedDataGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None,
            headless=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        app_source = (
            REPOSITORY_ROOT / "src/impodo/web/static/app.js"
        ).read_text(encoding="utf-8")
        source = (
            REPOSITORY_ROOT / "src/impodo/web/static/source-workflow.js"
        ).read_text(encoding="utf-8")
        html = f"""
          <form data-linked-data-form data-single-submit>
            <select name="related_actions" id="linked-choice">
              <option value="keep" selected>Keep linked value</option>
              <option value="transfer">Transfer related records</option>
            </select>
            <div data-linked-data-unsaved-notice tabindex="-1" hidden>
              Save your linked-data choices before you save the fields.
            </div>
            <button id="save-linked" type="submit">
              Save linked-data choices
            </button>
          </form>
          <form data-capture-plan-form data-single-submit>
            <button id="save-plan" type="submit">Save bounded capture plan</button>
          </form>
          <script>{app_source}</script>
          <script>{source}</script>
        """
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route(
            "**/*",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )
        self.page.goto("http://127.0.0.1:12345/sources")

    def test_dirty_choice_blocks_capture_plan_and_warns_before_leaving(self):
        self.page.select_option("#linked-choice", "transfer")

        self.assertTrue(
            self.page.locator("[data-linked-data-unsaved-notice]").is_visible()
        )
        self.assertFalse(
            self.page.evaluate(
                """() => {
                  const event = new Event("beforeunload", {cancelable: true});
                  window.dispatchEvent(event);
                  return !event.defaultPrevented;
                }"""
            )
        )

        self.page.locator("#save-plan").click()

        self.assertEqual(
            self.page.evaluate(
                "document.activeElement.dataset.linkedDataUnsavedNotice"
            ),
            "",
        )
        self.assertTrue(self.page.locator("#save-plan").is_enabled())
        self.assertIsNone(
            self.page.locator("[data-capture-plan-form]").get_attribute(
                "aria-busy"
            )
        )
        self.assertEqual(self.page.url, "http://127.0.0.1:12345/sources")
        self.assertEqual(self.errors, [])

    def test_submitting_linked_data_clears_the_guard(self):
        self.page.select_option("#linked-choice", "transfer")
        self.page.evaluate(
            """() => {
              document.querySelector("[data-linked-data-form]").addEventListener(
                "submit", (event) => event.preventDefault()
              );
              document.querySelector("[data-linked-data-form]").requestSubmit();
            }"""
        )

        self.assertTrue(
            self.page.locator("[data-linked-data-unsaved-notice]").is_hidden()
        )
        self.assertTrue(
            self.page.evaluate(
                """() => {
                  const event = new Event("beforeunload", {cancelable: true});
                  window.dispatchEvent(event);
                  return !event.defaultPrevented;
                }"""
            )
        )
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
