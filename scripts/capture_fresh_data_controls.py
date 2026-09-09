"""Capture current Fresh data control prompts with authenticated fictional data."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_fresh_data_controls import FreshDataControlBrowserTests


def capture(output: Path, *, browser_channel: str) -> None:
    """Render the implemented decision point without connecting to Odoo."""

    from playwright.sync_api import expect, sync_playwright

    fixture = FreshDataControlBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        path = fixture.registered_delivery()
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not create an authenticated session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=browser_channel, headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1024}, device_scale_factor=1, locale="en-GB")
            context.add_cookies([{
                "name": "impodo_session", "value": session, "url": base_url,
                "httpOnly": True, "sameSite": "Strict",
            }])
            page = context.new_page()
            page.goto(base_url + path, wait_until="networkidle")
            control = page.locator('input[name="control_0"]')
            expect(control).to_be_visible()
            expect(control).to_have_attribute("required", "")
            control.fill("125.50")
            group = page.get_by_role("group", name="Expected totals for this delivery")
            group.evaluate("element => window.scrollTo(0, Math.max(0, element.getBoundingClientRect().top + window.scrollY - 200))")
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=False)
            browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPOSITORY_ROOT / "docs/images/user/03a-fresh-data-control-totals.png")
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    capture(args.output, browser_channel=args.browser_channel)
