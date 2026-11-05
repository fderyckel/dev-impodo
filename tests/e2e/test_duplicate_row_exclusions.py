"""Qualify duplicate comparison, preview recovery and saved row exclusions.

An isolated authenticated fixture supplies every source and Odoo read. Set
IMPODO_DUPLICATE_ROWS_SCREENSHOT_DIRECTORY to capture the reviewed dialog.
"""

import os
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.identity_group_review import duplicate_identity_workspace


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class DuplicateRowJourneyTests(unittest.TestCase):
    def test_compare_preview_retry_apply_and_unsaved_work(self):
        fixture = HierarchyWorkflowFixture("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, dataset, check, reads = duplicate_identity_workspace(
            fixture,
            repeated_count=7,
            unique_source_ids=True,
            source_identity_names=("Reference",),
        )
        self.enterContext(
            patch(
                "impodo.web.routers.mapping._read_readiness_snapshots",
                side_effect=lambda context, state, requirements, **kwargs: (
                    fixture.identity_group_reader(requirements)
                ),
            )
        )
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            headless=True,
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL")
            or ("msedge" if sys.platform == "win32" else None),
        )
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        context.add_cookies(
            [
                {
                    "name": "impodo_session",
                    "value": fixture.client.cookies.get("impodo_session"),
                    "url": base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(
            base + f"/workspaces/{workspace_id}/mapping", wait_until="networkidle"
        )
        review = page.locator("[data-duplicate-rows-url]").first

        # A genuine blocking policy must remain explained after a checkbox
        # changes. Inject only that presentation state, without saving a rule.
        def explain_block_once(route):
            response = route.fetch()
            payload = response.json()
            payload["unavailable"] = "The existing row selection needs review."
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(payload)
            )

        page.route("**/mapping/duplicate-rows?*", explain_block_once, times=1)
        review.focus()
        review.press("Enter")
        dialog = page.get_by_role("dialog", name="Compare duplicate rows")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("checkbox")).to_have_count(7)
        dialog.get_by_role(
            "checkbox", name="Exclude source position 1", exact=True
        ).check()
        expect(dialog.get_by_role("alert")).to_contain_text(
            "existing row selection needs review"
        )
        expect(
            dialog.get_by_role("button", name="Review exclusions", exact=True)
        ).to_be_disabled()
        dialog.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(review).to_be_focused()
        review.press("Enter")
        expect(dialog.get_by_role("checkbox")).to_have_count(7)
        expect(dialog.get_by_role("checkbox").first).not_to_be_checked()
        expect(dialog.get_by_role("combobox")).to_have_count(0)
        expect(dialog.get_by_text("Version 1", exact=True)).to_be_visible()
        self.assertEqual(page.locator(".duplicate-value-differs").count(), 14)
        dialog.get_by_role(
            "checkbox", name="Exclude source position 1", exact=True
        ).check()
        dialog.get_by_role(
            "checkbox", name="Exclude source position 2", exact=True
        ).check()
        # A repeated matching identifier must not disable the checkbox action.
        # The server finds and proves the exact row identifier automatically.
        expect(
            dialog.get_by_role("button", name="Review exclusions", exact=True)
        ).to_be_enabled()

        # A failed preview must leave the selections available for the retry.
        def fail_once(route):
            route.fulfill(
                status=409,
                content_type="application/json",
                body='{"detail":"Retry this preview."}',
            )

        page.route("**/mapping/duplicate-rows", fail_once, times=1)
        dialog.get_by_role("button", name="Review exclusions", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("Retry this preview")
        expect(
            dialog.get_by_role("checkbox", name="Exclude source position 1", exact=True)
        ).to_be_checked()
        dialog.get_by_role("button", name="Try again", exact=True).click()
        expect(dialog.get_by_role("region", name="Exclusion preview")).to_contain_text(
            "Exclude 2 selected rows; keep 9"
        )
        expect(dialog.get_by_role("region", name="Exclusion preview")).to_contain_text(
            "exclusion rule uses SourceId"
        )
        expect(
            dialog.get_by_role("button", name="Apply exclusions and check identities")
        ).to_be_focused()
        output = os.environ.get("IMPODO_DUPLICATE_ROWS_SCREENSHOT_DIRECTORY")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(directory / "11o-mapping-duplicate-exclusions.png")
            )
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        expect(
            dialog.get_by_role("button", name="Apply exclusions and check identities")
        ).to_be_visible()

        # Commit the request, then lose its response. A retry must recover the
        # receipt rather than save a second draft or discard the selection.
        def lose_save_response(route):
            response = route.fetch()
            self.assertEqual(response.status, 200)
            route.abort()

        page.route("**/mapping/duplicate-rows", lose_save_response, times=1)
        dialog.get_by_role(
            "button", name="Apply exclusions and check identities"
        ).click()
        expect(dialog.get_by_role("alert")).to_contain_text("same save outcome")
        expect(dialog.get_by_role("checkbox").first).to_be_disabled()
        dialog.get_by_role("button", name="Try again", exact=True).click()
        expect(dialog).not_to_be_visible()
        page.wait_for_url("**/*order_check=*")
        expect(page.locator(".identity-health-card .status").first).to_contain_text(
            "needs attention", timeout=15000
        )
        draft = fixture.context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(draft.version, 2)
        self.assertEqual(
            draft.definition.datasets[0].source_identity_column_keys,
            (
                next(
                    column.stable_key
                    for column in dataset.columns
                    if column.source_name == "Reference"
                ),
            ),
        )
        self.assertEqual(
            draft.definition.datasets[0].row_inclusion.conditions[0].comparison_value,
            "ROW-1, ROW-2",
        )
        self.assertEqual(len(reads), 2)
        current = fixture.context.matching_order.current_check(
            workspace_id, actor=fixture.actor
        )
        self.assertEqual(current.identity_results[0].source_row_count, 9)
        self.assertEqual(current.identity_results[0].source_repeated_row_count, 5)
        # Returning to the page shows the saved exclusion rule, and an unsaved
        # change cannot be overwritten by a modal command based on saved data.
        page.set_viewport_size({"width": 1440, "height": 1024})
        page.goto(
            base + f"/workspaces/{workspace_id}/mapping", wait_until="networkidle"
        )
        expect(page.locator('input[name="row_inclusion_value_0_0"]')).to_have_value(
            "ROW-1, ROW-2"
        )
        page.get_by_role("radio", name="Use every row", exact=True).check()
        page.locator("[data-duplicate-rows-url]").first.click()
        expect(dialog.get_by_role("alert")).to_contain_text("unsaved matching choices")
        dialog.get_by_role("button", name="Close duplicate review").click()
        expect(
            page.get_by_role("radio", name="Use every row", exact=True)
        ).to_be_checked()
        self.assertEqual(
            fixture.context.queries.get_mapping_working_draft(workspace_id).version, 2
        )
        self.assertEqual(errors, [])
