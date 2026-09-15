"""Capture the interrupted-load decision with isolated fictional records."""

from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_load_recovery import LoadRecoveryBrowserTests


def capture() -> None:
    from playwright.sync_api import expect, sync_playwright

    fixture = LoadRecoveryBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        with fixture.recovery_context():
            session = fixture.client.cookies.get("impodo_session")
            if not session:
                raise RuntimeError("The isolated app did not establish its session")
            server, thread, port = _start_server(fixture.app)
            base_url = f"http://127.0.0.1:{port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                browser_context = browser.new_context(
                    viewport={"width": 1440, "height": 1024},
                    device_scale_factor=1, locale="en-GB",
                )
                browser_context.add_cookies([{
                    "name": "impodo_session", "value": session, "url": base_url,
                    "httpOnly": True, "sameSite": "Strict",
                }])
                page = browser_context.new_page()
                page.goto(
                    f"{base_url}/workspaces/{fixture.workspace.workspace_id}/load/outcome",
                    wait_until="networkidle",
                )
                expect(page.get_by_role("button", name="Assess and resume interrupted load")).to_be_visible()
                page.screenshot(
                    path=str(REPOSITORY_ROOT / "docs/images/user/18b-load-recovery.png"),
                    full_page=False,
                )
                browser.close()
            if fixture.events:
                raise AssertionError("Opening recovery started an Odoo operation")
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    capture()
