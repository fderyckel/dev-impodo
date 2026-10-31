"""Capture a combined product table linked to a hierarchy from its third input."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _start_server,
    _stop_server,
)
from tests.integration.web.test_combined_hierarchy import CombinedHierarchyTests


def capture(output_directory: Path) -> None:
    """Reopen the persisted mapping in an authenticated fictional browser."""
    from playwright.sync_api import expect, sync_playwright

    fixture = CombinedHierarchyTests(methodName="runTest")
    fixture.setUp()
    server = thread = None
    try:
        workspace_id, _, _, _ = fixture.combined(2)
        cookie = fixture.client.cookies.get("impodo_session")
        if not cookie:
            raise RuntimeError("The isolated app did not establish its session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            context.add_cookies(
                [
                    {
                        "name": "impodo_session",
                        "value": cookie,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/mapping",
                wait_until="networkidle",
            )
            page.get_by_role(
                "link", name="Open this table's fields", exact=True
            ).last.click()
            page.wait_for_load_state("networkidle")
            relation = page.locator(
                '[data-relation-mapping-row][data-target-field="categ_id"]'
            )
            expect(relation).to_contain_text("Selected categories path")
            page.reload(wait_until="networkidle")
            expect(relation).to_contain_text("Selected categories path")
            relation.evaluate("element => element.scrollIntoView({block: 'start'})")
            page.evaluate("window.scrollBy(0, -75)")
            output_directory.mkdir(parents=True, exist_ok=True)
            target = output_directory / "12b-combined-hierarchy-link.png"
            page.screenshot(path=str(target))
            print(f"Captured {target}")
            context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory", type=Path, default=REPOSITORY_ROOT / "docs/images/user"
    )
    capture(parser.parse_args().output_directory.resolve())
