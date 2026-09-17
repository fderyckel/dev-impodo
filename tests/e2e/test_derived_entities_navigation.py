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
        self.page = self.browser.new_page(viewport={"width": 900, "height": 600})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        html = f"""
          <button data-derived-entity-target="lookup-extraction">Lookup</button>
          <button data-derived-entity-target="hierarchy-extraction">Hierarchy</button>
          <button data-derived-entity-target="hierarchy-source-one">Change selections</button>
          <div style="height: 1000px"></div>
          <details id="lookup-extraction"><summary>Lookup form</summary></details>
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
