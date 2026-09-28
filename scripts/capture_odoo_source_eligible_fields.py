"""Capture the authenticated Odoo-source eligible-field decision point.

The helper creates only fictional Product data, serves the current application
on an ephemeral loopback port, authenticates through the normal launch route,
and writes one 1440 by 1024 PNG under ``docs/images/user``.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
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
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)


DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "docs"
    / "images"
    / "user"
    / "08g-odoo-source-eligible-fields.png"
)


def _product_schema(schema, *, credential_binding_hash: str):
    """Project the test schema into one fictional Product source schema."""

    original = {field.name: field for field in schema.models[0].fields}
    eligible_scalar = original["name"]
    fields = (
        replace(eligible_scalar, label="Product name"),
        replace(
            eligible_scalar,
            name="default_code",
            label="Internal reference",
            required=False,
        ),
        replace(
            eligible_scalar,
            name="list_price",
            label="Sales price",
            type="float",
            required=False,
            readonly=False,
            exportable=True,
        ),
        replace(
            original["company_id"],
            name="categ_id",
            label="Product category",
            relation="product.category",
        ),
        replace(
            original["category_ids"],
            name="tag_ids",
            label="Product tags",
            relation="product.tag",
        ),
        replace(
            original["child_ids"],
            name="bom_ids",
            label="Bills of Materials",
            relation="mrp.bom",
            relation_field="product_tmpl_id",
        ),
        original["write_date"],
    )
    model = replace(
        schema.models[0],
        name="product.template",
        label="Product",
        fields=fields,
        unique_constraints=(
            ("default_code",),
        ),
    )
    return replace(
        schema,
        models=(model,),
        content_hash="sha256:" + "8" * 64,
        read_credential_binding_hash=credential_binding_hash,
    )


def capture(output: Path, *, browser_channel: str) -> None:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ModuleNotFoundError as error:  # pragma: no cover - operator guidance
        raise RuntimeError(
            "Playwright is required. Run with `uv run --with playwright`."
        ) from error

    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = ProjectSetupBrowserTestCase(methodName="runTest")
    fixture.setUp()
    server = None
    thread = None
    browser = None
    try:
        workspace_state, schema = fixture._registered_remote_schema_workspace()
        credential = store_target_credential(
            fixture.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "fictional-read-key",
            persistent=False,
        )
        product_schema = _product_schema(
            schema,
            credential_binding_hash=credential.binding_hash,
        )
        queries = fixture.app.state.context.queries
        original_schema_reader = queries.get_odoo_schema_catalog

        def schema_reader(workspace_id):
            if workspace_id == workspace_state.workspace_id:
                return product_schema
            return original_schema_reader(workspace_id)

        queries.get_odoo_schema_catalog = schema_reader
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
            try:
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
                response = page.goto(
                    f"{base_url}/workspaces/{workspace_state.workspace_id}"
                    "/sources?model=product.template&edit=1#capture-plan",
                    wait_until="networkidle",
                )
                if response is None or response.status != 200:
                    status = "no response" if response is None else response.status
                    raise RuntimeError(
                        "The eligible-field page did not load successfully: "
                        f"{status}"
                    )
                decision = page.get_by_role(
                    "group",
                    name="Eligible fields from Product",
                )
                expect(decision).to_be_visible()
                expect(
                    page.locator(
                        'input[name="field_names"][value="default_code"]'
                    )
                ).to_be_visible()
                expect(
                    page.locator('input[name="field_names"][value="list_price"]')
                ).to_be_visible()
                decision.scroll_into_view_if_needed()
                page.evaluate("window.scrollBy(0, -120)")
                _capture(page, output)
                context.close()
            finally:
                browser.close()
                browser = None
    finally:
        if browser is not None:
            browser.close()
        if server is not None and thread is not None:
            _stop_server(server, thread)
        fixture.tearDown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--browser-channel", default="msedge")
    arguments = parser.parse_args()
    capture(arguments.output.resolve(), browser_channel=arguments.browser_channel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
