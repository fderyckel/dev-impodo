"""Capture the documented Match data recovery states from an isolated app.

Run this helper from the repository root with Playwright available. It creates
only fictional test data, serves the current application on an ephemeral
loopback port, authenticates through the normal launch route, and writes four
1440 by 1024 PNG files under ``docs/images/user``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys
from threading import Thread
import time

import uvicorn

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from impodo.web.security import LoopbackSecurityMiddleware


DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "docs" / "images" / "user"
VIEWPORT = {"width": 1440, "height": 1024}
INVALID_FORMULA = 'value 1= "UNI"'


def _start_server(app) -> tuple[uvicorn.Server, Thread, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    expected_host = f"127.0.0.1:{port}"
    for middleware in app.user_middleware:
        if middleware.cls is LoopbackSecurityMiddleware:
            middleware.kwargs["expected_host"] = expected_host
    app.middleware_stack = None
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="impodo-screenshot-server",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("The isolated Impodo screenshot server did not start.")
    return server, thread, port


def _stop_server(server: uvicorn.Server, thread: Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)
    if thread.is_alive():
        raise RuntimeError("The isolated Impodo screenshot server did not stop.")


def _configure_formula(page, formula: str, *, source_column_key: str) -> None:
    row = page.locator('tr[data-target-field="field_0000"]')
    row.locator("[data-value-source]").select_option("source")
    source = row.locator("[data-source-column]")
    source.focus()
    source.select_option(source_column_key)
    formula_box = row.locator("[data-rule-formula]")
    formula_box.evaluate(
        """element => {
          let current = element.parentElement;
          while (current) {
            if (current instanceof HTMLDetailsElement) current.open = true;
            current = current.parentElement;
          }
        }"""
    )
    formula_box.fill(formula)
    formula_box.blur()


def _capture(page, target: Path) -> None:
    page.screenshot(path=str(target), full_page=False)
    print(f"Captured {target.relative_to(REPOSITORY_ROOT)}")


def capture(output_directory: Path, *, browser_channel: str) -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError as error:  # pragma: no cover - operator guidance
        raise RuntimeError(
            "Playwright is required. Run with `uv run --with playwright`."
        ) from error

    output_directory.mkdir(parents=True, exist_ok=True)
    fixture = ProjectSetupBrowserTestCase(methodName="runTest")
    fixture.setUp()
    server: uvicorn.Server | None = None
    thread: Thread | None = None
    try:
        workspace_id, dataset, _business_key = fixture._mapping_ready_workspace(
            scalar_field_count=1
        )
        source_column_key = dataset.columns[1].stable_key
        session_cookie = fixture.client.cookies.get("impodo_session")
        if not session_cookie:
            raise RuntimeError("The isolated setup did not create an Impodo session.")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        mapping_url = f"{base_url}/workspaces/{workspace_id}/mapping"

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
            mapping_response = page.goto(mapping_url, wait_until="networkidle")
            formula_row = page.locator('tr[data-target-field="field_0000"]')
            if formula_row.count() != 1:
                status = mapping_response.status if mapping_response else "no response"
                summary = page.locator("body").inner_text()[:2_000]
                raise RuntimeError(
                    "The current Match data page did not render the fictional "
                    f"formula field. URL={page.url!r}; status={status}; body={summary!r}"
                )

            _configure_formula(
                page,
                INVALID_FORMULA,
                source_column_key=source_column_key,
            )
            feedback = page.locator(
                'tr[data-target-field="field_0000"] [data-formula-feedback]'
            )
            expect(feedback).to_be_visible(timeout=5_000)
            expect(feedback).to_contain_text("Must fix")
            feedback.scroll_into_view_if_needed()
            _capture(
                page,
                output_directory / "11a-mapping-formula-error.png",
            )

            page.locator("[data-save-mapping-progress]").click()
            save_status = page.locator("[data-mapping-save-status]")
            expect(save_status).to_contain_text(
                "Saved — needs attention",
                timeout=10_000,
            )
            save_status.scroll_into_view_if_needed()
            _capture(
                page,
                output_directory / "11b-mapping-saved-needs-attention.png",
            )

            newer_page = context.new_page()
            newer_page.goto(mapping_url, wait_until="networkidle")
            _configure_formula(
                newer_page,
                'value == "Newer saved choice"',
                source_column_key=source_column_key,
            )
            newer_feedback = newer_page.locator(
                'tr[data-target-field="field_0000"] [data-formula-feedback]'
            )
            expect(newer_feedback).to_be_hidden(timeout=5_000)
            newer_page.locator("[data-save-mapping-progress]").click()
            expect(newer_page.locator("[data-mapping-save-status]")).to_contain_text(
                "Progress saved",
                timeout=10_000,
            )

            page.bring_to_front()
            formula_box = page.locator(
                'tr[data-target-field="field_0000"] [data-rule-formula]'
            )
            formula_box.fill('value 1= "My retained choice"')
            formula_box.blur()
            expect(feedback).to_be_visible(timeout=5_000)
            page.locator("[data-save-mapping-progress]").click()
            expect(save_status).to_contain_text("Conflict", timeout=10_000)
            conflict = page.locator("[data-mapping-save-outcome]")
            expect(conflict).to_be_visible()
            expect(page.locator("[data-mapping-conflict-recovery]")).to_be_visible()
            conflict.scroll_into_view_if_needed()
            _capture(
                page,
                output_directory / "11c-mapping-save-conflict.png",
            )

            _stop_server(server, thread)
            server = None
            thread = None
            disconnected = page.locator("[data-server-recovery]")
            expect(disconnected).to_be_visible(timeout=22_000)
            expect(disconnected).to_contain_text("Impodo is not responding")
            _capture(
                page,
                output_directory / "11d-impodo-not-responding.png",
            )

            newer_page.close()
            context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
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
