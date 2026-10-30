"""Exercise intentional blank choices in an authenticated fictional workspace."""

import os
import sys
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.odoo.contracts import RecordSnapshot, TargetRecord
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_mapping_workflow as fixtures
from tests.support.browser_scenarios import _browser_schema
from tests.support.identity import resolve_local_actor


@unittest.skipUnless(sync_playwright, "Playwright is required for browser checks")
class Many2oneBlankMatchBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MappingWorkflowBrowserTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.workspace_id, dataset, _key = self.fixture._mapping_ready_workspace(
            scalar_field_count=0,
            relationship_field_count=1,
            relationship_field_labels=("Classification",),
        )
        context = self.fixture.app.state.context
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            source_identity_column_keys=(dataset.columns[0].stable_key,),
            target_identity=(
                IdentityComponentMapping((dataset.columns[0].stable_key,), ("ref",)),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="relation_0000",
                    kind="many2one",
                    source_column_keys=(dataset.columns[1].stable_key,),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="res.partner",
                        key_mappings=(
                            ReferenceKeyMapping(dataset.columns[1].stable_key, "ref"),
                        ),
                    ),
                ),
            ),
        )
        context.mapping_workspace.save_working_draft(
            self.workspace_id,
            datasets=(mapping,),
            expected_version=None,
            actor=resolve_local_actor(context),
        )

        def read_choices(workspace, _metadata, _records):
            metadata = _browser_schema(workspace)
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "res.partner": (
                        TargetRecord("res.partner", 91, {"ref": "Standard"}),
                    )
                },
                requested_fields={"res.partner": ("ref",)},
            )

        context.readiness_reader = read_choices
        self.addCleanup(patch.stopall)
        patch(
            "impodo.web.routers.mapping._source_value_choices",
            return_value=(
                {"value": "TO_DEFINE", "count": 87},
                {"value": "Standard", "count": 10},
            ),
        ).start()
        session = self.fixture.client.cookies.get("impodo_session")
        self.fixture.client.close()
        server, thread, port = _start_server(self.fixture.app)
        self.addCleanup(_stop_server, server, thread)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        browser = self.playwright.chromium.launch(
            channel="msedge" if sys.platform == "win32" else None, headless=True
        )
        self.addCleanup(browser.close)
        browser_context = browser.new_context(
            viewport={"width": 1440, "height": 1024}, locale="en-GB"
        )
        base = f"http://127.0.0.1:{port}"
        browser_context.add_cookies(
            [
                {
                    "name": "impodo_session",
                    "value": session,
                    "url": base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        self.page = browser_context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(
            f"{base}/workspaces/{self.workspace_id}/mapping", wait_until="networkidle"
        )
        self.row = self.page.locator(
            '[data-relation-mapping-row][data-target-field="relation_0000"]'
        )
        self.row.evaluate("""row => { for (let item = row; item; item = item.parentElement) {
            if (item instanceof HTMLDetailsElement) item.open = true; } }""")

    def test_leave_blank_is_distinct_from_unresolved_and_survives_reopen_and_refresh(
        self,
    ):
        self.row.get_by_role("button", name="Match values", exact=True).click()
        dialog = self.page.locator("[data-value-match-dialog]")
        choice = dialog.get_by_label("Odoo choice for TO_DEFINE", exact=True)
        expect(choice).to_have_value("")
        choice.select_option(label="Leave blank (no linked record)")
        dialog.get_by_role("button", name="Use complete matches", exact=True).click()
        self.row.get_by_label("If the source value is blank", exact=True).select_option("leave_blank")
        self.assertIn(
            '"action":"leave_blank"',
            self.row.locator("[data-value-match-storage]").input_value(),
        )
        self.row.get_by_role("button", name="Match values", exact=True).click()
        expect(choice.locator("option:checked")).to_have_text(
            "Leave blank (no linked record)"
        )
        dialog.get_by_role("button", name="Refresh Odoo values", exact=True).click()
        expect(choice.locator("option:checked")).to_have_text(
            "Leave blank (no linked record)"
        )
        choice.focus()
        choice.select_option(label="Standard")
        choice.select_option(label="Leave blank (no linked record)")
        self.assertEqual(self.errors, [])
        dialog.get_by_role("button", name="Use complete matches", exact=True).click()
        with self.page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/mapping/save")
            )
        ) as saved:
            self.page.get_by_role("button", name="Save progress", exact=True).click()
        self.assertEqual(saved.value.status, 200)
        current = self.fixture.app.state.context.mapping_workspace.mappings.get_mapping_working_draft(
            self.workspace_id
        )
        self.assertEqual(
            current.definition.datasets[0].relationships[0].blank_source_values,
            ("TO_DEFINE",),
        )
        self.assertEqual(current.definition.datasets[0].relationships[0].blank_source_action, "leave_blank")
        self.page.reload(wait_until="networkidle")
        self.row.evaluate("""row => { for (let item = row; item; item = item.parentElement) {
            if (item instanceof HTMLDetailsElement) item.open = true; } }""")
        expect(self.row.get_by_label("If the source value is blank", exact=True)).to_have_value("leave_blank")
        capture = os.environ.get("IMPODO_BLANK_SOURCE_SCREENSHOT")
        if capture:
            self.row.get_by_label("If the source value is blank", exact=True).scroll_into_view_if_needed()
            self.page.screenshot(path=capture)
        self.row.get_by_role("button", name="Match values", exact=True).click()
        expect(choice.locator("option:checked")).to_have_text(
            "Leave blank (no linked record)"
        )

    def test_required_relationship_does_not_offer_leave_blank(self):
        self.row.get_by_text("Support options", exact=True).click()
        self.row.get_by_label("Must have a value", exact=True).check()
        self.row.get_by_role("button", name="Match values", exact=True).click()
        choice = self.page.get_by_label("Odoo choice for TO_DEFINE", exact=True)
        expect(choice).to_be_visible()
        expect(choice.locator("option[data-leave-blank]")).to_have_count(0)
