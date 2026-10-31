"""Qualify Final review expectations with isolated authenticated browser data.

Set IMPODO_EXPECTATIONS_SCREENSHOT_DIRECTORY to capture the registered 1440 by
1024 workflow images while running this test. No live Odoo service is used.
"""

import os
from pathlib import Path
import sys
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.support.hierarchy_execution import compared_hierarchy
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class BusinessExpectationsJourneyTests(unittest.TestCase):
    def test_keyboard_stale_tabs_narrow_review_and_original_interrupted_approval(self):
        fixture = HierarchyWorkflowFixture(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        workspace_id, _, (target, snapshot, identities) = compared_hierarchy(
            fixture, mode="LOCAL"
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
        workspace = f"{base}/workspaces/{workspace_id}"

        def capture(name):
            output = os.environ.get("IMPODO_EXPECTATIONS_SCREENSHOT_DIRECTORY")
            if output:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / name))

        page.goto(
            workspace + "/summary/expectations#expect-product.template",
            wait_until="networkidle",
        )
        product = page.locator('[id="expect-product.template"]')
        expect(product).to_be_focused()
        advanced = product.locator("details > summary")
        advanced.focus()
        advanced.press("Enter")
        expect(product.locator("details")).to_have_attribute("open", "")
        advanced.press("Enter")
        product.locator('[name^="create_max_"]').fill("0")
        page.evaluate("window.scrollTo(0, 300)")
        capture("16i-business-expectations.png")
        product.locator('[name^="create_min_"]').fill("1")
        page.get_by_role("button", name="Save business expectations").click()
        expect(page.locator("[data-expectation-error]")).to_be_focused()
        product.locator('[name^="create_min_"]').fill("")
        expect(product.locator('[name^="create_max_"]')).to_have_value("0")
        page.get_by_role("button", name="Save business expectations").click()
        review = page.locator("[data-business-review]")
        expect(review).to_contain_text("At most 0")
        expect(review).to_contain_text("Needs review")
        review.scroll_into_view_if_needed()
        capture("16j-expected-versus-found.png")

        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        region = review.get_by_role("region")
        region.focus()
        self.assertGreater(
            region.evaluate("element => element.scrollWidth"),
            region.evaluate("element => element.clientWidth"),
        )
        region.press("ArrowRight")
        page.wait_for_timeout(300)
        self.assertGreater(region.evaluate("element => element.scrollLeft"), 0)
        page.set_viewport_size({"width": 1440, "height": 1024})

        stale = context.new_page()
        stale.goto(workspace + "/summary/expectations", wait_until="networkidle")
        old_version = stale.locator('[name="expected_version"]').input_value()
        page.goto(workspace + "/summary/expectations", wait_until="networkidle")
        for model, count in (("product.template", "6"), ("product.category", "7")):
            fieldset = page.locator(f'[id="expect-{model}"]')
            for edge in ("min", "max"):
                fieldset.locator(f'[name^="create_{edge}_"]').fill(count)
            fieldset.locator('[name^="update_max_"]').fill("0")
        page.get_by_role("button", name="Save business expectations").click()
        expect(page.locator("[data-business-review]")).to_contain_text(
            "Within expectations"
        )
        stale.get_by_role("button", name="Save business expectations").click()
        expect(stale.locator("[data-expectation-error]")).to_be_focused()
        expect(stale.locator('[name="expected_version"]')).to_have_value(old_version)
        stale.close()

        page.goto(workspace + "/load/confirm", wait_until="networkidle")
        expect(
            page.get_by_role("heading", name="Confirm the Odoo load")
        ).to_be_visible()
        approved = fixture.context.preflight.business_intent_review(
            workspace_id, snapshot.semantic_hash, actor=fixture.actor
        )
        expect(page.locator('[name="intent_assessment_hash"]')).to_have_value(
            approved.assessment_hash
        )
        capture("17c-expected-load-confirmation.png")
        page.locator("[data-load-confirmation-form]").scroll_into_view_if_needed()
        capture("17b-load-confirmation.png")

        def interrupted():
            raise RuntimeError("Fictional lost response after Odoo accepted a batch")

        target.after_create = interrupted
        with self.assertRaises((WorkspaceError, RuntimeError)):
            fixture.context.execution.execute(
                workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                expected_intent_assessment_hash=approved.assessment_hash,
                executor=target,
                actor=fixture.actor,
                **identities,
            )
        fixture.expect_business_writes(workspace_id)
        run_local_operation_sync(
            fixture.context.preflight.reassess_business_intent,
            workspace_id,
            actor=fixture.actor,
        )
        page.goto(workspace + "/load/outcome", wait_until="networkidle")
        original = page.locator("details").filter(
            has=page.get_by_text("Originally approved changes", exact=True)
        )
        original.locator(":scope > summary").click()
        expect(original).to_contain_text("Approved scope")
        expect(original).to_contain_text(
            "This saved load keeps the expectations approved when it started."
        )
        self.assertEqual(errors, [])
