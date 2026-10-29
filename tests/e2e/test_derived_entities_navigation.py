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
          <button data-derived-entity-target="combine-source-tables">Several tables describe the same records</button>
          <button data-derived-entity-target="combine-two-tables">Advanced: combine two tables</button>
          <button data-derived-entity-target="hierarchy-source-one">Change selections</button>
          <div style="height: 1000px"></div>
          <details id="lookup-extraction"><summary>Lookup form</summary></details>
          <details id="combine-source-tables" data-derived-entity-section>
            <summary>Combine several source tables</summary>
            <form data-exact-join-group-form>
              <select name="primary_dataset_id" data-group-primary-dataset>
                <option value="accounts">Customer accounts</option>
                <option value="parties">Party details</option>
              </select>
              <select data-group-primary-key data-test-primary-key="current">
                <option value="">Choose a field</option>
                <optgroup label="Customer accounts" data-group-primary-key-dataset="accounts">
                  <option value="accounts|account" data-dataset-id="accounts" selected>Customer accounts &middot; Account</option>
                </optgroup>
                <optgroup label="Party details" data-group-primary-key-dataset="parties">
                  <option value="parties|party" data-dataset-id="parties">Party details &middot; Party</option>
                </optgroup>
              </select>
              <select data-group-primary-key data-test-primary-key="retained-mismatch">
                <option value="">Not used</option>
                <optgroup label="Customer accounts" data-group-primary-key-dataset="accounts">
                  <option value="accounts|account" data-dataset-id="accounts">Customer accounts &middot; Account</option>
                </optgroup>
                <optgroup label="Party details" data-group-primary-key-dataset="parties">
                  <option value="parties|party" data-dataset-id="parties" selected>Party details &middot; Party</option>
                </optgroup>
              </select>
              <input type="checkbox" name="group_addition" value="1" data-group-addition data-dataset-id="parties">
              <select data-group-addition-key data-dataset-id="parties">
                <option value="parties|party_recid" selected>RecId</option>
              </select>
              <input type="search" data-group-field-search>
              <select data-group-field-filter>
                <option value="all">All participating fields</option>
                <option value="recommended">Recommended</option>
                <option value="decision">Needs decision</option>
                <option value="empty">Empty</option>
                <option value="selected">Selected</option>
              </select>
              <button type="button" data-group-use-recommended>Use recommended output</button>
              <button type="button" data-group-select-visible>Select visible fields</button>
              <button type="button" data-group-clear-visible>Clear visible fields</button>
              <span data-group-field-count></span>
              <span data-group-field-limit-error hidden>Too many fields</span>
              <fieldset data-group-projection-dataset="accounts">
                <div data-group-field-row data-dataset-id="accounts" data-dataset-name="Customer accounts" data-column-key="account" data-source-name="Account" data-candidate-type="string">
                  <input type="checkbox" name="group_projection" data-group-field-checkbox checked>
                  <input data-group-output-name data-default-name="Account" data-qualified-name="Customer accounts.Account" value="Account">
                  <small data-group-field-reason></small>
                </div>
                <div data-group-field-row data-dataset-id="accounts" data-dataset-name="Customer accounts" data-column-key="account_recid" data-source-name="RecId" data-candidate-type="integer">
                  <input type="checkbox" name="group_projection" data-group-field-checkbox checked>
                  <input data-group-output-name data-default-name="RecId" data-qualified-name="Customer accounts.RecId" value="RecId">
                  <small data-group-field-reason></small>
                </div>
                <div data-group-field-row data-dataset-id="accounts" data-dataset-name="Customer accounts" data-column-key="empty" data-source-name="Unused" data-candidate-type="empty">
                  <input type="checkbox" name="group_projection" data-group-field-checkbox>
                  <input data-group-output-name data-default-name="Unused" data-qualified-name="Customer accounts.Unused" value="Unused">
                  <small data-group-field-reason></small>
                </div>
              </fieldset>
              <fieldset data-group-projection-dataset="parties">
                <div data-group-field-row data-dataset-id="parties" data-dataset-name="Party details" data-column-key="party_recid" data-source-name="RecId" data-candidate-type="integer">
                  <input type="checkbox" name="group_projection" data-group-field-checkbox>
                  <input data-group-output-name data-default-name="RecId" data-qualified-name="Party details.RecId" value="Party details.RecId">
                  <small data-group-field-reason></small>
                </div>
                <div data-group-field-row data-dataset-id="parties" data-dataset-name="Party details" data-column-key="email" data-source-name="Email" data-candidate-type="string">
                  <input type="checkbox" name="group_projection" data-group-field-checkbox>
                  <input data-group-output-name data-default-name="Email" data-qualified-name="Party details.Email" value="Email">
                  <small data-group-field-reason></small>
                </div>
                <div data-group-field-row data-dataset-id="parties" data-dataset-name="Party details" data-column-key="empty" data-source-name="Unused" data-candidate-type="empty">
                  <input type="checkbox" name="group_projection" data-group-field-checkbox>
                  <input data-group-output-name data-default-name="Unused" data-qualified-name="Party details.Unused" value="Party details.Unused">
                  <small data-group-field-reason></small>
                </div>
              </fieldset>
              <button type="submit">Review complete combination</button>
            </form>
          </details>
          <details id="combine-two-tables" data-derived-entity-section>
            <summary>Advanced: combine two tables</summary>
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

    def test_grouped_join_is_keyboard_reachable_and_pairwise_form_validates_inputs(self):
        self.page.goto("http://127.0.0.1:12345/derived-entities")
        choice = self.page.get_by_role(
            "button", name="Several tables describe the same records"
        )
        choice.focus()
        self.page.keyboard.press("Enter")
        self.assertTrue(
            self.page.locator("#combine-source-tables").evaluate("el => el.open")
        )
        self.assertTrue(self.page.locator("[data-exact-join-group-form]").is_visible())
        self.page.get_by_role("button", name="Advanced: combine two tables").click()
        self.assertTrue(
            self.page.locator("#combine-two-tables").evaluate("el => el.open")
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

    def test_grouped_join_recommends_unique_non_key_fields_and_bulk_filters(self):
        self.page.goto(
            "http://127.0.0.1:12345/derived-entities#combine-source-tables"
        )
        form = self.page.locator("[data-exact-join-group-form]")
        addition = form.locator("[data-group-addition]")
        addition.check()

        party_key = form.locator(
            '[data-group-field-row][data-dataset-id="parties"]'
            '[data-column-key="party_recid"]'
        )
        party_email = form.locator(
            '[data-group-field-row][data-dataset-id="parties"]'
            '[data-column-key="email"]'
        )
        self.assertFalse(
            party_key.locator("[data-group-field-checkbox]").is_checked()
        )
        self.assertTrue(
            party_email.locator("[data-group-field-checkbox]").is_checked()
        )
        self.assertEqual(
            party_key.locator("[data-group-output-name]").input_value(),
            "Party details.RecId",
        )
        self.assertIn("3 / 200", form.locator("[data-group-field-count]").inner_text())

        form.locator("[data-group-field-filter]").select_option("decision")
        visible_decisions = form.locator(
            '[data-group-field-row]:visible [data-group-field-checkbox]'
        )
        self.assertEqual(visible_decisions.count(), 1)
        form.locator("[data-group-select-visible]").click()
        self.assertTrue(party_key.locator("[data-group-field-checkbox]").is_checked())

        form.locator("[data-group-field-filter]").select_option("empty")
        visible_empty = form.locator(
            '[data-group-field-row]:visible [data-group-field-checkbox]'
        )
        self.assertEqual(visible_empty.count(), 2)
        self.assertTrue(visible_empty.first.is_enabled())
        self.assertEqual(self.errors, [])

    def test_grouped_join_limits_primary_keys_to_the_selected_primary_table(self):
        self.page.goto(
            "http://127.0.0.1:12345/derived-entities#combine-source-tables"
        )
        form = self.page.locator("[data-exact-join-group-form]")
        primary = form.locator("[data-group-primary-dataset]")
        primary_key = form.locator('[data-test-primary-key="current"]')
        retained_mismatch = form.locator(
            '[data-test-primary-key="retained-mismatch"]'
        )
        account_option = primary_key.locator('[value="accounts|account"]')
        party_option = primary_key.locator('[value="parties|party"]')

        self.assertEqual(primary_key.input_value(), "accounts|account")
        self.assertEqual(retained_mismatch.input_value(), "")
        self.assertFalse(account_option.is_disabled())
        self.assertTrue(party_option.is_disabled())
        self.assertEqual(
            primary_key.locator("option:checked").inner_text(),
            "Customer accounts · Account",
        )

        primary.select_option("parties")
        self.assertEqual(primary_key.input_value(), "")
        self.assertTrue(account_option.is_disabled())
        self.assertFalse(party_option.is_disabled())

        primary_key.select_option("parties|party")
        self.assertEqual(primary_key.input_value(), "parties|party")
        self.assertEqual(
            primary_key.locator("option:checked").inner_text(),
            "Party details · Party",
        )

        primary.select_option("accounts")
        self.assertEqual(primary_key.input_value(), "")
        self.assertEqual(self.errors, [])

    def test_exact_join_summary_stacks_at_narrow_width(self):
        self.page.set_viewport_size({"width": 480, "height": 800})
        self.page.goto(
            "http://127.0.0.1:12345/derived-entities#combine-two-tables"
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
