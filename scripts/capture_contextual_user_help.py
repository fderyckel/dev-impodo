"""Capture and exercise contextual guide links using authenticated fictional data.

Run with ``uv run --frozen --group quality --with playwright python
scripts/capture_contextual_user_help.py``. The browser uses installed Edge.
GitHub navigation is intercepted so this review needs no remote documentation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _start_server,
    _stop_server,
)
from tests.support.browser_scenarios import (
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.stage2_matching_rules import (
    install_stage2_schema,
    representative_stage2_models,
)

OUTPUT = REPOSITORY_ROOT / "docs/images/user"


def capture() -> None:
    """Review guide links, unsaved choices, focus, and desktop/narrow placement."""

    from playwright.sync_api import expect, sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)
    fixture = ProjectSetupBrowserTestCase(methodName="runTest")
    fixture.setUp()
    server = thread = None
    try:
        workspace_id, _dataset, _key = fixture._mapping_ready_workspace(
            scalar_field_count=1,
            relationship_field_count=1,
            connection_mode=OdooConnectionMode.REMOTE,
        )
        schema_workspace_id, _dataset, _key = fixture._mapping_ready_workspace(
            scalar_field_count=1,
            connection_mode=OdooConnectionMode.REMOTE,
        )
        install_stage2_schema(
            fixture,
            schema_workspace_id,
            representative_stage2_models(),
            hash_character="7",
        )
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not establish its session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            browser_context = browser.new_context(
                viewport=VIEWPORT,
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
            browser_context.route(
                "https://github.com/**",
                lambda route: route.fulfill(body="<h1>Documentation preview</h1>"),
            )
            page = browser_context.new_page()
            for path, help_id, filename in (
                (
                    f"{workspace_id}/derived-entities",
                    "source-composition",
                    "06g-source-decision-help.png",
                ),
                (
                    f"{schema_workspace_id}/schema",
                    "odoo-identity",
                    "08h-odoo-decision-help.png",
                ),
                (
                    f"{workspace_id}/mapping",
                    "source-identity",
                    "09d-matching-decision-help.png",
                ),
                (
                    f"{workspace_id}/mapping",
                    "relationships",
                    "09e-relationship-decision-help.png",
                ),
            ):
                page.goto(f"{base_url}/workspaces/{path}", wait_until="networkidle")
                link = page.locator(f'[data-user-help-id="{help_id}"]')
                expect(link).to_be_visible()
                link.scroll_into_view_if_needed()
                page.evaluate("window.scrollBy(0, -160)")
                expect(link).to_have_attribute("target", "_blank")
                box = link.bounding_box()
                assert box and box["width"] >= 44 and box["height"] >= 44
                page.screenshot(path=str(OUTPUT / filename), full_page=False)

            # An unsaved row-selection change survives opening documentation.
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/mapping",
                wait_until="networkidle",
            )
            page.locator('[data-row-inclusion-mode][value="matching_rows"]').check()
            choice = page.locator('[data-row-inclusion-mode][value="matching_rows"]')
            link = page.locator('[data-user-help-id="rows-to-use"]')
            url_before = page.url
            with page.expect_popup() as popup_info:
                link.click()
            popup = popup_info.value
            popup.wait_for_load_state()
            assert "#choose-which-source-rows-belong-to-the-migration" in popup.url
            expect(choice).to_be_checked()
            assert page.url == url_before
            popup.close()
            link.focus()
            expect(link).to_be_focused()

            # Links remain reachable beside their headings at a true phone width.
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            for help_id in ("rows-to-use", "source-identity", "relationships"):
                link = page.locator(f'[data-user-help-id="{help_id}"]')
                link.scroll_into_view_if_needed()
                box = link.bounding_box()
                assert box and 0 <= box["x"] and box["x"] + box["width"] <= 390
            page.locator(
                '[data-user-help-id="source-identity"]'
            ).scroll_into_view_if_needed()
            with NamedTemporaryFile(
                prefix="impodo-matching-help-", suffix=".png", delete=False
            ) as narrow_capture:
                narrow_path = narrow_capture.name
            page.screenshot(path=narrow_path, full_page=False)
            print(f"Phone layout checked: {narrow_path}")
            browser_context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()

if __name__ == "__main__":
    capture()
