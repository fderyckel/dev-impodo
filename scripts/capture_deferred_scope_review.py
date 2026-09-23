"""Capture the reviewed deferred-group page with authenticated fictional data."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from impodo.application.preflight_service import DeferredScopeReview
from impodo.domain.preflight.deferred_scope import (
    DeferredIssue,
    DeferredIssueGroup,
    DeferredIssueScope,
    DeferredOmittedRow,
    DeferredScopePreview,
)
from scripts.capture_match_data_recovery_screenshots import (
    VIEWPORT,
    _start_server,
    _stop_server,
)
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase


DEFAULT_OUTPUT = (
    REPOSITORY_ROOT / "docs/images/developer/stage4-deferred-record-groups.png"
)


def _review() -> DeferredScopeReview:
    issue_id = "sha256:" + "1" * 64
    row_id = "sha256:" + "2" * 64
    issue = DeferredIssue(
        issue_id=issue_id,
        code="REFERENCE_NOT_FOUND",
        scope=DeferredIssueScope.ROW,
        row_id=row_id,
        field="product_id",
        message="The component product P-COMP-404 is missing in Odoo.",
    )
    preview = DeferredScopePreview(
        comparison_id="fictional-comparison",
        comparison_hash="sha256:" + "3" * 64,
        execution_snapshot_hash="sha256:" + "4" * 64,
        selected_issue_ids=(issue_id,),
        groups=(
            DeferredIssueGroup(
                group_id="sha256:" + "5" * 64,
                issue_id=issue_id,
                root_row_id=row_id,
                row_ids=(row_id,),
                counts_by_dataset=(("bill_of_material_lines", 1),),
                write_count=0,
            ),
        ),
        omitted_rows=(
            DeferredOmittedRow(
                row_id=row_id,
                dataset="bill_of_material_lines",
                source_row=18,
                source_trace_id="fictional-trace",
                disposition="BLOCKED",
                direct_issue_ids=(issue_id,),
                inherited_issue_ids=(),
            ),
        ),
        counts_by_dataset=(("bill_of_material_lines", 1),),
        prepared_record_count=14,
        already_set_aside_count=2,
        original_write_count=10,
        omitted_write_count=0,
        remaining_write_count=10,
        remaining_problem_record_count=0,
        remaining_run_issue_count=0,
    )
    return DeferredScopeReview(
        comparison_id=preview.comparison_id,
        comparison_hash=preview.comparison_hash,
        issues=(issue,),
        candidates=(
            SimpleNamespace(
                issue=issue,
                selectable=True,
                dataset="bill_of_material_lines",
                source_row=18,
                source_identity=("BOM-FICTIONAL-01", "P-COMP-404"),
                target_model="mrp.bom.line",
            ),
        ),
        preview=preview,
    )


def capture(output: Path, *, browser_channel: str) -> None:
    """Serve the real route and capture its authenticated decision state."""

    from playwright.sync_api import expect, sync_playwright

    fixture = ProjectSetupBrowserTestCase(methodName="runTest")
    fixture.setUp()
    server = thread = None
    try:
        workspace_id, _dataset, _key = fixture._mapping_ready_workspace(
            scalar_field_count=1
        )
        review = _review()
        fixture.app.state.context.preflight.deferred_scope_review = (
            lambda requested_workspace_id, **_kwargs: (
                review
                if requested_workspace_id == workspace_id
                else None
            )
        )
        context = fixture.app.state.context
        workspace_state = context.queries.get(workspace_id)
        navigation_facts = context.navigation.get(workspace_state)
        staged_navigation = replace(
            navigation_facts,
            mapping_complete=True,
            staging_run_id="fictional-staging",
            resolution_status="FROZEN",
            resolution_staging_run_id="fictional-staging",
            quality_run_id="fictional-quality",
            quality_staging_run_id="fictional-staging",
            normalization_run_id="fictional-normalization",
            normalization_staging_run_id="fictional-staging",
            normalization_quality_run_id="fictional-quality",
            normalization_status="FROZEN",
            normalization_decision_count=1,
            normalization_reviewed_count=1,
            preflight_status="BLOCKED",
        )
        context.navigation.get = lambda _workspace_state: staged_navigation
        session = fixture.client.cookies.get("impodo_session")
        if not session:
            raise RuntimeError("The isolated app did not create an authenticated session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
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
                        "value": session,
                        "url": base_url,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = context.new_page()
            page.goto(
                f"{base_url}/workspaces/{workspace_id}/summary/deferred-groups",
                wait_until="networkidle",
            )
            expect(page.get_by_role("heading", name="Review complete affected groups"))
            expect(page.get_by_text("P-COMP-404", exact=False).first).to_be_visible()
            expect(
                page.get_by_role(
                    "button",
                    name="Set aside affected groups for this load",
                )
            ).to_be_visible()
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=False)
            context.close()
            browser.close()
    finally:
        try:
            if server is not None and thread is not None:
                _stop_server(server, thread)
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--browser-channel", default="msedge")
    arguments = parser.parse_args()
    capture(arguments.output, browser_channel=arguments.browser_channel)
