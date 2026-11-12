"""Reopen saved matches against changed lookup choices using fictional data."""

import json
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class ValueMatchVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None, headless=True
        )
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(5000)
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.saved = [
            {"source_value": "Legacy", "target_value": '["Bank","Company A"]'}
        ]
        self.targets = []
        self.source_choices = [{"value": "Legacy", "count": 3}]
        self.requests = []
        self.defer_choices = False
        self.pending = []
        self.page.route("**/*", self._route)
        self.page.goto("http://127.0.0.1:12345/mapping")
        self.page.wait_for_function("window.impodoMappingEditor !== undefined")

    def _route(self, route):
        if route.request.url.endswith("/choices"):
            self.requests.append(route.request.post_data)
            if self.defer_choices:
                self.pending.append(route)
            else:
                self._return_choices(route)
            return
        if route.request.url.endswith("/credential"):
            route.fulfill(json={"message": "Key saved."})
            return
        if route.request.method == "POST":
            entries = dict(route.request.post_data_json["entries"])
            self.saved = json.loads(entries["relation_value_matches_0_0"])
            route.fulfill(
                json={
                    "status": "committed",
                    "operation_id": entries["operation_id"],
                    "message": "Matches saved.",
                }
            )
            return
        html = """<form data-mapping-form action="/mapping" method="post">
          <div data-relation-mapping-row data-target-field="payment_method_id">
            <select name="relation_value_source_0_0" data-relation-value-source><option value="source">Source</option></select>
            <select name="relation_source_0_0"><option value="column:legacy">Legacy</option><option value="">Choose a source</option></select>
            <select name="relation_origin_0_0"><option value="target_catalog">Odoo</option></select>
            <select name="relation_key_0_0"><option value="name_company" data-scope-fields="company_id">Name and Company</option></select>
            <input name="relation_value_matches_0_0" data-value-match-storage type="hidden">
            <span data-value-match-summary></span>
            <button type="button" data-open-value-match data-value-match-kind="relationship" data-target-label="Payment Method">Match values</button>
            <button type="button" data-value-match-prerequisite hidden></button>
          </div>
          <div data-scalar-mapping-row>
            <select data-value-source><option value="source">Source</option><option value="omit">Skip</option></select>
            <select name="scalar_source_0_0"><option value="column:tracking">Tracking</option></select>
            <input data-value-match-storage type="hidden">
            <button type="button" data-open-value-match data-value-match-kind="scalar" data-target-label="Tracking">Match source values</button>
            <button type="button" data-value-match-prerequisite hidden></button>
          </div>
          <button type="submit" name="action" value="save_progress">Save progress</button>
          <span data-mapping-save-status></span>
        </form>
        <dialog data-read-credential-dialog>
          <form data-read-credential-form method="post" action="/credential">
            <input name="key" data-read-credential-input aria-label="Odoo read key">
            <p data-read-credential-error hidden></p>
            <button data-save-read-credential>Save key</button>
            <button type="button" data-close-read-credential>Cancel key prompt</button>
          </form>
        </dialog>"""
        static = REPOSITORY_ROOT / "src/impodo/web/static"
        styles = ("tokens.css", "components.css", "workflow-pages.css", "mapping.css")
        html = "<style>" + "\n".join((static / name).read_text() for name in styles) + "</style>" + html
        dialog = (
            REPOSITORY_ROOT
            / "src/impodo/web/templates/mapping/_value_match_dialog.html"
        ).read_text()
        html += dialog.replace(
            "/workspaces/{{ workspace_id }}/mapping/value-choices", "/choices"
        )
        html += (
            '<script>document.querySelector("[data-value-match-storage]").value = '
            + json.dumps(json.dumps(self.saved))
            + ";</script>"
        )
        html += """<script>window.impodoMappingSourceColumns = {
          hydrateSourceOptions() {}, releaseSourceOptions() {}, initializeLazySourceSelect() {}
        };</script>"""
        for name in (
            "app.js",
            "mapping-save-recovery.js",
            "mapping-selection-choices.js",
            "mapping-value-match-company.js",
            "mapping-blank-create.js",
            "mapping-value-match.js",
            "mapping-editor.js",
        ):
            html += "<script>" + (static / name).read_text() + "</script>"
        route.fulfill(body=html, content_type="text/html; charset=utf-8")

    def _return_choices(self, route):
        route.fulfill(
            json={
                "source_choices": self.source_choices,
                "target_choices": self.targets,
                "allow_blank": True,
                "target_checked_at": "2026-11-02T07:15:00Z",
            }
        )

    def _open(self):
        self.page.get_by_role("button", name="Match values", exact=True).click()
        self.page.locator("select[data-source-value]").wait_for()
        return self.page.locator("select[data-source-value]")

    def test_missing_source_focuses_the_field_without_opening_the_dialog(self):
        source = self.page.locator('[name="relation_source_0_0"]')
        source.select_option("")
        self.page.get_by_role("button", name="Match values", exact=True).click()
        correction = self.page.get_by_role("button", name="Choose one source column first")
        expect(correction).to_be_focused()
        correction.press("Enter")
        expect(source).to_be_focused()
        expect(self.page.locator("[data-value-match-dialog]")).to_be_hidden()
        self.assertEqual(self.requests, [])

    def test_pending_open_shows_slow_and_timeout_states_then_retries_open(self):
        self.page.set_viewport_size({"width": 390, "height": 680})
        self.page.clock.install()
        self.defer_choices = True
        with self.page.expect_request("**/choices"):
            self.page.get_by_role("button", name="Match values", exact=True).click()
        dialog = self.page.locator("[data-value-match-dialog]")
        status = dialog.get_by_role("status")
        close = dialog.get_by_role("button", name="Close dialog")
        expect(status).to_have_text("Loading choices…")
        expect(status).to_be_in_viewport()
        expect(close).to_be_in_viewport()
        expect(close).to_be_focused()
        expect(dialog.locator("[data-value-match-spinner]")).to_be_visible()
        expect(dialog.locator("[data-value-match-results]")).to_have_attribute("aria-busy", "true")
        self.page.clock.run_for(5_001)
        expect(status).to_have_text("Still waiting for choices…")
        self.page.clock.fast_forward(150_000)
        expect(status).to_have_text("Loading timed out")
        expect(dialog.locator("[data-value-match-spinner]")).to_be_hidden()
        expect(dialog.locator("[data-value-match-results]")).to_have_attribute("aria-busy", "false")
        self.pending.pop().abort()
        self.defer_choices = False
        self.targets = [{"value": self.saved[0]["target_value"], "label": "Bank (Company A)"}]
        dialog.get_by_role("button", name="Try again").click()
        expect(status).to_have_text("1 of 1 matched")
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertIn('name="refresh"\r\n\r\n0', request)

    def test_refresh_failure_and_retry_keep_an_unsaved_selection(self):
        self.page.set_viewport_size({"width": 1440, "height": 1024})
        self.targets = [{"value": "Other", "label": "Other"}]
        selected = self._open()
        selected.focus()
        selected.select_option("Other")
        self.defer_choices = True
        with self.page.expect_request("**/choices"):
            self.page.get_by_role("button", name="Refresh Odoo values").click()
        status = self.page.get_by_role("status")
        expect(status).to_have_text("Refreshing Odoo values…")
        expect(status).to_be_in_viewport()
        expect(selected).to_be_visible()
        expect(selected).to_have_value("Other")
        expect(self.page.locator("[data-use-value-matches]")).to_be_disabled()
        self.pending.pop().fulfill(status=503, json={"detail": "Odoo could not be reached."})
        expect(status).to_have_text("Couldn’t load choices · Showing previous choices")
        expect(selected).to_have_value("Other")
        expect(self.page.get_by_text("Odoo could not be reached.")).to_be_hidden()
        self.defer_choices = False
        self.page.get_by_role("button", name="Try again").click()
        expect(status).to_have_text("Odoo values refreshed · 1 of 1 matched")
        expect(selected).to_have_value("Other")
        self.assertIn('name="refresh"\r\n\r\n1', self.requests[-1])

    def test_read_key_prompt_resumes_refresh_with_the_unsaved_match(self):
        self.targets = [{"value": "Other", "label": "Other"}]
        selected = self._open()
        selected.focus()
        selected.select_option("Other")
        self.defer_choices = True
        with self.page.expect_request("**/choices"):
            self.page.get_by_role("button", name="Refresh Odoo values").click()
        expect(self.page.get_by_role("status")).to_have_text("Refreshing Odoo values…")
        self.pending.pop().fulfill(status=422, json={"read_credential_required": True})
        credential = self.page.locator("[data-read-credential-dialog]")
        expect(credential).to_be_visible()
        credential.get_by_role("textbox", name="Odoo read key").fill("fictional-key")
        self.defer_choices = False
        credential.get_by_role("button", name="Save key").click()
        expect(credential).to_be_hidden()
        expect(self.page.get_by_role("status")).to_have_text("Odoo values refreshed · 1 of 1 matched")
        expect(selected).to_have_value("Other")

    def test_old_response_cannot_replace_a_different_field(self):
        self.page.evaluate("""() => {
          const fetchChoices = window.fetch;
          window.fetch = (url, { signal, ...options }) => fetchChoices(url, options);
        }""")
        self.defer_choices = True
        with self.page.expect_request("**/choices"):
            self.page.get_by_role("button", name="Match values", exact=True).click()
        expect(self.page.get_by_role("status")).to_have_text("Loading choices…")
        stale = self.pending.pop()
        self.page.keyboard.press("Escape")
        expect(self.page.locator("[data-value-match-dialog]")).to_be_hidden()
        self.defer_choices = False
        self.targets = [{"value": "Legacy", "label": "Legacy"}]
        self.page.get_by_role("button", name="Match source values", exact=True).click()
        expect(self.page.get_by_role("status")).to_have_text("1 of 1 matched")
        stale.fulfill(status=503, json={"detail": "Old field failure."})
        expect(self.page.get_by_role("heading", name="Match source choices to Tracking")).to_be_visible()
        expect(self.page.get_by_role("status")).to_have_text("1 of 1 matched")
        expect(self.page.get_by_label("Filter Odoo choices by company")).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_empty_source_and_target_are_distinct(self):
        self.saved = []
        self.page.reload()
        self._open()
        status = self.page.get_by_role("status")
        expect(status).to_have_text("No Odoo choices available")
        self.source_choices = []
        self.page.get_by_role("button", name="Refresh Odoo values").click()
        expect(status).to_have_text("Odoo values refreshed · No source values to match")

    def test_missing_cached_choice_remains_visible_and_survives_save_and_reload(self):
        selected = self._open()
        self.assertEqual(selected.input_value(), self.saved[0]["target_value"])
        self.assertIn("needs review", selected.locator("option:checked").inner_text())
        self.assertIn(
            "saved choice needs review",
            self.page.locator("[data-value-match-status]").inner_text(),
        )
        self.page.locator("[data-use-value-matches]").click()
        self.page.get_by_role("button", name="Save progress").click()
        self.page.wait_for_function(
            "document.querySelector('[data-mapping-save-status]').textContent.includes('Matches saved')"
        )
        self.page.reload()
        self.assertEqual(self._open().input_value(), '["Bank","Company A"]')
        self.assertEqual(self.errors, [])

    def test_refresh_restores_current_label_without_changing_saved_company(self):
        self._open()
        self.targets = [{"value": '["Bank","Company A"]', "label": "Bank (Company A)"}]
        self.page.locator("[data-refresh-value-match]").click()
        self.page.wait_for_function(
            "document.querySelector('select[data-source-value] option:checked')?.textContent.includes('Bank (Company A)')"
        )
        expect(self.page.get_by_label("Filter Odoo choices by company")).to_be_hidden()
        self.assertNotIn(
            "needs review", self.page.locator("[data-value-match-status]").inner_text()
        )
        self.assertEqual(self.errors, [])

    def test_exact_suggestion_does_not_replace_a_saved_company_choice(self):
        self.targets = [{"value": "Legacy", "label": "Legacy"}]
        self.assertEqual(self._open().input_value(), '["Bank","Company A"]')

    def test_company_filter_narrows_choices_and_keeps_existing_match(self):
        company_a = '["Bank","Company A"]'
        company_b = '["Bank","Company B"]'
        self.targets = [
            {"value": company_a, "label": "Bank (Company A)"},
            {"value": company_b, "label": "Bank (Company B)"},
            {"value": '["Cash","Company A"]', "label": "Cash (Company A)"},
        ]
        selected = self._open()
        company_filter = self.page.get_by_label("Filter Odoo choices by company")
        self.assertEqual(company_filter.input_value(), "")
        company_filter.select_option("Company B")
        selected.focus()
        self.assertEqual(selected.input_value(), company_a)
        self.assertEqual(selected.locator("option:checked").inner_text(), "Bank (Company A)")
        self.assertEqual(selected.get_by_role("option", name="Cash (Company A)").count(), 0)
        selected.select_option(company_b)
        self.page.locator("[data-use-value-matches]").click()
        self.page.get_by_role("button", name="Save progress").click()
        self.page.wait_for_function(
            "document.querySelector('[data-mapping-save-status]').textContent.includes('Matches saved')"
        )
        self.page.reload()
        selected = self._open()
        self.assertEqual(selected.input_value(), company_b)
        self.assertEqual(selected.locator("option:checked").inner_text(), "Bank (Company B)")
        self.assertEqual(self.errors, [])

    def test_explicit_leave_blank_remains_selected_when_reopened(self):
        self.saved = [{"source_value": "Legacy", "action": "leave_blank"}]
        self.page.reload()
        selected = self._open()
        self.assertTrue(selected.input_value().startswith("leave-blank:"))
        self.page.locator("[data-use-value-matches]").click()
        actual = json.loads(
            self.page.locator("[data-relation-mapping-row] [data-value-match-storage]").input_value()
        )
        self.assertEqual(actual, self.saved)
        self.assertEqual(self.errors, [])

    def test_review_notice_clears_when_a_current_choice_is_selected(self):
        self.targets = [{"value": "Current", "label": "Current"}]
        selected = self._open()
        selected.focus()
        selected.select_option("Current")
        self.assertNotIn(
            "needs review", self.page.locator("[data-value-match-status]").inner_text()
        )
        self.page.locator("[data-use-value-matches]").click()
        actual = json.loads(
            self.page.locator("[data-relation-mapping-row] [data-value-match-storage]").input_value()
        )
        self.assertEqual(
            actual, [{"source_value": "Legacy", "target_value": "Current"}]
        )
