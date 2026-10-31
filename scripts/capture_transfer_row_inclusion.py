"""Capture transfer row rules and verify keyboard controls with fictional data.

Run as ``python -m scripts.capture_transfer_row_inclusion`` from the checkout
with Playwright available. The authenticated 1440 by 1024 captures never use
an external Odoo server, live customer values, or credentials.
"""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_transfer_row_inclusion import TransferRowRuleRouteTests


def capture(output, *, browser_channel="msedge"):
    f = TransferRowRuleRouteTests("runTest")
    f.setUp()
    server = thread = None
    try:
        assert f.check().status_code == 303
        session = f.client.cookies.get("impodo_session")
        f.client.close()
        server, thread, port = _start_server(f.fixture.app)
        base = f"http://127.0.0.1:{port}"
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=browser_channel, headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1024}, locale="en-GB"
            )
            context.add_cookies(
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
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + f.path, wait_until="networkidle")
            every = page.locator('[data-row-inclusion-mode][value="all_rows"]')
            every.focus()
            page.keyboard.press("Space")
            expect(page.locator("[data-row-inclusion-builder]")).to_be_hidden()
            matching = page.locator('[data-row-inclusion-mode][value="matching_rows"]')
            matching.focus()
            page.keyboard.press("Space")
            expect(page.locator("[data-row-inclusion-builder]")).to_be_visible()
            add = page.get_by_role("button", name="Add another condition", exact=True)
            add.focus()
            page.keyboard.press("Enter")
            expect(
                page.locator("[data-row-inclusion-condition]:visible")
            ).to_have_count(2)
            remove = page.locator(
                "[data-row-inclusion-condition]:visible"
            ).last.get_by_role("button", name="Remove", exact=True)
            remove.focus()
            page.keyboard.press("Enter")
            expect(
                page.locator("[data-row-inclusion-condition]:visible")
            ).to_have_count(1)
            page.locator("[data-row-inclusion-editor]").scroll_into_view_if_needed()
            page.screenshot(path=str(output / "13a-transfer-row-rule.png"))
            page.locator(".matching-results-shell").evaluate(
                "e=>window.scrollTo(0,e.getBoundingClientRect().top+window.scrollY-24)"
            )
            expect(
                page.get_by_role("button", name="Confirm 1 rows to use")
            ).to_be_visible()
            page.screenshot(path=str(output / "13b-transfer-row-counts.png"))
            page.get_by_role("link", name="Review rows", exact=True).click()
            page.locator('select[name="outcome"]').select_option("excluded")
            page.get_by_role("button", name="Filter rows", exact=True).click()
            expect(page.get_by_role("cell", name="Bob", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "13c-transfer-row-review.png"))
            page.get_by_role(
                "link", name="Back to destination matching", exact=True
            ).click()
            button = page.get_by_role("button", name="Confirm 1 rows to use")
            button.focus()
            page.keyboard.press("Enter")
            expect(
                page.get_by_role(
                    "heading", name="Destination matching is ready", exact=True
                )
            ).to_be_visible()
            assert errors == [], errors
            print(
                "PASS: 1440x1024 authenticated captures, keyboard mode/add/remove/confirmation, and excluded-row review."
            )
            browser.close()
    finally:
        if server is not None:
            _stop_server(server, thread)
        f.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory", type=Path, default=Path("docs/images/user")
    )
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    capture(args.output_directory, browser_channel=args.browser_channel)
