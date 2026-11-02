"""Reopen saved matches against changed lookup choices using fictional data."""

import json
import sys
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class ValueMatchVisibilityTests(unittest.TestCase):
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
        self.page = self.browser.new_page()
        self.page.set_default_timeout(5000)
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.saved = [
            {"source_value": "Legacy", "target_value": '["Bank","Company A"]'}
        ]
        self.targets = []
        self.page.route("**/*", self._route)
        self.page.goto("http://127.0.0.1:12345/mapping")
        self.page.wait_for_function("window.impodoMappingEditor !== undefined")

    def _route(self, route):
        if route.request.url.endswith("/choices"):
            route.fulfill(
                json={
                    "source_choices": [{"value": "Legacy", "count": 3}],
                    "target_choices": self.targets,
                    "allow_blank": True,
                    "target_checked_at": "2026-11-02T07:15:00Z",
                }
            )
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
            <select name="relation_source_0_0"><option value="column:legacy">Legacy</option></select>
            <select name="relation_origin_0_0"><option value="target_catalog">Odoo</option></select>
            <select name="relation_key_0_0"><option value="name_company">Name and Company</option></select>
            <input name="relation_value_matches_0_0" data-value-match-storage type="hidden">
            <span data-value-match-summary></span>
            <button type="button" data-open-value-match data-value-match-kind="relationship" data-target-label="Payment Method">Match values</button>
          </div>
          <button type="submit" name="action" value="save_progress">Save progress</button>
          <span data-mapping-save-status></span>
        </form>"""
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
        for name in ("mapping-save-recovery.js", "mapping-editor.js"):
            html += (
                "<script>"
                + (REPOSITORY_ROOT / "src/impodo/web/static" / name).read_text()
                + "</script>"
            )
        route.fulfill(body=html, content_type="text/html")

    def _open(self):
        self.page.locator("[data-open-value-match]").click()
        self.page.locator("select[data-source-value]").wait_for()
        return self.page.locator("select[data-source-value]")

    def test_missing_cached_choice_remains_visible_and_survives_save_and_reload(self):
        selected = self._open()
        self.assertEqual(selected.input_value(), self.saved[0]["target_value"])
        self.assertIn("needs review", selected.locator("option:checked").inner_text())
        self.assertIn(
            "saved choice(s) are absent",
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
        self.assertNotIn(
            "absent", self.page.locator("[data-value-match-status]").inner_text()
        )
        self.assertEqual(self.errors, [])

    def test_exact_suggestion_does_not_replace_a_saved_company_choice(self):
        self.targets = [{"value": "Legacy", "label": "Legacy"}]
        self.assertEqual(self._open().input_value(), '["Bank","Company A"]')

    def test_explicit_leave_blank_remains_selected_when_reopened(self):
        self.saved = [{"source_value": "Legacy", "action": "leave_blank"}]
        self.page.reload()
        selected = self._open()
        self.assertTrue(selected.input_value().startswith("leave-blank:"))
        self.page.locator("[data-use-value-matches]").click()
        actual = json.loads(
            self.page.locator("[data-value-match-storage]").input_value()
        )
        self.assertEqual(actual, self.saved)
        self.assertEqual(self.errors, [])

    def test_review_notice_clears_when_a_current_choice_is_selected(self):
        self.targets = [{"value": "Current", "label": "Current"}]
        selected = self._open()
        selected.focus()
        selected.select_option("Current")
        self.assertNotIn(
            "absent", self.page.locator("[data-value-match-status]").inner_text()
        )
        self.page.locator("[data-use-value-matches]").click()
        actual = json.loads(
            self.page.locator("[data-value-match-storage]").input_value()
        )
        self.assertEqual(
            actual, [{"source_value": "Legacy", "target_value": "Current"}]
        )
