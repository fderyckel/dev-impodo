"""Reuse the company chosen after Stage 2 when loading linked matching fields."""

import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.integration.web import test_linked_matching_fields as linked_fields
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm


class LinkedMatchingCompanyTests(unittest.TestCase):
    @contextmanager
    def scenario(self, major, mode, *, custom=False):
        fixture = linked_fields.LinkedMatchingFieldTests(methodName="runTest")
        fixture.setUp()
        try:
            parent, edge, related, key, label = (
                ("x.work_order", "x_asset_id", "x.asset", "x_asset_code", "Asset Code")
                if custom
                else ("mrp.bom.line", "bom_id", "mrp.bom", "code", "BoM Reference")
            )
            workspace_id, columns = fixture.configure_link(
                major,
                mode,
                parent,
                edge,
                related,
                key,
                label,
                choose_company=False,
            )
            if mode is OdooConnectionMode.LOCAL:
                fixture.local_odoo_reader.get_model_metadata.side_effect = (
                    lambda state, profile, models: replace(
                        fixture.metadata,
                        models={name: fixture.metadata.models[name] for name in models},
                    )
                )
            base = f"/workspaces/{workspace_id}"
            state = fixture.context.queries.get(workspace_id)
            self.assertIsNone(state.odoo_company)
            with patch(
                "impodo.web.routers.odoo_company.read_company_access",
                return_value=fixture.access,
            ):
                selected = fixture.client.post(
                    base + "/mapping/company",
                    data={
                        "csrf_token": fixture.csrf,
                        "revision": str(state.revision),
                        "company_id": "1",
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
            self.assertEqual(selected.status_code, 303, selected.text[:500])
            schema = fixture.context.queries.get_odoo_schema_catalog(workspace_id)
            state = fixture.context.queries.get(workspace_id)
            self.assertNotEqual(
                schema.read_context_hash, state.odoo_company.context_hash
            )
            yield fixture, workspace_id, columns, parent, edge, related, key
        finally:
            fixture.tearDown()

    def lookup(self, fixture, workspace_id, parent, edge):
        return fixture.client.post(
            f"/workspaces/{workspace_id}/mapping/linked-matching-fields",
            data={
                "csrf_token": fixture.csrf,
                "target_model": parent,
                "target_field": edge,
            },
            headers=POST_HEADERS,
        )

    def test_company_selection_then_discovery_refreshes_once_for_both_majors_and_readers(
        self,
    ):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for custom in (False, True):
                    with (
                        self.subTest(major=major, mode=mode, custom=custom),
                        self.scenario(major, mode, custom=custom) as scenario,
                    ):
                        fixture, workspace_id, columns, parent, edge, related, key = (
                            scenario
                        )
                        original_reader = fixture.context.schema_reader

                        def read(state, secret, *, fixture=fixture, reader=original_reader):
                            self.assertFalse(
                                getattr(_read_databases, "connections", None)
                            )
                            self.assertEqual(
                                state.odoo_company, fixture.access.choices[0]
                            )
                            return reader(state, secret)

                        fixture.context.schema_reader = Mock(side_effect=read)
                        local_reader = fixture.local_odoo_reader.get_model_metadata
                        local_reader.reset_mock()
                        with (
                            count_duckdb_statements() as refreshed_sql,
                            collect_duckdb_request_timings() as refreshed_timing,
                        ):
                            response = self.lookup(fixture, workspace_id, parent, edge)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertIn(
                            key, [field["name"] for field in response.json()["fields"]]
                        )
                        schema = fixture.context.queries.get_odoo_schema_catalog(
                            workspace_id
                        )
                        self.assertEqual(
                            schema.read_context_hash,
                            fixture.access.choices[0].context_hash,
                        )
                        self.assertEqual(
                            fixture.context.queries.get(workspace_id).odoo_company,
                            fixture.access.choices[0],
                        )
                        self.assertIsNone(schema.pending_refresh)
                        self.assertEqual(
                            [request.model for request in fixture.lookup_calls[-1][0]],
                            [related],
                        )
                        self.assertEqual(fixture.lookup_calls[-1][1], ())
                        self.assertEqual(refreshed_timing.lock_retry_count, 0)
                        # Two admission stores, four preparation stores, and
                        # four publication stores after the Odoo refresh.
                        # Current access skips publication and keeps six below.
                        self.assertLessEqual(refreshed_timing.connection_count, 10)
                        self.assertLessEqual(refreshed_timing.schema_check_count, 7)
                        self.assertLessEqual(len(refreshed_sql), 170)
                        refresh_count = (
                            local_reader.call_count
                            if mode is OdooConnectionMode.LOCAL
                            else fixture.context.schema_reader.call_count
                        )
                        self.assertEqual(refresh_count, 1)
                        with (
                            count_duckdb_statements() as baseline_sql,
                            collect_duckdb_request_timings() as baseline_timing,
                        ):
                            baseline = self.lookup(fixture, workspace_id, parent, edge)
                        with (
                            count_duckdb_statements() as repeated_sql,
                            collect_duckdb_request_timings() as repeated_timing,
                        ):
                            repeated = self.lookup(fixture, workspace_id, parent, edge)
                        self.assertEqual(baseline.status_code, 200, baseline.text)
                        self.assertEqual(repeated.status_code, 200, repeated.text)
                        self.assertEqual(repeated_sql, baseline_sql)
                        self.assertEqual(
                            repeated_timing.connection_count,
                            baseline_timing.connection_count,
                        )
                        self.assertLessEqual(repeated_timing.connection_count, 6)
                        self.assertEqual(repeated_timing.lock_retry_count, 0)
                        self.assertEqual(
                            local_reader.call_count
                            if mode is OdooConnectionMode.LOCAL
                            else fixture.context.schema_reader.call_count,
                            refresh_count,
                        )
                        url = f"/workspaces/{workspace_id}/mapping?step=tables&task=links&relation_query={edge}"
                        form = fixture.select_field(
                            fixture.client.get(url), columns, key
                        )
                        saved = fixture.send_mapping(form, "save_progress")
                        self.assertEqual(saved.status_code, 200, saved.text[:500])
                        reopened = MappingForm(fixture.client.get(url).text)
                        self.assertIn(
                            ["relation_key_0_0", "odoo-field:" + key], reopened.entries
                        )

    def test_changed_checking_key_refreshes_without_reselecting_company(self):
        for major in (19, 20):
            with (
                self.subTest(major=major),
                self.scenario(major, OdooConnectionMode.REMOTE) as scenario,
            ):
                fixture, workspace_id, _, parent, edge, _, _ = scenario
                first = self.lookup(fixture, workspace_id, parent, edge)
                self.assertEqual(first.status_code, 200, first.text)
                state = fixture.context.queries.get(workspace_id)
                owner = fixture.context.target_credential_workspace(
                    workspace_id, workspace_state=state, actor=fixture.actor
                )
                credential = store_target_credential(
                    fixture.secrets,
                    owner,
                    TargetCredentialRole.READ,
                    "new-fictional-key",
                    persistent=False,
                )
                response = self.lookup(fixture, workspace_id, parent, edge)
                self.assertEqual(response.status_code, 200, response.text)
                schema = fixture.context.queries.get_odoo_schema_catalog(workspace_id)
                self.assertEqual(
                    schema.read_credential_binding_hash, credential.binding_hash
                )
                self.assertEqual(
                    fixture.context.queries.get(workspace_id).odoo_company,
                    state.odoo_company,
                )

    def test_invalid_relationship_does_not_trigger_automatic_refresh(self):
        with self.scenario(20, OdooConnectionMode.REMOTE) as scenario:
            fixture, workspace_id, _, parent, _, _, _ = scenario
            fixture.context.schema_reader = Mock(
                side_effect=AssertionError("Unexpected Odoo read")
            )
            rejected = self.lookup(fixture, workspace_id, parent, "missing_field")
            self.assertEqual(rejected.status_code, 422, rejected.text)
            fixture.context.schema_reader.assert_not_called()
            self.assertFalse(fixture.lookup_calls)

    def test_changed_fields_require_review_before_discovery(self):
        for major in (19, 20):
            with (
                self.subTest(major=major),
                self.scenario(major, OdooConnectionMode.REMOTE) as scenario,
            ):
                fixture, workspace_id, _, parent, edge, _, _ = scenario
                model = fixture.metadata.models[parent]
                fixture.metadata = replace(
                    fixture.metadata,
                    models={
                        **fixture.metadata.models,
                        parent: replace(
                            model,
                            fields={
                                **model.fields,
                                edge: replace(model.fields[edge], readonly=True),
                            },
                        ),
                    },
                )
                response = self.lookup(fixture, workspace_id, parent, edge)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertTrue(response.json()["review_odoo_details"])
                self.assertFalse(fixture.lookup_calls)
                again = self.lookup(fixture, workspace_id, parent, edge)
                self.assertEqual(again.status_code, 422, again.text)
                self.assertTrue(again.json()["review_odoo_details"])
                self.assertFalse(fixture.lookup_calls)

    def test_failed_refresh_keeps_company_and_allows_retry(self):
        for major in (19, 20):
            with (
                self.subTest(major=major),
                self.scenario(major, OdooConnectionMode.REMOTE) as scenario,
            ):
                fixture, workspace_id, _, parent, edge, _, _ = scenario
                original = fixture.context.queries.get_odoo_schema_catalog(workspace_id)
                reader = fixture.context.schema_reader
                fixture.context.schema_reader = Mock(
                    side_effect=ConnectorError("Odoo is temporarily unavailable")
                )
                failed = self.lookup(fixture, workspace_id, parent, edge)
                self.assertEqual(failed.status_code, 422, failed.text)
                self.assertFalse(fixture.lookup_calls)
                self.assertEqual(
                    fixture.context.queries.get_odoo_schema_catalog(workspace_id),
                    original,
                )
                self.assertEqual(
                    fixture.context.queries.get(workspace_id).odoo_company,
                    fixture.access.choices[0],
                )
                fixture.context.schema_reader = reader
                retried = self.lookup(fixture, workspace_id, parent, edge)
                self.assertEqual(retried.status_code, 200, retried.text)
