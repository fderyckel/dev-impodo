"""Capture grouped exact-join evidence from an isolated customer workspace."""

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
        workspace_id, selection, by_name = fixture._frozen_customer_workspace(
            several_tables=True
        )
        primary = by_name["customer_accounts"]
        additions = (
            (by_name["party_details"], "RecId"),
            (by_name["customer_addresses"], "Party"),
            (by_name["customer_terms"], "Party"),
        )
        dataset_indexes = {
            dataset.dataset_id: index
            for index, dataset in enumerate(selection.datasets)
        }
        session_cookie = fixture.client.cookies.get("impodo_session")
        if not session_cookie:
            raise RuntimeError("The isolated setup did not create an Impodo session.")
        fixture.client.close()

        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = (
                playwright.chromium.launch(headless=True)
                if browser_channel == "bundled"
                else playwright.chromium.launch(
                    channel=browser_channel,
                    headless=True,
                )
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
                "button", name="Several tables describe the same records"
            ).click()
            form = page.locator("#combine-source-tables")
            expect(form).to_have_attribute("open", "")
            form.locator('input[name="output_dataset_name"]').fill(
                "customers_complete"
            )
            form.locator('select[name="primary_dataset_id"]').select_option(
                primary.dataset_id
            )
            form.locator('select[name="primary_key_field_1"]').select_option(
                f'{primary.dataset_id}|{_column(primary, "Partition").stable_key}'
            )
            form.locator('select[name="primary_key_field_2"]').select_option(
                f'{primary.dataset_id}|{_column(primary, "Party").stable_key}'
            )
            form.locator('select[name="primary_key_type_2"]').select_option("integer")
            for dataset, identity_field in additions:
                dataset_index = dataset_indexes[dataset.dataset_id]
                form.locator(
                    f'[data-group-addition][data-dataset-id="{dataset.dataset_id}"]'
                ).check()
                form.locator(
                    f'select[name="group_addition_key_{dataset_index}_1"]'
                ).select_option(
                    f'{dataset.dataset_id}|{_column(dataset, "Partition").stable_key}'
                )
                form.locator(
                    f'select[name="group_addition_key_{dataset_index}_2"]'
                ).select_option(
                    f"{dataset.dataset_id}|"
                    f"{_column(dataset, identity_field).stable_key}"
                )
            form.get_by_role(
                "button", name="Review complete combination"
            ).click()

            preview = page.locator("#join-group-preview")
            expect(preview).to_be_visible()
            expect(preview).to_contain_text("The primary grain is preserved")
            expect(preview).to_contain_text("3")
            preview.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -80)")
            _capture(
                page,
                output_directory / "06d-customer-combination.png",
            )

            preview.get_by_role("button", name="Save combined table").click()
            page.wait_for_load_state("networkidle")
            saved = page.locator(".exact-join-rule-card").first
            expect(saved).to_be_visible()
            expect(saved).to_contain_text("Combined table prepared")
            expect(saved).to_contain_text("Mapped logical dataset")
            saved.evaluate(
                "(element) => element.scrollIntoView({block: 'start'})"
            )
            page.evaluate("window.scrollBy(0, -80)")
            _capture(
                page,
                output_directory / "06e-customer-combination-saved.png",
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
    parser.add_argument(
        "--browser-channel",
        default="msedge",
        help="Playwright channel name, or 'bundled' for its installed Chromium",
    )
    arguments = parser.parse_args()
    capture(
        arguments.output_directory.resolve(),
        browser_channel=arguments.browser_channel,
    )


if __name__ == "__main__":
    main()
