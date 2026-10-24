"""Check Stage 1 navigation in a real browser without a running server."""

import sys
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class DerivedEntitiesNavigationTests(unittest.TestCase):
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
        script = (
            REPOSITORY_ROOT / "src/impodo/web/static/derived-entities.js"
        ).read_text(encoding="utf-8")
        styles = (
            REPOSITORY_ROOT / "src/impodo/web/static/workflow-pages.css"
        ).read_text(encoding="utf-8")
        self.page = self.browser.new_page(viewport={"width": 900, "height": 600})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        html = f"""
          <style>{styles}</style>
          <button data-derived-entity-target="lookup-extraction">Lookup</button>
          <button data-derived-entity-target="hierarchy-extraction">Hierarchy</button>
          <button data-derived-entity-target="combine-source-tables">Combine columns from multiple tables</button>
          <button data-derived-entity-target="hierarchy-source-one">Change selections</button>
          <div style="height: 1000px"></div>
          <details id="lookup-extraction"><summary>Lookup form</summary></details>
          <details id="combine-source-tables" data-derived-entity-section>
            <summary>Combine source tables</summary>
            <form data-exact-join-form>
              <label>Base dataset
                <select data-exact-join-input="left" name="left_dataset_id">
                  <option value="accounts" data-physical="true">Customer accounts</option>
                  <option value="parties" data-physical="true">Party details</option>
                </select>
              </label>
              <small id="exact-join-grain"></small>
              <label>Added dataset
                <select data-exact-join-input="right" name="right_dataset_id">
                  <option value="accounts" data-physical="true">Customer accounts</option>
                  <option value="parties" data-physical="true" selected>Party details</option>
                </select>
              </label>
              <select data-exact-key-side="left"><option value=""></option><option value="account" data-dataset-id="accounts">Account</option><option value="party" data-dataset-id="parties">Party</option></select>
              <select data-exact-key-side="right"><option value=""></option><option value="account" data-dataset-id="accounts">Account</option><option value="party" data-dataset-id="parties">Party</option></select>
              <fieldset data-exact-projection-dataset="accounts"><legend>Customer accounts</legend><input type="checkbox" name="projection" value="0"></fieldset>
              <fieldset data-exact-projection-dataset="parties"><legend>Party details</legend><input type="checkbox" name="projection" value="1"></fieldset>
              <label data-exact-use-side="left">Base use<select><option>Preparation only</option></select></label>
              <label data-exact-use-side="right">Added use<select><option>Preparation only</option></select></label>
              <div class="join-summary-flow"><div>Base</div><div class="relationship-connector">combine</div><div>Added</div><div class="relationship-connector">creates</div><div>Output</div></div>
              <div class="metric-grid exact-join-metrics"><div>1</div><div>2</div><div>3</div></div>
            </form>
          </details>
          <details id="hierarchy-extraction"><summary>Hierarchy form</summary>
            <details id="hierarchy-source-one"><summary>Source table</summary></details>
            <section id="hierarchy-preview" data-derived-entity-preview>Preview</section>
          </details>
          <script>{script}</script>
        """
        self.page.route(
            "**/*",
            lambda route: route.fulfill(body=html, content_type="text/html"),
        )

    def test_choices_and_change_selections_do_not_add_back_button_steps(self):
        self.page.goto("http://127.0.0.1:12345/derived-entities")
        initial_history = self.page.evaluate("history.length")
        self.page.get_by_role("button", name="Hierarchy").click()
        self.assertTrue(self.page.locator("#hierarchy-extraction").evaluate("el => el.open"))
        self.page.get_by_role("button", name="Lookup").click()
        self.assertTrue(self.page.locator("#lookup-extraction").evaluate("el => el.open"))
        self.page.get_by_role("button", name="Change selections").click()
        self.assertTrue(self.page.locator("#hierarchy-source-one").evaluate("el => el.open"))
        self.assertEqual(self.page.evaluate("history.length"), initial_history)
        self.assertEqual(self.page.evaluate("location.hash"), "")
        self.assertEqual(self.errors, [])

    def test_return_link_opens_the_nested_form(self):
        self.page.goto(
            "http://127.0.0.1:12345/derived-entities#hierarchy-source-one"
        )
        self.assertTrue(self.page.locator("#hierarchy-extraction").evaluate("el => el.open"))
        self.assertTrue(self.page.locator("#hierarchy-source-one").evaluate("el => el.open"))
        self.assertEqual(self.errors, [])

    def test_preview_response_is_revealed_without_a_fragment(self):
        self.page.goto("http://127.0.0.1:12345/derived-entities")
        self.page.wait_for_function("window.scrollY > 0")
        self.assertTrue(self.page.locator("#hierarchy-extraction").evaluate("el => el.open"))
        self.assertTrue(self.page.locator("#hierarchy-preview").is_visible())
        self.assertEqual(self.errors, [])

    def test_exact_join_is_keyboard_reachable_and_validates_distinct_inputs(self):
        self.page.goto("http://127.0.0.1:12345/derived-entities")
        choice = self.page.get_by_role(
            "button", name="Combine columns from multiple tables"
        )
        choice.focus()
        self.page.keyboard.press("Enter")
        self.assertTrue(
            self.page.locator("#combine-source-tables").evaluate("el => el.open")
        )
        right = self.page.locator('[name="right_dataset_id"]')
        right.select_option("accounts")
        self.assertFalse(
            self.page.locator("[data-exact-join-form]").evaluate(
                "form => form.checkValidity()"
            )
        )
        self.assertIn("different", right.evaluate("el => el.validationMessage"))
        right.select_option("parties")
        self.assertTrue(
            self.page.locator("[data-exact-join-form]").evaluate(
                "form => form.checkValidity()"
            )
        )
        self.assertEqual(self.errors, [])

    def test_exact_join_summary_stacks_at_narrow_width(self):
        self.page.set_viewport_size({"width": 480, "height": 800})
        self.page.goto(
            "http://127.0.0.1:12345/derived-entities#combine-source-tables"
        )
        summary_columns = self.page.locator(".join-summary-flow").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns"
        )
        metric_columns = self.page.locator(".exact-join-metrics").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns"
        )
        self.assertNotIn(" ", summary_columns)
        self.assertNotIn(" ", metric_columns)
        self.assertEqual(self.errors, [])
