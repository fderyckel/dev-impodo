"""Capture a saved generated-variant choice in an isolated fictional project."""

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
from tests.integration.web.test_product_supporting_review import (
    ProductSupportingReviewTests,
    product_variant_workspace,
)


def capture(output_directory: Path) -> None:
    """Save and reopen the exact Product collection that previously had no option."""
    from playwright.sync_api import expect, sync_playwright

    fixture = ProductSupportingReviewTests(methodName="runTest")
    fixture.setUp()
    server = thread = None
    try:
        workspace_id, _dataset, _key, _schema = product_variant_workspace(fixture)
        calls = tuple(fixture.readiness_calls)
        cookie = fixture.client.cookies.get("impodo_session")
        if not cookie:
            raise RuntimeError("The isolated app did not establish its session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser_context = browser.new_context(
                viewport=VIEWPORT, device_scale_factor=1
            )
            browser_context.add_cookies(
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
            page = browser_context.new_page()
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/schema", wait_until="networkidle"
            )
            review = page.locator("#supporting-data-review")
            choice = review.locator("select[name=action_0]")
            expect(choice.locator("option")).to_have_count(2)
            choice.select_option("ODOO_MANAGED")
            expect(review).to_contain_text("Odoo supplies this relationship")
            review.get_by_role("button", name="Save choices", exact=True).click()
            page.wait_for_load_state("networkidle")
            expect(review).to_contain_text("Choices saved")
            page.reload(wait_until="networkidle")
            expect(choice).to_have_value("ODOO_MANAGED")
            expect(page.locator("#matching-rules-blocked-reason")).to_have_count(0)
            review.evaluate("element => element.scrollIntoView({block: 'start'})")
            page.evaluate("window.scrollBy(0, -70)")
            output_directory.mkdir(parents=True, exist_ok=True)
            target = output_directory / "08k-product-variant-support.png"
            page.screenshot(path=str(target))
            print(f"Captured {target}")
            page.set_viewport_size({"width": 390, "height": 844})
            expect(choice).to_be_visible()
            if page.evaluate(
                "document.documentElement.scrollWidth > window.innerWidth + 1"
            ):
                raise RuntimeError(
                    "The generated-variant review overflows a narrow screen"
                )
            browser_context.close()
            browser.close()
        if tuple(fixture.readiness_calls) != calls:
            raise RuntimeError(
                "Reviewing generated variants unexpectedly contacted Odoo"
            )
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
