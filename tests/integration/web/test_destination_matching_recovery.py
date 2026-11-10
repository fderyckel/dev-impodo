"""Keep matching choices through a failed read without changing saved evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.destination_matching_service import DestinationMatchKeyChoice
from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    transfer_destination_identity_hash,
)
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from impodo.web.routers.destination_matching import _matching_rows
from tests.application.workspace import test_destination_matching as domain_fixtures
from tests.integration.web import test_transfer_page_reads as fixtures
from tests.support import browser_scenarios
from tests.support.hierarchy_correction import MappingForm
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class DestinationMatchingRecoveryTests(unittest.TestCase):
    def test_pending_identifiers_are_limited_to_captured_fields(self):
        now = datetime.now(UTC)
        workspace = domain_fixtures._workspace(now)
        selection = domain_fixtures._selection(now)
        product = selection.datasets[0]
        for major in (19, 20):
            schema = replace(
                domain_fixtures._source_schema(workspace, now),
                odoo_version=f"{major}.0",
            )
            for primary, expected in (
                ("product-name", ("product-name", "product-code")),
                ("uncaptured-field", ("product-code",)),
            ):
                with self.subTest(major=major, primary=primary):
                    rows = _matching_rows(
                        workspace,
                        selection,
                        schema,
                        submitted_choices=(
                            DestinationMatchKeyChoice(
                                dataset_id=product.dataset_id,
                                source_column_key=primary,
                                additional_source_column_keys=("product-code",),
                                destination_handling="reuse_only",
                            ),
                        ),
                    )
                    self.assertEqual(rows[0]["selected_keys"], expected)
                    self.assertEqual(rows[0]["destination_handling"], "reuse_only")
                    self.assertIsNone(rows[0]["result"])

    def test_failed_check_keeps_choices_and_retry_saves_the_same_rule(self):
        original_schema = browser_scenarios._browser_schema
        for major in (19, 20):
            with self.subTest(major=major):

                def schema_for_major(workspace):
                    schema = original_schema(workspace)
                    return replace(
                        schema,
                        fingerprint=replace(
                            schema.fingerprint,
                            odoo_version=f"{major}.0",
                            module_versions={"base": f"{major}.0.1.0"},
                        ),
                    )

                with (
                    patch.object(
                        browser_scenarios, "_browser_schema", schema_for_major
                    ),
                    patch.object(
                        browser_scenarios,
                        "ODOO_SOURCE_POLICY_HASH",
                        odoo_source_policy_hash(major),
                    ),
                ):
                    fixture = fixtures.TransferPageReadTests("runTest")
                    fixture.setUp()
                    try:
                        self._exercise_recovery(fixture, major)
                    finally:
                        fixture.tearDown()

    def _exercise_recovery(self, fixture, major):
        context = fixture.app.state.context
        frozen, _schema = fixture._frozen_odoo_source_workspace()
        actor = resolve_local_actor(context)
        configured = context.workspace_states.configure_transfer_destination(
            frozen.workspace_id,
            actor=actor,
            expected_revision=frozen.revision,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://destination.example.test",
            odoo_database="destination",
        )
        credential = store_target_credential(
            fixture.secrets,
            configured,
            TargetCredentialRole.DESTINATION_TRANSFER,
            "fictional-destination-key",
            persistent=False,
        )
        verified = context.workspace_states.verify_transfer_destination(
            frozen.workspace_id,
            actor=actor,
            expected_revision=configured.revision,
            target_hash=transfer_destination_identity_hash(configured),
            credential_binding_hash=credential.binding_hash,
            read_principal_hash="sha256:" + "1" * 64,
            odoo_version=f"{major}.0",
        )
        workspace_id = verified.workspace_id
        dataset = context.queries.get_source_selection(workspace_id).datasets[0]
        column_key = dataset.columns[0].stable_key
        path = f"/workspaces/{workspace_id}/destination-matching"
        data = {
            "csrf_token": fixture.csrf,
            "revision": str(verified.revision),
            "match_key": f"{dataset.dataset_id}::{column_key}",
            "destination_handling": f"{dataset.dataset_id}::reuse_only",
            "row_inclusion_mode_0": "matching_rows",
            "row_inclusion_join_0": "all",
            "row_inclusion_condition_id_0_0": str(uuid4()),
            "row_inclusion_source_0_0": column_key,
            "row_inclusion_operator_0_0": "equals",
            "row_inclusion_value_0_0": "Alice",
            "row_inclusion_type_0_0": "string",
        }
        with (
            patch.object(
                context,
                "read_identity_probe",
                side_effect=ConnectorTransportError(
                    "Odoo JSON-2 has_access read for res.partner "
                    "was unavailable with HTTP 404 after 1 attempt"
                ),
            ),
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = fixture._post(path, data)
        self.assertEqual(response.status_code, 422, response.text)
        form = MappingForm(response.text, form_attribute="data-matching-builder")
        entries = dict(form.entries)
        for name in (
            "match_key",
            "destination_handling",
            "row_inclusion_mode_0",
            "row_inclusion_join_0",
            "row_inclusion_source_0_0",
            "row_inclusion_operator_0_0",
            "row_inclusion_value_0_0",
            "row_inclusion_type_0_0",
        ):
            self.assertEqual(entries[name], data[name], name)
        self.assertIn("Your choices are kept on this page", response.text)
        self.assertIn("HTTP 404", response.text)
        self.assertNotIn("fictional-destination-key", response.text)
        self.assertEqual(context.queries.get(workspace_id), verified)
        self.assertIsNone(context.queries.get(workspace_id).destination_match_plan)
        # The full failed command keeps its admission and preparation phases;
        # failure projection has one retained scope rather than repeated opens.
        self.assertLessEqual(timings.connection_count, 21)
        self.assertLessEqual(timings.schema_check_count, 12)
        self.assertLessEqual(len(statements), 111)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        self.assertEqual(timings.lock_retry_count, 0)

        recovered = fixture._post(path, data)
        self.assertEqual(recovered.status_code, 303, recovered.text)
        plan = context.queries.get(workspace_id).destination_match_plan
        self.assertEqual(plan.model_matches[0].destination_handling, "reuse_only")
        self.assertEqual(plan.model_matches[0].source_column_keys, (column_key,))
        self.assertEqual(
            plan.model_matches[0].row_inclusion.conditions[0].comparison_value, "Alice"
        )
        self.assertEqual(plan.model_matches[0].rule_excluded_source_row_numbers, (2,))

        # A later failed edit keeps the exact saved plan and revision, while
        # rendering only the new, unchecked choices on the failed response.
        before_failure = context.queries.get(workspace_id)
        edited = {
            **data,
            "revision": str(before_failure.revision),
            "destination_handling": f"{dataset.dataset_id}::transfer",
            "row_inclusion_value_0_0": "Bob",
        }
        with patch.object(
            context,
            "read_identity_probe",
            side_effect=ConnectorTransportError("Odoo read failed with HTTP 404"),
        ):
            failed_edit = fixture._post(path, edited)
        self.assertEqual(failed_edit.status_code, 422)
        edited_form = MappingForm(
            failed_edit.text, form_attribute="data-matching-builder"
        )
        edited_entries = dict(edited_form.entries)
        self.assertEqual(
            edited_entries["destination_handling"],
            f"{dataset.dataset_id}::transfer",
        )
        self.assertEqual(edited_entries["row_inclusion_value_0_0"], "Bob")
        self.assertNotIn('id="matching-results"', failed_edit.text)
        self.assertEqual(context.queries.get(workspace_id), before_failure)
        retried_edit = fixture._post(path, edited)
        self.assertEqual(retried_edit.status_code, 303, retried_edit.text)
        updated_plan = context.queries.get(workspace_id).destination_match_plan
        self.assertEqual(updated_plan.model_matches[0].destination_handling, "transfer")
        self.assertEqual(
            updated_plan.model_matches[0].row_inclusion.conditions[0].comparison_value,
            "Bob",
        )
        self.assertEqual(
            updated_plan.model_matches[0].rule_excluded_source_row_numbers, (1,)
        )


if __name__ == "__main__":
    unittest.main()
