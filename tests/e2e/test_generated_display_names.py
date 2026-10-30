"""Explicitly restore source spelling without changing the internal row key."""

import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_join_lookup_preparation as fixtures
from tests.integration.web import test_load_workflow as load_fixtures
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class GeneratedDisplayNameBrowserTests(unittest.TestCase):
    def test_rejection_outcome_separates_written_failed_and_unattempted_rows(self):
        fixture = load_fixtures.LoadWorkflowBrowserTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        context = fixture.app.state.context
        workspace = fixture.workspaces.create(
            name="Fictional load recovery", source_system="Other"
        )
        run = SimpleNamespace(
            run_id="11111111-1111-4111-8111-111111111111",
            rows=(),
            committed_count=1,
            failed_count=1,
            blocked_count=1,
            partially_applied_count=0,
            unknown_count=0,
        )
        preview = SimpleNamespace(
            snapshot=SimpleNamespace(
                counts={"CREATE": 3, "UPDATE": 0, "UNCHANGED": 0},
                target_database="fictional_training",
                target_odoo_version="19.0",
                semantic_hash="sha256:" + "a" * 64,
                target_hash="sha256:" + "b" * 64,
            ),
            datasets=(),
            current_run=run,
            can_load=False,
        )
        diagnostic = OdooWriteDiagnostic(
            WriteFailureCode.REQUIRED_VALUE, "x.training", "load", fields=("name",)
        )
        rows = tuple(
            SimpleNamespace(
                dataset="training",
                source_row=i + 2,
                target_model="x.training",
                operation="CREATE",
                execution_status=execution_status,
                status=SimpleNamespace(value=status),
                odoo_id=odoo_id,
                differing_fields=(),
                message=message,
                retry_safe=False,
            )
            for i, (status, execution_status, odoo_id, message) in enumerate(
                (
                    ("VERIFIED", "COMMITTED", 41, "Written and verified"),
                    ("NOT_WRITTEN", "FAILED", None, diagnostic.summary),
                    (
                        "NOT_WRITTEN",
                        "BLOCKED",
                        None,
                        "Not attempted after an Odoo rejection",
                    ),
                )
            )
        )
        report = SimpleNamespace(
            status=SimpleNamespace(value="FALLOUT"),
            rows=rows,
            verified_count=1,
            fallout_count=2,
            unknown_count=0,
            retry_safe_count=0,
        )
        for service, method, value in (
            (context.execution, "current_preview", preview),
            (context.reconciliation, "current", report),
            (context.reconciliation, "current_detail_available", False),
        ):
            mock = patch.object(type(service), method, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        session = fixture.client.cookies.get("impodo_session")
        fixture.client.close()
        server, thread, port = _start_server(fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None, headless=True
        )
        self.addCleanup(browser.close)
        browser_context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        browser_context.add_cookies(
            [
                dict(
                    name="impodo_session",
                    value=session,
                    url=base,
                    httpOnly=True,
                    sameSite="Strict",
                )
            ]
        )
        page = browser_context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(
            f"{base}/workspaces/{workspace.workspace_id}/load/outcome",
            wait_until="networkidle",
        )
        expect(
            page.get_by_role(
                "heading",
                name="1 written in Odoo · 1 rejected · 1 not attempted",
                exact=False,
            )
        ).to_be_visible()
        expect(page.get_by_text(diagnostic.summary, exact=True)).to_be_visible()
        capture = os.environ.get("IMPODO_REJECTION_SCREENSHOT")
        if capture:
            page.get_by_role(
                "heading",
                name="1 written in Odoo · 1 rejected · 1 not attempted",
                exact=False,
            ).scroll_into_view_if_needed()
            page.screenshot(path=capture)
        self.assertEqual(errors, [])

    def test_restore_original_spelling_saves_identity_without_rebuilding_the_list(self):
        fixture = fixtures.JoinLookupPreparationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        workspace_id, _ = fixture._setup((12, 20, 15))
        context = fixture.context
        plan = context.derived_entities.get_derived_entity_plan(workspace_id)
        revision = context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        draft = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        mapping = next(
            m for m in revision.definition.datasets if m.target_model == "x_secondary"
        )
        original_column = mapping.target_identity[0].source_column_keys[0]
        internal_column = mapping.source_identity_column_keys[0]
        old = replace(
            mapping,
            target_identity=(
                replace(
                    mapping.target_identity[0], source_column_keys=(internal_column,)
                ),
            ),
        )
        context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=tuple(
                old if m.dataset_id == mapping.dataset_id else m
                for m in revision.definition.datasets
            ),
            expected_version=draft.version,
            actor=fixture.actor,
        )
        selection = context.queries.get_mapping_source_selection(workspace_id)
        index = next(
            i
            for i, dataset in enumerate(selection.datasets)
            if dataset.dataset_id == mapping.dataset_id
        )
        session = fixture.fixture.client.cookies.get("impodo_session")
        fixture.fixture.client.close()
        server, thread, port = _start_server(fixture.fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None, headless=True
        )
        self.addCleanup(browser.close)
        browser_context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        browser_context.add_cookies(
            [
                dict(
                    name="impodo_session",
                    value=session,
                    url=base,
                    httpOnly=True,
                    sameSite="Strict",
                )
            ]
        )
        page = browser_context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(
            f"{base}/workspaces/{workspace_id}/mapping?mapping_dataset={index}",
            wait_until="networkidle",
        )
        button = page.get_by_role("button", name="Use original spelling", exact=True)
        expect(button).to_be_visible()
        capture = os.environ.get("IMPODO_GENERATED_NAME_SCREENSHOT")
        if capture:
            button.scroll_into_view_if_needed()
            page.screenshot(path=capture)
        button.click()
        expect(page.locator(f'select[name="identity_source_{index}_0"]')).to_have_value(
            original_column
        )
        self.assertEqual(errors, [])
        invalid = page.locator("form[data-mapping-form] :invalid").evaluate_all(
            "items => items.map(item => [item.name, item.validationMessage])"
        )
        self.assertEqual(invalid, [])
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.split("?", 1)[0].endswith("/mapping/save")
            )
        ) as saved:
            page.get_by_role("button", name="Save progress", exact=True).click()
        self.assertEqual(saved.value.status, 200)
        current = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        restored = next(
            m for m in current.definition.datasets if m.dataset_id == mapping.dataset_id
        )
        self.assertEqual(
            restored.target_identity[0].source_column_keys, (original_column,)
        )
        self.assertEqual(restored.source_identity_column_keys, (internal_column,))
        self.assertEqual(
            context.derived_entities.get_derived_entity_plan(workspace_id).content_hash,
            plan.content_hash,
        )
        page.reload(wait_until="networkidle")
        expect(
            page.get_by_role("button", name="Use original spelling", exact=True)
        ).to_have_count(0)
        self.assertEqual(errors, [])
