"""Checklist navigation reuses bulk evidence and the existing default command."""

from dataclasses import replace
from html import unescape
import re
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
)
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase, POST_HEADERS
from tests.support.identity import resolve_local_actor
from tests.support.required_values import required_values_workspace


class RequiredValuesBrowserTests(ProjectSetupBrowserTestCase):
    def test_providers_and_correction_anchors_reuse_current_evidence_without_values(
        self,
    ):
        workspace, dataset, _revision, _validation = required_values_workspace(self)
        context = self.app.state.context
        service = context.mapping_workspace
        with (
            patch.object(
                service.validator,
                "validate",
                side_effect=AssertionError("No read-time validation"),
            ),
            patch.object(
                service.categorical_coverage,
                "collect",
                side_effect=AssertionError("No source scan"),
            ),
            patch(
                "impodo.web.routers.mapping._refresh_mapping_odoo_defaults",
                side_effect=AssertionError("No default capture"),
            ),
            collect_duckdb_request_timings() as timings,
        ):
            response = self.client.get(
                f"/workspaces/{workspace}/mapping/required-values"
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Source value or rule", response.text)
        self.assertIn("Fixed value", response.text)
        self.assertIn("Checked Odoo default", response.text)
        self.assertIn("Odoo-managed", response.text)
        self.assertIn("Missing decision", response.text)
        self.assertIn("Chosen · not checked", response.text)
        self.assertIn("Review checked defaults", response.text)
        self.assertNotIn("Private fixed value", response.text)
        self.assertNotIn("Odoo default 0002", response.text)
        self.assertIn("#scalar-0-field_0001", response.text)
        self.assertIn("#relationship-0-relation_0000", response.text)
        self.assertIn("#target-identity-0", response.text)
        self.assertIn("field_query=Field+0001", response.text)
        # Access discovery and rendering open two registry and two workspace
        # reference owners; the operation retains one Data version and one engine.
        self.assertGreater(timings.connection_count, 0)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 1)
        self.assertEqual(timings.lock_retry_count, 0)
        main = self.client.get(f"/workspaces/{workspace}/mapping")
        self.assertIn("Review required values", main.text)
        filtered = self.client.get(
            f"/workspaces/{workspace}/mapping/required-values?dataset={dataset.dataset_id}&state=missing"
        )
        self.assertEqual(filtered.text.count("data-required-field="), 2)

    def test_grouped_defaults_run_one_complete_validation_and_keep_remaining_blockers(
        self,
    ):
        workspace, _dataset, revision, _validation = required_values_workspace(self)
        context = self.app.state.context
        service = context.mapping_workspace
        original = service.check_definition
        with patch.object(service, "check_definition", wraps=original) as check:
            page = self.client.get(f"/workspaces/{workspace}/mapping/required-values")
            self.assertIn("mapping#next-step-blockers", page.text)
            check.assert_not_called()
            response = self.client.post(
                f"/workspaces/{workspace}/mapping/save",
                data={
                    "csrf_token": self.csrf,
                    "action": "confirm_defaults",
                    "expected_parent_version": str(revision.version),
                    "expected_working_draft_version": "1",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303, response.text)
            check.assert_called_once()
        page = self.client.get(f"/workspaces/{workspace}/mapping/required-values")
        self.assertNotIn("Review checked defaults", page.text)
        self.assertEqual(page.text.count("data-required-field="), 6)
        missing = self.client.get(
            f"/workspaces/{workspace}/mapping/required-values?state=missing"
        )
        self.assertEqual(missing.text.count("data-required-field="), 1)
        self.assertIn("field_0003", missing.text)

    def test_large_checklist_pages_do_not_open_a_database_per_field(self):
        workspace, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=1005, required_scalar_indexes=tuple(range(1005))
        )
        context = self.app.state.context
        context.mapping_workspace.save_working_draft(
            workspace,
            datasets=(
                DatasetMapping(
                    dataset.dataset_id,
                    "res.partner",
                    source_identity_column_keys=(dataset.columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            (dataset.columns[0].stable_key,), key.key_fields
                        ),
                    ),
                    fields=tuple(
                        ScalarFieldMapping(
                            f"field_{i:04d}", dataset.columns[1].stable_key
                        )
                        for i in range(1005)
                    ),
                ),
            ),
            expected_version=None,
            actor=resolve_local_actor(context),
        )
        with collect_duckdb_request_timings() as timings:
            response = self.client.get(
                f"/workspaces/{workspace}/mapping/required-values"
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("of 1006 required fields", response.text)
        self.assertEqual(response.text.count("data-required-field="), 100)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 1)
        self.assertEqual(timings.lock_retry_count, 0)
        next_url = unescape(
            re.search(r'href="([^"]+)"[^>]*>Next 100', response.text).group(1)
        )
        self.assertEqual(
            self.client.get(next_url).text.count("data-required-field="), 100
        )
        last = self.client.get(
            f"/workspaces/{workspace}/mapping/required-values?page=11"
        )
        self.assertIn("Showing 1001–1006", last.text)
        self.assertEqual(last.text.count("data-required-field="), 6)
        with collect_duckdb_request_timings() as main_timings:
            main = self.client.get(f"/workspaces/{workspace}/mapping")
        self.assertEqual(main.status_code, 200)
        self.assertLessEqual(main_timings.connection_count, 8)
        self.assertLessEqual(main_timings.schema_check_count, 2)

    def test_changed_draft_schema_and_invalid_filters_do_not_claim_current_proof(self):
        workspace, _dataset, _revision, _validation = required_values_workspace(self)
        context = self.app.state.context
        draft = context.queries.get_mapping_working_draft(workspace)
        context.mapping_workspace.save_working_draft(
            workspace,
            datasets=(replace(draft.definition.datasets[0], fields=()),),
            expected_version=draft.version,
            actor=resolve_local_actor(context),
        )
        page = self.client.get(
            f"/workspaces/{workspace}/mapping/required-values?state=stale"
        )
        self.assertIn("Needs refresh", page.text)
        self.assertGreater(page.text.count("data-required-field="), 0)
        schema = context.queries.get_odoo_schema_catalog(workspace)
        changed = replace(
            schema,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        field for field in model.fields if field.name != "relation_0000"
                    ),
                )
                for model in schema.models
            ),
        )
        with patch.object(
            context.queries, "get_odoo_schema_catalog", return_value=changed
        ):
            page = self.client.get(f"/workspaces/{workspace}/mapping/required-values")
        self.assertIn("unavailable in the captured Odoo data", page.text)
        removed = re.search(
            r'data-required-field="relation_0000".*?</tr>', page.text, re.DOTALL
        ).group(0)
        self.assertIn("Review Odoo requirements", removed)
        self.assertIn(f'href="/workspaces/{workspace}/schema"', removed)
        self.assertNotIn("#scalar-", removed)
        for query in ("dataset=foreign", "state=forged", "page=-1", "page=999999999"):
            self.assertEqual(
                self.client.get(
                    f"/workspaces/{workspace}/mapping/required-values?{query}",
                    follow_redirects=False,
                ).status_code,
                303,
            )

    def test_duplicate_labels_open_the_exact_scalar_and_relationship_fields(self):
        workspace, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=150,
            required_scalar_indexes=tuple(range(150)),
            relationship_field_count=150,
            required_relationship_indexes=tuple(range(150)),
        )
        context = self.app.state.context
        context.mapping_workspace.save_working_draft(
            workspace,
            datasets=(
                DatasetMapping(
                    dataset.dataset_id,
                    "res.partner",
                    source_identity_column_keys=(dataset.columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            (dataset.columns[0].stable_key,), key.key_fields
                        ),
                    ),
                ),
            ),
            expected_version=None,
            actor=resolve_local_actor(context),
        )
        schema = context.queries.get_odoo_schema_catalog(workspace)
        duplicate_labels = replace(
            schema,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        replace(field, label="Shared label")
                        if field.name != "ref"
                        else field
                        for field in model.fields
                    ),
                )
                for model in schema.models
            ),
        )
        with patch.object(
            context.queries, "get_odoo_schema_catalog", return_value=duplicate_labels
        ):
            for field, page_number, anchor in (
                ("field_0149", 2, "scalar-0-field_0149"),
                ("relation_0149", 4, "relationship-0-relation_0149"),
            ):
                page = self.client.get(
                    f"/workspaces/{workspace}/mapping/required-values?page={page_number}"
                )
                row = re.search(
                    rf'data-required-field="{field}".*?</tr>', page.text, re.DOTALL
                ).group(0)
                url = unescape(re.search(r'href="([^"]+)"', row).group(1))
                target = self.client.get(url)
                self.assertEqual(target.status_code, 200, target.text)
                self.assertIn(f'id="{anchor}"', target.text)
                self.assertIn('value="Shared label"', target.text)

    def test_injected_worker_projection_keeps_raw_inputs_out_of_the_route(self):
        workspace, _dataset, _revision, _validation = required_values_workspace(self)
        context = self.app.state.context
        projection = context.required_values.review(
            workspace, actor=resolve_local_actor(context)
        )
        with (
            patch.object(
                context.required_values, "review", return_value=projection
            ) as receipt,
            patch.object(
                context.queries,
                "get_mapping_source_selection",
                side_effect=AssertionError("Server page cannot acquire source inputs"),
            ),
            patch.object(
                context.queries,
                "get_mapping_working_draft",
                side_effect=AssertionError("Server page cannot acquire literals"),
            ),
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=AssertionError("Server page cannot acquire defaults"),
            ),
        ):
            response = self.client.get(
                f"/workspaces/{workspace}/mapping/required-values"
            )
        self.assertEqual(response.status_code, 200, response.text)
        receipt.assert_called_once()
        self.assertEqual(response.text.count("data-required-field="), 6)
