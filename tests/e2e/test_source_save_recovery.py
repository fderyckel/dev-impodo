"""Qualify Stage 1 save recovery and Odoo-access returns without a running server."""

from html import escape
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from tests.support.paths import REPOSITORY_ROOT


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class SourceSaveRecoveryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True,
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL")
            or ("msedge" if sys.platform == "win32" else None),
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 900})
        self.addCleanup(self.page.close)
        self.page.set_default_timeout(5000)
        self.base = "http://127.0.0.1:12345"
        self.path = "/workspaces/recovery-fixture/derived-entities"
        self.preview_path = f"{self.path}/hierarchy/preview"
        self.save_path = f"{self.path}/hierarchy/save"
        self.storage_key = "impodo.source.journey:recovery-fixture:odoo-return"
        self.script = (
            REPOSITORY_ROOT / "src/impodo/web/static/derived-journey.js"
        ).read_text()
        self.choices_script = (
            REPOSITORY_ROOT / "src/impodo/web/static/derived-entities.js"
        ).read_text()
        self.mode, self.version, self.returned = "hold", "7", False
        self.pending, self.save_posts, self.preview_posts, self.reads = [], [], [], []
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self._route)
        self.editor = self.page.locator(
            '[data-source-editor][data-source-source-id="source-two"]'
        )
        self.state = self.page.locator("[data-source-state]")
        self.notice = self.page.locator("[data-source-notice]")
        self.accept = self.page.get_by_role("button", name="Use this table", exact=True)

    def tearDown(self):
        for route in self.pending:
            route.abort("failed")
        self.assertEqual(self.errors, [])

    def _page_html(self, *, reviewed=False, values=None):
        values = values or {}
        csrf = "fresh-server-csrf" if self.returned else "original-server-csrf"
        forms = []
        for source_id, label in [
            ("source-one", "First table"),
            ("source-two", "Second table"),
        ]:
            fields = {
                "source_dataset_id": source_id,
                "output_dataset_name": "reviewed_categories"
                if reviewed
                else f"{source_id}_default",
                "hierarchy_level_1": "group",
                "hierarchy_level_2": "subgroup",
                "expected_parent_version": self.version,
            }
            if source_id == "source-two":
                fields.update(
                    {name: value for name, value in values.items() if name in fields}
                )
            inputs = "".join(
                f'<input type="hidden" name="{name}" value="{escape(str(fields[name]), quote=True)}">'
                for name in [
                    "source_dataset_id",
                    "hierarchy_level_1",
                    "hierarchy_level_2",
                    "expected_parent_version",
                ]
            )
            review = ""
            if reviewed and source_id == "source-two":
                hidden = "".join(
                    f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'
                    for name, value in fields.items()
                )
                review = f'''<section id="hierarchy-review" data-source-review data-derived-entity-preview>
                    <h2>Review result</h2><form method="post" action="{self.save_path}" data-source-save>
                    <input type="hidden" name="csrf_token" value="{csrf}">{hidden}
                    <button type="button" data-derived-entity-target="hierarchy-source-two">Change selections</button>
                    <button type="submit">Use this table</button></form></section>'''
            forms.append(f'''<details class="source-preparation-choice" id="hierarchy-{source_id}" open>
                <summary>{label}</summary><form method="post" action="{self.preview_path}"
                data-source-editor data-source-source-id="{source_id}">
                <input type="hidden" name="csrf_token" value="{csrf}">{inputs}
                <label>Table name<input name="output_dataset_name"
                value="{escape(str(fields["output_dataset_name"]), quote=True)}" required></label>
                <button type="submit">Review hierarchy</button></form>{review}</details>''')
        saved = ""
        if self.version != "7":
            saved = """<section id="saved-source-changes"><h2>Saved changes</h2>
                <details id="hierarchy-rule-saved" data-source-saved data-source-name="reviewed_categories">
                <summary>reviewed_categories · Saved</summary></details></section>"""
        return f"""<!doctype html><html><head><meta charset="utf-8"><style>[hidden]{{display:none!important}}</style></head><body>
            <main><div data-source-journey data-source-position-key="recovery-fixture"
            data-source-current-operation="hierarchy-extraction" data-source-current-source-id="source-two">
            <section id="source-operation-choices" data-source-choices>Choose a change</section>{saved}
            <details id="hierarchy-extraction" data-source-operation="hierarchy-extraction" open>
            <summary>Hierarchy</summary>{"".join(forms)}</details>
            <a href="/workspaces/recovery-fixture/target?return_to=other">Other destination setup</a>
            <a href="/workspaces/recovery-fixture/target?return_to=source-hierarchy">Review Odoo access</a>
            <div data-source-action-bar><p data-source-notice role="alert" tabindex="-1" hidden></p>
            <span data-source-state></span><div class="actions">
            <button type="button" data-source-discard hidden>Discard unsaved change</button>
            <button type="button" data-source-add-another>Add another change</button>
            <a href="/workspaces/recovery-fixture/schema" data-source-next>Continue to Odoo data</a>
            </div></div></div></main><script>{self.choices_script}</script><script>{self.script}</script></body></html>"""

    def _route(self, route):
        request, path = route.request, urlsplit(route.request.url).path
        if request.method == "POST" and path == self.save_path:
            self.save_posts.append(request.url)
            if self.mode == "hold":
                self.pending.append(route)
            elif self.mode == "committed-abort":
                self.version = "8"
                route.abort("failed")
            elif self.mode == "abort":
                route.abort("failed")
            elif self.mode == "success":
                self.version = "8"
                route.fulfill(content_type="text/html", body="Saved")
            else:
                route.fulfill(
                    status=422,
                    content_type="text/html",
                    body='<main><div role="alert"><span>The saved source choices changed. Review this change again.</span></div></main>',
                )
        elif request.method == "POST" and path == self.preview_path:
            self.preview_posts.append(request.url)
            values = {
                name: items[0]
                for name, items in parse_qs(request.post_data or "").items()
            }
            route.fulfill(
                content_type="text/html",
                body=self._page_html(reviewed=True, values=values),
            )
        elif path == "/workspaces/recovery-fixture/schema":
            route.fulfill(
                content_type="text/html", body="<h1>Recipe Odoo requirements</h1>"
            )
        elif path == "/workspaces/recovery-fixture/target":
            self.returned = True
            self.version = "9"
            route.fulfill(
                content_type="text/html",
                body=f'''<label>Read key<input name="read_key" type="password"></label>
                <a href="{self.path}">Return to Source data</a>''',
            )
        else:
            self.reads.append(path)
            route.fulfill(
                content_type="text/html",
                body=self._page_html(reviewed=path == self.preview_path),
            )

    def _open_review(self):
        self.page.goto(f"{self.base}{self.preview_path}")
        expect(self.state).to_have_text("Ready to use")

    def test_continue_confirms_save_before_ordinary_next_page_navigation(self):
        self.mode = "success"
        # Supply the successful response after the save redirect has completed.
        # Real HTTP redirect chains are covered by the authenticated journey.
        self.page.add_init_script("""(() => {
          const original = window.fetch;
          window.fetch = async (...args) => {
            const response = await original(...args);
            if (!String(args[0]).endsWith('/hierarchy/save')) return response;
            const saved = await original('/workspaces/recovery-fixture/derived-entities');
            return {ok: saved.ok, status: saved.status, redirected: true,
              url: saved.url, text: () => saved.text()};
          };
        })();""")
        self._open_review()
        self.page.locator("[data-source-next]").click()
        try:
            self.page.wait_for_url(f"{self.base}/workspaces/recovery-fixture/schema")
        except PlaywrightTimeoutError:
            self.fail(
                f"Continue stayed at {self.page.url}; state={self.state.text_content()}; notice={self.notice.text_content()}; saves={len(self.save_posts)}; reads={self.reads}; errors={self.errors}"
            )
        expect(
            self.page.get_by_role("heading", name="Recipe Odoo requirements")
        ).to_be_visible()
        self.assertEqual(self.version, "8")
        self.assertIn(self.path, self.reads)
        self.assertEqual(len(self.save_posts), 1)

    def _lose_save(self, mode):
        self.mode = mode
        self._open_review()
        self.accept.click()
        expect(self.state).to_have_text("Save not confirmed")
        self.assertEqual(len(self.save_posts), 1)

    def test_busy_save_freezes_edits_and_blocks_another_preview(self):
        self._open_review()
        self.page.get_by_role("button", name="Change selections", exact=True).click()
        expect(self.editor).to_be_visible()
        self.accept.click()
        expect(self.state).to_have_text("Saving…")
        expect(self.editor).to_have_attribute("inert", "")
        expect(
            self.page.get_by_role("button", name="Change selections", exact=True)
        ).to_be_disabled()
        with self.assertRaises(PlaywrightTimeoutError):
            self.editor.get_by_label("Table name").fill(
                "changed_while_saving", timeout=150
            )
        self.editor.evaluate("form => form.requestSubmit()")
        self.assertEqual(self.preview_posts, [])
        self.assertEqual(len(self.save_posts), 1)
        self.pending.pop().abort("failed")
        expect(self.state).to_have_text("Save not confirmed")
        self.assertEqual(len(self.save_posts), 1)

    def test_uncommitted_readback_allows_only_an_explicit_retry(self):
        self._lose_save("abort")
        expect(self.editor).to_have_attribute("inert", "")
        self.page.get_by_role("button", name="Check saved changes", exact=True).click()
        expect(self.state).to_have_text("Ready to use")
        expect(self.editor).not_to_have_attribute("inert", "")
        self.assertEqual(self.reads[-1], self.path)
        self.assertEqual(len(self.save_posts), 1)
        self.mode = "known-rejection"
        self.accept.click()
        expect(self.notice).to_contain_text("The saved source choices changed")
        self.assertEqual(len(self.save_posts), 2)

    def test_committed_lost_response_opens_canonical_saved_changes_without_replay(self):
        self._lose_save("committed-abort")
        self.page.get_by_role("button", name="Check saved changes", exact=True).click()
        saved_link = self.page.get_by_role(
            "link", name="Open saved changes", exact=True
        )
        expect(saved_link).to_have_attribute(
            "href", f"{self.path}#saved-source-changes"
        )
        self.assertEqual(self.reads[-1], self.path)
        self.assertEqual(len(self.save_posts), 1)
        saved_link.click()
        self.page.wait_for_url(f"{self.base}{self.path}#saved-source-changes")
        expect(
            self.page.get_by_role("heading", name="Saved changes", exact=True)
        ).to_be_visible()
        expect(self.state).to_have_text("Saved")
        self.assertEqual(len(self.save_posts), 1)

    def test_known_rejection_keeps_typed_values_and_reviewed_revision(self):
        self.mode = "known-rejection"
        self.page.goto(f"{self.base}{self.path}#hierarchy-source-two")
        self.editor.get_by_label("Table name").fill("typed_categories")
        self.page.locator("[data-source-next]").click()
        self.page.wait_for_url(f"{self.base}{self.preview_path}")
        expect(self.state).to_have_text("Ready to use")
        self.accept.click()
        expect(self.notice).to_contain_text("The saved source choices changed")
        expect(self.editor.locator('[name="output_dataset_name"]')).to_have_value(
            "typed_categories"
        )
        expect(self.editor.locator('[name="expected_parent_version"]')).to_have_value(
            "7"
        )
        expect(
            self.page.locator('[data-source-save] [name="output_dataset_name"]')
        ).to_have_value("typed_categories")
        expect(
            self.page.locator('[data-source-save] [name="expected_parent_version"]')
        ).to_have_value("7")
        self.assertEqual(len(self.preview_posts), 1)
        self.assertEqual(len(self.save_posts), 1)

    def test_allowlisted_odoo_detour_restores_once_with_current_csrf(self):
        self.page.goto(f"{self.base}{self.path}#hierarchy-source-two")
        self.editor.get_by_label("Table name").fill("retained_categories")
        self.page.get_by_role(
            "link", name="Other destination setup", exact=True
        ).click()
        expect(self.notice).to_contain_text("Review and use your current change first")
        self.assertIsNone(
            self.page.evaluate("key => sessionStorage.getItem(key)", self.storage_key)
        )
        self.page.get_by_role("link", name="Review Odoo access", exact=True).click()
        self.page.wait_for_url("**/target?return_to=source-hierarchy")
        self.page.get_by_label("Read key").fill("temporary-test-key")
        draft = self.page.evaluate(
            "key => JSON.parse(sessionStorage.getItem(key))", self.storage_key
        )
        self.assertEqual(draft["sourceId"], "source-two")
        self.assertEqual(draft["action"], self.preview_path)
        self.assertNotIn("csrf_token", dict(draft["entries"]))
        self.assertNotIn("read_key", dict(draft["entries"]))
        self.assertNotIn("temporary-test-key", str(draft))
        self.page.get_by_role("link", name="Return to Source data", exact=True).click()
        self.page.wait_for_url(f"{self.base}{self.path}")
        expect(self.page.locator("[data-source-journey]")).to_have_attribute(
            "data-source-active", "hierarchy-extraction"
        )
        expect(self.editor).to_be_visible()
        expect(self.editor.locator('[name="output_dataset_name"]')).to_have_value(
            "retained_categories"
        )
        expect(self.editor.locator('[name="expected_parent_version"]')).to_have_value(
            "7"
        )
        expect(self.editor.locator('[name="csrf_token"]')).to_have_value(
            "fresh-server-csrf"
        )
        self.assertIsNone(
            self.page.evaluate("key => sessionStorage.getItem(key)", self.storage_key)
        )
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.reload()
        expect(self.editor.locator('[name="output_dataset_name"]')).to_have_value(
            "source-two_default"
        )
        expect(self.editor.locator('[name="expected_parent_version"]')).to_have_value(
            "9"
        )
        self.assertEqual(self.save_posts, [])
        self.assertEqual(self.preview_posts, [])
