"""Capture the authenticated Recipe target review with fictional local data."""

from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_recipe_target_matches import RecipeTargetMatchBrowserTests


def capture() -> None:
    """Render current routes with the real compiler and no external Odoo calls."""

    from playwright.sync_api import expect, sync_playwright

    fixture = RecipeTargetMatchBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        path = fixture.review_ready_delivery()
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not create an authenticated session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1024}, device_scale_factor=1, locale="en-GB")
            context.add_cookies([{
                "name": "impodo_session", "value": session, "url": base_url,
                "httpOnly": True, "sameSite": "Strict",
            }])
            page = context.new_page()
            page.goto(base_url + path, wait_until="networkidle")
            expect(page.get_by_role("heading", name="1 value needs a match")).to_be_visible()
            expect(page.get_by_role("button", name="Save matches and continue")).to_be_visible()
            expect(page.get_by_role("link", name="Open full field matcher")).to_have_count(0)
            page.get_by_role("button", name="Save matches and continue").scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -64)")
            page.screenshot(path=str(REPOSITORY_ROOT / "docs/images/user/03b-recipe-target-values.png"), full_page=False)
            browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    capture()
