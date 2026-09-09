"""Capture Production values and interrupted setup from the authenticated UI."""

from pathlib import Path
import sys
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impodo.domain.run.contracts import MigrationRunPlanningError
from scripts.capture_match_data_recovery_screenshots import _start_server
from tests.integration.web.test_production_readiness import ProductionReadinessBrowserTests


def capture() -> None:
    from playwright.sync_api import expect, sync_playwright

    fixture = ProductionReadinessBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        fixture.ready_production()
        context = fixture.context
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not create an authenticated session")
        fixture.client.close()
        with patch.object(context.run_planning, "target_evidence_from_workspace", return_value=(fixture.schema, None)), \
                patch.object(context, "write_identity_probe", return_value=fixture.identity):
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
                page.goto(base_url + fixture.url, wait_until="networkidle")
                page.locator('[name="control_0"]').fill("200.00")
                expect(page.get_by_role("button", name="Create Production work areas")).to_be_visible()
                page.locator("form.form-card").scroll_into_view_if_needed()
                page.screenshot(path=str(REPOSITORY_ROOT / "docs/images/user/05a-production-readiness.png"), full_page=False)
                page.locator('[name="write_api_key"]').fill("fictional-production-write")
                with patch.object(context.run_planning.repository, "commit_provisioning",
                                  side_effect=MigrationRunPlanningError("Setup stopped before its final save. Continue below.")):
                    page.get_by_role("button", name="Create Production work areas").click()
                    expect(page.get_by_role("button", name="Finish Production setup")).to_be_visible(timeout=60000)
                page.locator("section.next-action-card").scroll_into_view_if_needed()
                page.screenshot(path=str(REPOSITORY_ROOT / "docs/images/user/05b-production-resume.png"), full_page=False)
                # TestClient covers resume; leave this capture focused on its decision.
                browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                server.should_exit = True
                thread.join(timeout=15)
                if thread.is_alive():
                    server.force_exit = True
                    thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("The isolated screenshot server did not stop")
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    capture()
