"""Capture the exact-join review from an isolated fictional customer workspace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _capture,
    _start_server,
    _stop_server,
)
from tests.integration.web.test_exact_join_workflow import ExactJoinBrowserTests

DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "docs" / "images" / "user"


def _column(dataset, name: str):
    return next(column for column in dataset.columns if column.source_name == name)


def _select_output_field(section, source_name: str, output_name: str) -> None:
    row = section.locator("tbody tr").filter(has_text=source_name).first
    row.locator('input[name="projection"]').check()
    row.locator('input[name="projection_name"]').fill(output_name)


def capture(output_directory: Path, *, browser_channel: str) -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError as error:  # pragma: no cover - operator guidance
        raise RuntimeError(
            "Playwright is required. Run with `uv run --with playwright`."
        ) from error

    output_directory.mkdir(parents=True, exist_ok=True)
    fixture = ExactJoinBrowserTests(methodName="runTest")
    fixture.setUp()
    server = None
    thread = None
    try:
        workspace_id, _selection, by_name = fixture._frozen_customer_workspace()
        left = by_name["customer_accounts"]
        right = by_name["party_details"]
        session_cookie = fixture.client.cookies.get("impodo_session")
        if not session_cookie:
            raise RuntimeError("The isolated setup did not create an Impodo session.")
        fixture.client.close()

        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=browser_channel,
                headless=True,
            )
            browser_context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                locale="en-GB",
            )
            browser_context.add_cookies(
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
            page = browser_context.new_page()
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/derived-entities",
                wait_until="networkidle",
            )
            page.get_by_role(
                "button", name="Combine columns from multiple tables"
            ).click()
            form = page.locator("#combine-source-tables")
            expect(form).to_have_attribute("open", "")
            form.locator('input[name="output_dataset_name"]').fill(
                "customer_contacts"
            )
            form.locator('select[name="left_dataset_id"]').select_option(
                left.dataset_id
            )
            form.locator('select[name="right_dataset_id"]').select_option(
                right.dataset_id
            )
            form.locator('select[name="key_left_1"]').select_option(
                f'{left.dataset_id}|{_column(left, "Partition").stable_key}'
            )
            form.locator('select[name="key_right_1"]').select_option(
                f'{right.dataset_id}|{_column(right, "Partition").stable_key}'
            )
            form.locator('select[name="key_left_2"]').select_option(
                f'{left.dataset_id}|{_column(left, "Party").stable_key}'
            )
            form.locator('select[name="key_right_2"]').select_option(
                f'{right.dataset_id}|{_column(right, "RecId").stable_key}'
            )
            form.locator('select[name="key_type_2"]').select_option("integer")

            left_fields = form.locator(
                f'[data-exact-projection-dataset="{left.dataset_id}"]'
            )
            right_fields = form.locator(
                f'[data-exact-projection-dataset="{right.dataset_id}"]'
            )
            _select_output_field(left_fields, "AccountNum", "account_number")
            _select_output_field(left_fields, "Company", "company")
            _select_output_field(right_fields, "Name", "name")
            _select_output_field(right_fields, "Email", "email")
            form.get_by_role("button", name="Review complete join").click()

            preview = page.locator("#join-preview")
            expect(preview).to_be_visible()
            expect(preview).to_contain_text("The base grain is preserved")
            preview.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -80)")
            _capture(
                page,
                output_directory / "06d-customer-combination.png",
            )
            browser_context.close()
            browser.close()
    finally:
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
    )
    parser.add_argument("--browser-channel", default="msedge")
    arguments = parser.parse_args()
    capture(
        arguments.output_directory.resolve(),
        browser_channel=arguments.browser_channel,
    )


if __name__ == "__main__":
    main()
