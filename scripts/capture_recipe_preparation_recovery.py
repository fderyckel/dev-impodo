"""Capture returning to a saved Recipe review after session state is lost."""

from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_recipe_preparation_recovery import RecipePreparationRecoveryBrowserTests
from impodo.web.composition.preparation_job_manager import PreparationJobManager


def capture() -> None:
    from playwright.sync_api import expect, sync_playwright

    fixture = RecipePreparationRecoveryBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        application = fixture.ready_recipe()
        context = fixture.context
        context.preparation.prepare(application.workspace_id, actor=context.actor)
        manager = PreparationJobManager(fixture.root)
        fixture.addCleanup(manager.shutdown)
        context.preparation_jobs = manager
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not create an authenticated session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.fixture.fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            browser_context = browser.new_context(viewport={"width": 1440, "height": 1024}, device_scale_factor=1, locale="en-GB")
            browser_context.add_cookies([{
                "name": "impodo_session", "value": session, "url": base_url,
                "httpOnly": True, "sameSite": "Strict",
            }])
            page = browser_context.new_page()
            page.goto(f"{base_url}/projects/{application.project_id}/runs/{application.migration_run_id}", wait_until="networkidle")
            expect(page.get_by_text("Ready for review", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="Review prepared data")).to_be_visible()
            page.screenshot(path=str(REPOSITORY_ROOT / "docs/images/user/03c-recovered-recipe-review.png"), full_page=False)
            browser.close()
        if manager._workers or manager._pending:
            raise AssertionError("Returning to the run started another worker")
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    capture()
