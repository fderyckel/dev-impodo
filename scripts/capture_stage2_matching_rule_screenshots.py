"""Capture the qualified Stage 2 matching-rule decisions.

The helper serves an isolated authenticated Impodo application, uses only
fictional captured Odoo 19 schema evidence, and writes four 1440 by 1024 PNG
files under ``docs/images/user``. Review and confirmation remain local after
the fixture installs the captured schema.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _capture,
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
    unresolved_stage2_model,
)


DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "docs" / "images" / "user"


def _show_decision(page, locator, *, offset: int = 80) -> None:
    locator.scroll_into_view_if_needed()
    page.evaluate(f"window.scrollBy(0, -{offset})")


def _connector_calls(fixture) -> tuple[tuple[object, ...], ...]:
    return (
        tuple(fixture.connection_calls),
        tuple(fixture.model_catalog_calls),
        tuple(fixture.schema_calls),
        tuple(fixture.read_identity_calls),
        tuple(fixture.readiness_calls),
    )


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
    server = None
    thread = None
    try:
        workspace_id, _dataset, _key = fixture._mapping_ready_workspace(
            scalar_field_count=1,
            connection_mode=OdooConnectionMode.REMOTE,
        )
        install_stage2_schema(
            fixture,
            workspace_id,
            representative_stage2_models(),
            hash_character="7",
        )
        calls_after_capture = _connector_calls(fixture)
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
            schema_url = f"{base_url}/workspaces/{workspace_id}/schema"
            page.goto(schema_url, wait_until="networkidle")

            summary = page.locator("#schema-governance")
            expect(summary).to_contain_text("Suggested")
            expect(summary).to_contain_text("3")
            _show_decision(page, summary)
            _capture(page, output_directory / "08b-odoo-business-keys.png")

            product = page.locator("[data-key-decision]").filter(
                has_text="How should Impodo find an existing Product?"
            )
            expect(product).to_contain_text("Suggested and ready to confirm")
            expect(product).to_contain_text("duplicate references")
            _show_decision(page, product)
            _capture(
                page,
                output_directory / "08d-odoo-matching-rule-warning.png",
            )

            product.locator("[data-key-editor]").evaluate(
                "element => { element.open = true; }"
            )
            product.locator("[data-primary-key-field]").select_option(
                "x_legacy_code"
            )
            asset = page.locator("[data-key-decision]").filter(
                has_text="How should Impodo find an existing Asset?"
            )
            asset.locator("[data-key-editor]").evaluate(
                "element => { element.open = true; }"
            )
            asset.locator("[data-primary-key-field]").select_option("")
            page.get_by_role(
                "button",
                name="Confirm suggested matching rules",
            ).click()
            page.wait_for_load_state("networkidle")
            product = page.locator("[data-key-decision]").filter(
                has_text="How should Impodo find an existing Product?"
            )
            expect(product).to_contain_text("Changed from suggestion")
            expect(product).to_contain_text("Legacy Reference")
            _show_decision(page, product)
            _capture(
                page,
                output_directory / "08e-odoo-matching-rule-override.png",
            )

            install_stage2_schema(
                fixture,
                workspace_id,
                (*representative_stage2_models(), unresolved_stage2_model()),
                hash_character="8",
            )
            page.goto(schema_url, wait_until="networkidle")
            unresolved = page.locator("[data-key-decision]").filter(
                has_text=(
                    "How should Impodo find an existing Unresolved Custom Record?"
                )
            )
            expect(unresolved).to_contain_text("Needs attention")
            expect(unresolved).to_contain_text(
                "Impodo found no single safe recommendation"
            )
            _show_decision(page, unresolved)
            _capture(
                page,
                output_directory / "08f-odoo-matching-rule-attention.png",
            )

            browser_context.close()
            browser.close()

        if _connector_calls(fixture) != calls_after_capture:
            raise RuntimeError(
                "Stage 2 review or confirmation made an unexpected Odoo call."
            )
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
