"""Qualify fresh Recipe row-rule columns in an isolated authenticated browser.

The stored Recipe targets fictional Odoo 20 metadata; no remote API is used.
IMPODO_RECIPE_ROW_SCREENSHOT_DIRECTORY retains 1440 by 1024 acceptance images.
"""

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    target_identity_hash,
)
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from scripts.capture_match_data_recovery_screenshots import _start_server, _stop_server
from tests.integration.web import test_fresh_data_controls as fixtures
from tests.support.identity import resolve_local_actor


def _row_rule_recipe(definition):
    """Require a column used only by the portable row-inclusion rule."""

    definition["odoo_target_contract"]["odoo_major_version"] = 20
    definition["odoo_target_contract"]["reference_policy_hash"] = reference_policy_hash(20)
    definition["control_definitions"] = {"controls": []}
    definition["source_shape"]["datasets"][0]["columns"].append(
        {
            "logical_column_id": "column:balances.row_status",
            "source_name": "Row status",
            "candidate_type": "STRING",
            "required_by": ["row_inclusion"],
        }
    )
    definition["mapping"]["datasets"][0]["row_inclusion"] = {
        "mode": "matching_rows",
        "join": "all",
        "conditions": [
            {
                "source_column_id": "column:balances.row_status",
                "operator": "equals",
                "comparison_value": "KEEP",
                "value_type": "string",
            }
        ],
    }


def _activate(fixture):
    """Use the real compiler after the browser accepts corrected source data."""

    context, setup = fixture.context, fixture.setup
    actor = resolve_local_actor(context)
    state = context.queries.get(setup.setup_workspace.workspace_id)
    state = context.workspace_states.update_target(
        state.workspace_id,
        actor=actor,
        expected_revision=state.revision,
        odoo_connection_mode="REMOTE",
        odoo_base_url="https://fictional.example.test",
        odoo_database="fictional_test",
        intended_applications=(),
        intended_models=("res.partner",),
    )
    credential = store_target_credential(
        context.secret_store,
        state,
        TargetCredentialRole.READ,
        "fictional-test-read",
        persistent=False,
    )
    target_hash = target_identity_hash(
        connection_mode="REMOTE",
        base_url=state.odoo_base_url,
        database=state.odoo_database,
    )
    observed = datetime.now(UTC).isoformat()
    metadata = MetadataSnapshot(
        TargetFingerprint(
            target_hash=target_hash,
            connection_mode="REMOTE",
            database="fictional_test",
            odoo_version="20.0+e",
            snapshot_timestamp=observed,
            module_versions={"base": "20.0.1.0"},
        ),
        {
            "res.partner": ModelMetadata(
                "res.partner",
                "Customers",
                {
                    "name": FieldMetadata(name="name", type="char", label="Name"),
                    "credit_limit": FieldMetadata(
                        name="credit_limit",
                        type="float",
                        label="Opening balance",
                    ),
                },
            )
        },
    )
    schema = context.schema_workspace.capture(
        state.workspace_id,
        metadata,
        read_credential_binding_hash=credential.binding_hash,
        read_identity=OdooReadIdentity(
            target_hash=target_hash,
            principal_hash=content_hash("fictional principal"),
            permission_hash=content_hash("fictional permissions"),
            context_hash=content_hash("fictional context"),
            readable_models=("res.partner",),
            observed_at=observed,
        ),
        actor=actor,
    )
    result = context.test_runs.activate(
        setup.binding.project_id,
        setup.run.migration_run_id,
        expected_workspace_revision=context.migration_projects.get(
            setup.binding.project_id,
            actor=actor,
        ).optimistic_revision,
        target_schema=schema,
        target_reference_bundle=None,
        credential_generation=schema.read_credential_binding_hash,
        operation_id=str(uuid4()),
        actor=actor,
    )
    return result.applications[0]


@unittest.skipUnless(
    sync_playwright, "Playwright is required for browser qualification"
)
class RecipeRowColumnJourneyTests(unittest.TestCase):
    def test_missing_rule_column_blocks_acceptance_until_corrected(self):
        self._check_delivery(
            "missing",
            b"Name,Amount\nAcme,100.00\nBeta,25.50\n",
        )

    def test_ambiguous_rule_columns_block_acceptance_until_corrected(self):
        self._check_delivery(
            "ambiguous",
            b"Name,Amount,Row status,Row-status\nAcme,100.00,KEEP,DROP\nBeta,25.50,DROP,KEEP\n",
        )

    def _check_delivery(self, case, source_csv):
        fixture = fixtures.FreshDataControlBrowserTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        url = fixture.registered_delivery(
            configure_recipe=_row_rule_recipe,
            source_csv=source_csv,
        )
        actor = resolve_local_actor(fixture.context)
        recipe_id = fixture.publication.recipe.recipe_id
        original = fixture.context.recipes.read_revision(recipe_id, 1, actor=actor)
        server, thread, port = _start_server(fixture.fixture.app)
        self.addCleanup(_stop_server, server, thread)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        browser = playwright.chromium.launch(
            headless=True,
            channel=os.environ.get("IMPODO_BROWSER_CHANNEL"),
        )
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1440, "height": 1024})
        base = f"http://127.0.0.1:{port}"
        context.add_cookies(
            [
                {
                    "name": "impodo_session",
                    "value": fixture.client.cookies.get("impodo_session"),
                    "url": base,
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def capture(state):
            output = os.environ.get("IMPODO_RECIPE_ROW_SCREENSHOT_DIRECTORY")
            if output:
                directory = Path(output)
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / f"{case}-{state}.png"))

        page.goto(base + url, wait_until="networkidle")
        card = page.locator(".recipe-match-card")
        expect(card).to_contain_text("Needs attention")
        expect(card).to_contain_text(
            "No safe detected table contains every required column."
        )
        expect(page.get_by_role("button", name="Use this fresh data")).to_have_count(0)
        card.locator("summary").click()
        expect(card).to_contain_text("Row status")
        card.scroll_into_view_if_needed()
        capture("blocked")
        # Exercise the server guard even though the unsafe action is absent in the UI.
        csrf = page.locator('form[action$="/accept"] [name="csrf_token"]').input_value()
        response = context.request.post(
            base + url + "/accept",
            form={"csrf_token": csrf},
            headers={"Origin": base},
        )
        self.assertEqual(response.status, 422)
        self.assertIn(
            "Resolve every missing, ambiguous, or unused source input", response.text()
        )
        self.assertIsNone(
            fixture.context.queries.get_source_selection(
                fixture.setup.setup_workspace.workspace_id,
            )
        )

        page.get_by_role("button", name="Remove file", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Remove file", exact=True).click()
        expect(page.get_by_text("No fresh files have been added yet.")).to_be_visible()
        page.locator('[name="source_file"]').set_input_files(
            {
                "name": "balances.csv",
                "mimeType": "text/csv",
                "buffer": b"Name,Amount,Row status\nExcluded,10.00,DROP\nAcme,100.00,KEEP\nBeta,25.50,KEEP\n",
            }
        )
        page.get_by_role("button", name="Add fresh files").click()
        page.get_by_role("button", name="Check files and match tables").click()
        expect(card).to_contain_text("Matched")
        expect(card.locator("dd").nth(1)).to_have_text("3")
        card.scroll_into_view_if_needed()
        capture("corrected")
        page.get_by_role("button", name="Use this fresh data").click()
        expect(page.get_by_role("link", name="2. Check Odoo")).to_be_visible()

        application = _activate(fixture)
        self.assertIs(application.status, RecipeApplicationStatus.BLOCKED)
        issues = fixture.context.run_planning.repository.list_issues(
            application.application_id
        )
        self.assertIn(
            "MAPPING_ROW_INCLUSION_CONFIRMATION_REQUIRED",
            {item.code for item in issues},
        )
        page.goto(
            base
            + f"/projects/{application.project_id}/runs/{application.migration_run_id}",
            wait_until="networkidle",
        )
        counts = page.locator("[data-row-inclusion-run-review]")
        expect(counts).to_contain_text("3 source, 2 included, 1 excluded")
        action = page.locator("[data-run-next-action]").get_by_role(
            "link", name="Review rows to use"
        )
        action.focus()
        action.press("Enter")
        review = page.locator("#rows-to-use-review")
        expect(review).to_contain_text("Needs confirmation")
        expect(
            review.get_by_role("button", name="Confirm 2 rows to use")
        ).to_be_visible()
        review.scroll_into_view_if_needed()
        capture("fresh-confirmation")
        confirm = review.get_by_role("button", name="Confirm 2 rows to use")
        confirm.focus()
        confirm.press("Enter")
        expect(review).to_contain_text("2 rows confirmed")
        snapshot, confirmation = fixture.context.row_inclusion_reviews.current(
            application.workspace_id,
            actor=actor,
        )
        self.assertEqual(
            (
                snapshot.source_row_count,
                snapshot.included_count,
                snapshot.excluded_count,
            ),
            (3, 2, 1),
        )
        self.assertIsNotNone(confirmation)
        self.assertEqual(
            fixture.context.recipes.read_revision(recipe_id, 1, actor=actor), original
        )
        self.assertEqual(errors, [])
