"""Capture the current Odoo-to-Odoo transfer decisions for user documentation.

The helper reuses the isolated browser workflow fixture. It authenticates on
an ephemeral loopback server, uses only fictional Odoo records and endpoints,
and writes five 1440 by 1024 PNG files under ``docs/images/user``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import (  # noqa: E402
    _start_server,
    _stop_server,
)
from tests.integration.web.test_source_workflow import (  # noqa: E402
    SourceWorkflowBrowserTests,
)


VIEWPORT = {"width": 1440, "height": 1024}
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "docs" / "images" / "user"
TEST_METHOD = "test_odoo_source_setup_skips_file_export_and_opens_schema_first"


@dataclass(frozen=True)
class CapturePoint:
    route_fragment: str
    marker: str
    output_name: str
    selector: str | None = None


CAPTURE_POINTS = (
    CapturePoint(
        "/transfer-destination#destination-connected",
        "Destination connection complete",
        "19-destination-connection.png",
        "#destination-connected",
    ),
    CapturePoint(
        "/destination-matching#matching-results",
        "Managed by destination Odoo",
        "20-destination-matching.png",
        "#matching-results .matching-result-card",
    ),
    CapturePoint(
        "/transfer-order#transfer-order-results",
        "Transfer order is ready",
        "21-transfer-order.png",
        "#transfer-order-results",
    ),
    CapturePoint(
        "/transfer-review",
        "Frozen execution scope",
        "22-transfer-review.png",
        "#review-package",
    ),
    CapturePoint(
        "/transfer-preflight",
        "Your data will go to odoo_destination",
        "23-transfer-preflight.png",
    ),
)


def _capture_route(
    app,
    *,
    session_cookie: str,
    route: str,
    marker: str,
    output: Path,
    browser_channel: str,
    selector: str | None,
) -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError as error:  # pragma: no cover - operator guidance
        raise RuntimeError(
            "Playwright is required. Run with `uv run --with playwright`."
        ) from error

    server = None
    thread = None
    previous_expected_hosts = tuple(
        (middleware, middleware.kwargs.get("expected_host"))
        for middleware in app.user_middleware
        if "expected_host" in middleware.kwargs
    )
    try:
        server, thread, port = _start_server(app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=browser_channel,
                headless=True,
            )
            context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                locale="en-GB",
            )
            context.add_cookies(
                [
                    {
                        "name": "impodo_session",
                        "value": session_cookie,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            response = page.goto(base_url + route, wait_until="networkidle")
            if response is None or response.status != 200:
                raise RuntimeError(f"Screenshot route did not load: {route}")
            decision = page.get_by_text(marker, exact=False).first
            expect(decision).to_be_visible()
            output.parent.mkdir(parents=True, exist_ok=True)
            if selector:
                region = page.locator(selector).first
                expect(region).to_be_visible()
                region.screenshot(path=str(output))
            else:
                decision.scroll_into_view_if_needed()
                page.evaluate("window.scrollBy(0, -120)")
                page.screenshot(path=str(output), full_page=False)
            context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        for middleware, expected_host in previous_expected_hosts:
            middleware.kwargs["expected_host"] = expected_host
        app.middleware_stack = None


def capture(output_directory: Path, *, browser_channel: str) -> tuple[Path, ...]:
    """Run the isolated transfer journey and capture its main decisions."""

    fixture = SourceWorkflowBrowserTests(methodName=TEST_METHOD)
    fixture.setUp()
    captured: dict[str, Path] = {}
    original_get: Callable = fixture.client.get
    try:
        session_cookie = fixture.client.cookies.get("impodo_session")
        if not session_cookie:
            raise RuntimeError("The isolated fixture did not create a session.")

        def get_and_capture(path, *args, **kwargs):
            response = original_get(path, *args, **kwargs)
            route = str(path)
            for point in CAPTURE_POINTS:
                if point.output_name in captured:
                    continue
                if point.route_fragment not in route or point.marker not in response.text:
                    continue
                output = output_directory / point.output_name
                _capture_route(
                    fixture.app,
                    session_cookie=session_cookie,
                    route=route,
                    marker=point.marker,
                    output=output,
                    browser_channel=browser_channel,
                    selector=point.selector,
                )
                captured[point.output_name] = output
            return response

        fixture.client.get = get_and_capture
        getattr(fixture, TEST_METHOD)()
        missing = [
            point.output_name
            for point in CAPTURE_POINTS
            if point.output_name not in captured
        ]
        if missing:
            raise RuntimeError(
                "The isolated journey did not reach: " + ", ".join(missing)
            )
        return tuple(captured[point.output_name] for point in CAPTURE_POINTS)
    finally:
        fixture.tearDown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
    )
    parser.add_argument(
        "--browser-channel",
        default="msedge",
        help="Installed Chromium channel for Playwright. Defaults to msedge.",
    )
    arguments = parser.parse_args()
    capture(
        arguments.output_directory.resolve(),
        browser_channel=arguments.browser_channel,
    )


if __name__ == "__main__":
    main()
