"""Capture Recipe run navigation and follow its current review action."""

from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_recipe_preparation_recovery import RecipePreparationRecoveryBrowserTests
from tests.support.recipe_lifecycle import prepare_test_application


def capture() -> None:
    from playwright.sync_api import expect, sync_playwright

    fixture = RecipePreparationRecoveryBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        application = fixture.ready_recipe()
        prepare_test_application(fixture, fixture.context, application, fixture.root)
        path = f"/projects/{application.project_id}/runs/{application.migration_run_id}"
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not create an authenticated session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.fixture.fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1024}, device_scale_factor=1)
            context.add_cookies([{
                "name": "impodo_session", "value": session, "url": base_url,
                "httpOnly": True, "sameSite": "Strict",
            }])
            page = context.new_page()
            page.goto(base_url + path, wait_until="networkidle")
            sidebar = page.get_by_role("navigation", name="Impodo workflow")
            expect(sidebar.locator('a[aria-current="step"]')).to_contain_text("Review and load")
            action = page.locator("[data-run-next-action]").get_by_role("link", name="Review prepared data")
            expect(action).to_be_in_viewport()
            steps = page.get_by_role("navigation", name="Recipe run steps")
            assert steps.evaluate("element => getComputedStyle(element).display") == "grid"
            assert len(steps.evaluate("element => getComputedStyle(element).gridTemplateColumns").split()) == 3
            page.screenshot(path=str(REPOSITORY_ROOT / "docs/images/user/03b-recipe-run-review.png"))
            action.click()
            expect(page).to_have_url(base_url + f"/workspaces/{application.workspace_id}/normalization")
            expect(page.get_by_role("heading", name="Review what Impodo prepared", exact=True)).to_be_visible()
            browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    capture()
