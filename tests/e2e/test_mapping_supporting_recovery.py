"""Keep unsaved account mappings through inline related-data review and retry."""

import os
from dataclasses import replace
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_mapping_supporting_recovery as scenarios
from tests.support.identity import resolve_local_actor


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class MappingSupportingRecoveryBrowserTests(unittest.TestCase):
    def test_failed_choice_save_and_retry_keep_unsaved_mapping_for_both_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self.exercise_recovery(major)

    def exercise_recovery(self, major):
        fixture = scenarios.MappingSupportingRecoveryTests(methodName="runTest")
        fixture.setUp()
        server = thread = None
        try:
            workspace_id, _, _, _ = scenarios.related_review_workspace(fixture, major)
            context = fixture.app.state.context
            actor = resolve_local_actor(context)
            packet = context.schema_workspace.supporting_review_inputs(
                workspace_id, actor=actor
            )
            review = context.schema_workspace.supporting.current(
                workspace_id, actor=actor
            )
            # Start with a current page, then make its review stale after it opens.
            review = context.schema_workspace.supporting.save(
                packet,
                expected_binding_hash=packet.binding_hash,
                expected_version=review.version,
                operation_id=str(uuid4()),
                choices=review.choices,
                actor=actor,
            )
            session = fixture.client.cookies.get("impodo_session")
            fixture.client.close()
            server, thread, port = _start_server(fixture.app)
            base = f"http://127.0.0.1:{port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    browser_context = browser.new_context(
                        viewport={"width": 1440, "height": 1024}
                    )
                    browser_context.add_cookies(
                        [
                            {
                                "name": "impodo_session",
                                "value": session,
                                "url": base,
                                "httpOnly": True,
                                "sameSite": "Strict",
                            }
                        ]
                    )
                    page = browser_context.new_page()
                    page_errors = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.goto(
                        f"{base}/workspaces/{workspace_id}/mapping?step=tables&task=fields",
                        wait_until="networkidle",
                    )
                    panel = page.locator("#related-data-review")
                    expect(panel).to_be_hidden()
                    literal = page.locator('[data-scalar-mapping-row][data-target-field="field_0000"] [data-literal-value]')
                    literal.fill("Unsaved account name")
                    stale = replace(
                        review,
                        version=review.version + 1,
                        operation_id=str(uuid4()),
                        binding_hash="sha256:" + "f" * 64,
                    )
                    context.schema_workspace.schemas.save_supporting_review(
                        workspace_id,
                        stale,
                        expected_version=review.version,
                        actor=actor,
                    )
                    page.locator('.sidebar-subnav-link[href*="step=check"]').click()
                    page.locator("[data-check-mapping]").click()
                    expect(panel).to_be_visible()
                    expect(
                        panel.locator("[data-related-review-details]")
                    ).to_have_attribute("open", "")
                    expect(literal).to_have_value("Unsaved account name")
                    expect(page.locator("[data-mapping-save-outcome]")).to_be_hidden()
                    confirm = panel.locator("[data-related-review-form] > .actions").get_by_role(
                        "button", name="Confirm choices and check matches"
                    )
                    expect(confirm).to_be_in_viewport()

                    save_url = f"**/workspaces/{workspace_id}/schema/supporting/save"
                    page.route(
                        save_url,
                        lambda route: route.fulfill(
                            status=422,
                            content_type="application/json",
                            body='{"detail":"Odoo details changed; reload choices."}',
                        ),
                    )
                    confirm.click()
                    expect(
                        panel.locator("[data-related-review-error]")
                    ).to_contain_text("Odoo details changed")
                    expect(literal).to_have_value("Unsaved account name")
                    self.assertEqual(
                        context.schema_workspace.supporting.current(
                            workspace_id, actor=actor
                        ),
                        stale,
                    )
                    page.unroute(save_url)
                    panel.get_by_role("button", name="Reload choices").click()
                    expect(panel.locator("[data-related-review-error]")).to_be_hidden()
                    expect(literal).to_have_value("Unsaved account name")
                    expect(confirm).to_be_in_viewport()

                    capture = os.environ.get("IMPODO_RELATED_REVIEW_SCREENSHOT")
                    if not capture and (directory := os.environ.get("IMPODO_STAGE3_SCREENSHOT_DIR")):
                        capture = str(Path(directory) / "11o-related-data-recovery.png")
                    if capture:
                        if major == 20:
                            panel.scroll_into_view_if_needed()
                            page.evaluate("window.scrollBy(0, -70)")
                            Path(capture).parent.mkdir(parents=True, exist_ok=True)
                            page.evaluate("""async () => {
                                await document.fonts.ready;
                                let previous = null, stable = 0;
                                for (let frame = 0; frame < 120; frame += 1) {
                                    await new Promise(requestAnimationFrame);
                                    const current = `${scrollX}:${scrollY}`;
                                    stable = current === previous ? stable + 1 : 0;
                                    previous = current;
                                    if (stable >= 4) return;
                                }
                            }""")
                            page.screenshot(path=capture, animations="disabled")
                            page.set_viewport_size({"width": 390, "height": 844})
                            self.assertFalse(
                                page.evaluate(
                                    "document.documentElement.scrollWidth > innerWidth + 1"
                                )
                            )
                            page.set_viewport_size({"width": 1440, "height": 1024})
                    with page.expect_navigation(wait_until="networkidle"):
                        with page.expect_response(
                            lambda response: urlsplit(response.url).path.endswith("/mapping/save")
                            and response.request.method == "POST"
                        ) as checked:
                            confirm.click()
                    self.assertEqual(checked.value.status, 200)
                    revision = context.queries.get_mapping_revision(workspace_id)
                    self.assertEqual(
                        revision.definition.datasets[0].fields[0].literal_value,
                        "Unsaved account name",
                    )
                    expect(literal).to_have_value(
                        "Unsaved account name"
                    )
                    provider = page.locator(
                        '[data-relation-mapping-row][data-target-field="company_ids"] [data-relation-value-source]'
                    )
                    page.locator('.sidebar-subnav-link[href*="step=tables"]').click()
                    page.locator('[data-mapping-go-task="links"]').click()
                    card = page.locator('[data-relation-mapping-row][data-target-field="company_ids"]')
                    if not card.evaluate("element => element.open"):
                        card.locator(":scope > summary").click()
                    provider.select_option("")
                    page.locator('.sidebar-subnav-link[href*="step=check"]').click()
                    page.locator("[data-check-mapping]").click()
                    expect(panel).to_be_visible()
                    expect(panel).to_contain_text(
                        "Choosing Company for this load does not fill Companies"
                    )
                    expect(panel.locator("[data-related-review-form]")).to_have_count(0)
                    expect(panel).to_contain_text("Account · Companies")
                    expect(page.locator("#next-step-blockers")).not_to_contain_text(
                        "Saved changes have not been checked yet"
                    )
                    panel.get_by_role("link", name="Review this field").first.click()
                    expect(provider).to_be_visible()
                    expect(provider).to_be_focused()
                    self.assertEqual(page_errors, [])
                finally:
                    browser.close()
        finally:
            if server is not None:
                _stop_server(server, thread)
            fixture.tearDown()
