"""Capture Odoo business-key and matching-rule decisions.

The helper serves an isolated authenticated Impodo application, uses only
fictional captured Odoo 19 schema evidence, and writes four 1440 by 1024 PNG
files under ``docs/images/user``. Review and confirmation remain local after
the fixture installs the captured schema.
"""

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
    locator.evaluate(
        """element => {
          for (let current = element; current; current = current.parentElement) {
            if (current.tagName === 'DETAILS') current.open = true;
          }
        }"""
    )
    locator.scroll_into_view_if_needed()
    page.evaluate(f"window.scrollBy(0, -{offset})")


def _rule_card(page, label: str):
    question = f"How should Impodo find an existing {label}?"
    return page.locator("[data-key-decision]").filter(
        has=page.locator(f'summary[aria-label="{question}"]'),
    )


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
            page.goto(f"{schema_url}?step=records", wait_until="networkidle")
            journey = page.locator("[data-schema-journey]")
            expect(journey).to_have_attribute("data-step", "records")
            expect(page.get_by_role("button", name="Save record types", exact=True)).to_be_visible()
            records = page.locator('.sidebar-subnav-link').filter(has_text="Choose records")
            related = page.locator('.sidebar-subnav-link').filter(has_text="Related values")
            matching = page.locator('.sidebar-subnav-link').filter(has_text="Matching rules")
            matching.click()
            expect(journey).to_have_attribute("data-step", "matching")
            expect(page.locator("[data-schema-step-panel]:visible")).to_have_count(1)
            expect(page.locator('form[action$="/schema/govern"] [data-primary-key-field]')).to_have_count(3)

            product = _rule_card(page, "Product")
            _show_decision(page, product)
            product.locator("[data-key-editor]").evaluate("element => { element.open = true; }")
            product.locator("[data-primary-key-field]").select_option("x_legacy_code")
            related.click()
            expect(journey).to_have_attribute("data-step", "related")
            matching.click()
            expect(product.locator("[data-primary-key-field]")).to_have_value("x_legacy_code")
            page.go_back()
            expect(journey).to_have_attribute("data-step", "related")
            page.go_forward()
            expect(journey).to_have_attribute("data-step", "matching")
            expect(product.locator("[data-primary-key-field]")).to_have_value("x_legacy_code")
            records.click()
            expect(journey).to_have_attribute("data-step", "records")
            matching.click()
            page.reload(wait_until="networkidle")
            expect(journey).to_have_attribute("data-step", "matching")

            summary = page.locator("#schema-governance")
            expect(summary).to_contain_text("Suggested")
            expect(summary).to_contain_text("3")
            _show_decision(page, summary)
            _capture(page, output_directory / "08b-odoo-business-keys.png")

            product = _rule_card(page, "Product")
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
            asset = _rule_card(page, "Asset")
            _show_decision(page, asset)
            asset.locator("[data-key-editor]").evaluate(
                "element => { element.open = true; }"
            )
            asset.locator("[data-primary-key-field]").select_option("")
            page.get_by_role(
                "button",
                name="Confirm suggested matching rules",
            ).click()
            page.wait_for_load_state("networkidle")
            product = _rule_card(page, "Product")
            expect(product).to_contain_text("Changed from suggestion")
            expect(product).to_contain_text("Legacy Reference")
            _show_decision(page, product)
            _capture(
                page,
                output_directory / "08e-odoo-matching-rule-override.png",
            )

            asset = _rule_card(page, "Asset")
            _show_decision(page, asset)
            asset.locator("[data-key-editor]").evaluate("element => { element.open = true; }")
            asset.locator("[data-primary-key-field]").select_option("code")
            page.get_by_role("button", name="Confirm matching rules", exact=True).click()
            page.wait_for_load_state("networkidle")
            governance = fixture.app.state.context.queries.get_schema_governance(workspace_id)
            if governance is None or len(governance.business_keys) != 3:
                raise RuntimeError("Confirmation did not retain the complete matching-rule set")
            product_rule = next(item for item in governance.business_keys if item.model == "product.template")
            if product_rule.key_fields != ("x_legacy_code",):
                raise RuntimeError("Confirmation lost the product rule edited before tab navigation")

            install_stage2_schema(
                fixture,
                workspace_id,
                (*representative_stage2_models(), unresolved_stage2_model()),
                hash_character="8",
            )
            page.goto(f"{schema_url}?step=matching", wait_until="networkidle")
            unresolved = _rule_card(page, "Unresolved Custom Record")
            expect(unresolved).to_contain_text("Needs attention")
            expect(unresolved).to_contain_text(
                "Impodo found no single safe recommendation"
            )
            _show_decision(page, unresolved)
            _capture(
                page,
                output_directory / "08f-odoo-matching-rule-attention.png",
            )

            page.set_viewport_size({"width": 390, "height": 844})
            _show_decision(page, unresolved)
            if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                raise RuntimeError("Matching-rule review overflows a narrow screen")
            expect(unresolved).to_be_visible()

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


def capture_supporting_review(output_directory: Path, *, browser_channel: str) -> None:
    """Qualify keyboard, persistence and narrow layout with isolated source data."""
    from playwright.sync_api import expect, sync_playwright

    from tests.integration.web.test_supporting_review import (
        SupportingReviewBrowserTests,
    )

    output_directory.mkdir(parents=True, exist_ok=True)
    fixture = SupportingReviewBrowserTests(methodName="runTest")
    fixture.setUp()
    server = thread = None
    try:
        calls = _connector_calls(fixture)
        cookie = fixture.client.cookies.get("impodo_session")
        if not cookie:
            raise RuntimeError("The isolated app did not establish its session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        base_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=browser_channel, headless=True)
            browser_context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1, locale="en-GB")
            browser_context.add_cookies([{"name": "impodo_session", "value": cookie, "url": base_url, "httpOnly": True, "sameSite": "Strict"}])
            page = browser_context.new_page()
            page.goto(
                f"{base_url}/workspaces/{fixture.workspace_id}/schema#supporting-data-review",
                wait_until="networkidle",
            )
            review = page.locator("#supporting-data-review")
            expect(review).to_be_focused()
            choices = review.locator('select[name^="action_"]')
            expect(choices).to_have_count(2)
            for choice in choices.all():
                choice.select_option("REUSE")
            choices.first.focus()
            expect(choices.first).to_be_focused()
            page.keyboard.press("Tab")
            expect(choices.first).not_to_be_focused()
            save = review.get_by_role("button", name="Save choices", exact=True)
            save.focus()
            position_before_save = page.evaluate("window.scrollY")
            save.press("Enter")
            expect(review).to_contain_text("Choices are saved")
            expect(save).to_be_focused()
            position_after_save = page.evaluate("window.scrollY")
            if abs(position_after_save - position_before_save) > 2:
                raise RuntimeError(
                    "Saving supporting choices moved the operator away from the save position"
                )
            expect(review.get_by_role("button", name="Check existing Odoo values")).to_be_visible()
            for choice in choices.all():
                expect(choice).to_have_value("REUSE")
            _show_decision(page, review)
            target = output_directory / "08i-file-supporting-choices.png"
            page.screenshot(path=str(target), full_page=False)
            print(f"Captured {target}")
            page.reload(wait_until="networkidle")
            expect(review).to_contain_text("Choices are saved")
            page.set_viewport_size({"width": 390, "height": 844})
            _show_decision(page, review)
            if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                raise RuntimeError("Supporting review overflows a narrow screen")
            expect(choices.first).to_be_visible()
            browser_context.close()
            browser.close()
        if _connector_calls(fixture) != calls:
            raise RuntimeError("Opening or saving supporting choices made an Odoo call")
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
        "--supporting-review-only",
        action="store_true",
        help="Capture and exercise the current file-source supporting decision instead.",
    )
    parser.add_argument(
        "--browser-channel",
        default="msedge",
        help="Installed Chromium channel for Playwright. Defaults to msedge.",
    )
    arguments = parser.parse_args()
    selected_capture = capture_supporting_review if arguments.supporting_review_only else capture
    selected_capture(
        arguments.output_directory.resolve(),
        browser_channel=arguments.browser_channel,
    )


if __name__ == "__main__":
    main()
