"""Capture load policy and pause controls with isolated fictional evidence."""

from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impodo.domain.execution.load_control import LoadControl, RejectionPolicy
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web.test_load_recovery import LoadRecoveryBrowserTests


def capture() -> None:
    """Render current confirmation and a checked paused load without Odoo calls."""
    from playwright.sync_api import expect, sync_playwright

    fixture = LoadRecoveryBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        control = LoadControl(policy=RejectionPolicy.CONTINUE)
        for _ in range(10):
            control = control.settle("res.partner", "CREATE", rejected=True)
        fixture.run.load_control = control
        fixture.run.rejected_count = fixture.run.failed_count = 10
        fixture.run.total_count = fixture.snapshot.write_count = 15
        fixture.run.planned_count = 5
        fixture.run.committed_count = fixture.run.in_flight_count = 0
        fixture.snapshot.counts["CREATE"] = 15
        fixture.recovery_plan.load_control_hash = control.content_hash
        fixture.recovery_plan.resume_count = 5
        fixture.recovery_plan.total_count = 15
        fixture.recovery_plan.waiting_count = 5
        fixture.recovery_plan.already_accepted_count = 0
        fixture.recovery_plan.uncertain_count = 0
        fixture.recovery_plan.confirmed_after_interruption_count = 0
        with fixture.recovery_context():
            fixture.check()
            session = fixture.client.cookies.get("impodo_session")
            if not session:
                raise RuntimeError("The fixture has no authenticated browser session")
            server, thread, port = _start_server(fixture.app)
            base_url = f"http://127.0.0.1:{port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                browser_context = browser.new_context(
                    viewport={"width": 1440, "height": 1024},
                    device_scale_factor=1,
                    locale="en-GB",
                )
                browser_context.add_cookies(
                    [
                        {
                            "name": "impodo_session",
                            "value": session,
                            "url": base_url,
                            "httpOnly": True,
                            "sameSite": "Strict",
                        }
                    ]
                )
                page = browser_context.new_page()
                page.goto(
                    f"{base_url}/workspaces/{fixture.workspace.workspace_id}/load/outcome",
                    wait_until="networkidle",
                )
                expect(
                    page.get_by_role(
                        "button", name="Continue with the next small group"
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("button", name="Stop further uploads")
                ).to_be_visible()
                page.locator("#load-recovery-title").scroll_into_view_if_needed()
                page.screenshot(
                    path=str(REPOSITORY_ROOT / "docs/images/user/18c-load-paused.png"),
                    full_page=False,
                )
                fixture.preview.current_run = None
                fixture.preview.can_load = True
                fixture.preview.can_complete_without_load = False
                fixture.preview.deferred_create_count = 0
                fixture.preview.business_intent_assessment_hash = "sha256:" + "c" * 64
                fixture.preview.current_execution_status = ""
                page.goto(
                    f"{base_url}/workspaces/{fixture.workspace.workspace_id}/load/confirm",
                    wait_until="networkidle",
                )
                choice = page.get_by_label("If Odoo rejects a record")
                expect(choice).to_be_visible()
                choice.select_option("CONTINUE")
                choice.scroll_into_view_if_needed()
                page.screenshot(
                    path=str(REPOSITORY_ROOT / "docs/images/user/18d-load-policy.png"),
                    full_page=False,
                )
                browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.tearDown()
    capture_mapping_coverage()


def capture_mapping_coverage() -> None:
    """Capture honest Stage 3 coverage and its reviewed exclusion navigation."""
    from playwright.sync_api import expect, sync_playwright
    from tests.integration.web.test_mapping_constraint_coverage import (
        MappingConstraintCoverageBrowserTests,
    )

    fixture = MappingConstraintCoverageBrowserTests()
    fixture.setUp()
    server = thread = None
    try:
        workspace_id = fixture.configure()
        session = fixture.client.cookies.get("impodo_session")
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            browser_context = browser.new_context(
                viewport={"width": 1440, "height": 1024},
                device_scale_factor=1,
                locale="en-GB",
            )
            browser_context.add_cookies(
                [
                    {
                        "name": "impodo_session",
                        "value": session,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = browser_context.new_page()
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/mapping",
                wait_until="networkidle",
            )
            coverage = page.locator("[data-odoo-validation-coverage]")
            expect(coverage).to_be_visible()
            coverage.scroll_into_view_if_needed()
            page.screenshot(
                path=str(
                    REPOSITORY_ROOT / "docs/images/user/16i-odoo-rule-coverage.png"
                ),
                full_page=False,
            )
            browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    capture()
