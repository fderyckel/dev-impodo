"""Qualify exact saved field counts for local and remote Odoo 19 and 20."""

import re
from dataclasses import replace
from html import unescape
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.mapping.field_effects import FieldEffectsProjection
from impodo.domain.mapping.contracts import (
    ScalarValueSource,
)
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase, POST_HEADERS
from tests.support.field_effects import field_effects_workspace
from tests.support.identity import resolve_local_actor


class FieldEffectsBrowserTests(ProjectSetupBrowserTestCase):
    def test_counts_links_and_stale_decisions_for_both_majors_and_access_modes(self):
        for major in (19, 20):
            for remote in (False, True):
                with self.subTest(major=major, remote=remote):
                    workspace, dataset, revision, snapshot = field_effects_workspace(
                        self, major=major, remote=remote
                    )
                    context = self.app.state.context
                    with (
                        patch.object(
                            context.transformation_impacts,
                            "prepare_snapshot",
                            side_effect=AssertionError("No evaluation on editor read"),
                        ),
                        patch.object(
                            context.mapping_workspace.validator,
                            "validate",
                            side_effect=AssertionError("No validation on editor read"),
                        ),
                        collect_duckdb_request_timings() as timing,
                    ):
                        page = self.client.get(
                            f"/workspaces/{workspace}/mapping?scalar_page_size=50"
                        )
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertEqual(
                        page.text.count('data-field-effects-state="Current"'), 6
                    )
                    self.assertNotIn("Private original", page.text)
                    self.assertNotIn("Private prepared", page.text)
                    self.assertLessEqual(timing.connection_count, 8)
                    self.assertLessEqual(timing.schema_check_count, 6)
                    self.assertEqual(timing.lock_retry_count, 0)
                    for index, counts in enumerate(snapshot.report.field_impacts):
                        row = re.search(
                            rf'<tr id="scalar-0-field_{index:04d}".*?</tr>',
                            page.text,
                            re.DOTALL,
                        ).group(0)
                        self.assertIn(f"{counts.counts.unchanged_count} unchanged", row)
                        url = unescape(
                            re.search(
                                r'href="([^"]+transformation-impact[^"]+)"', row
                            ).group(1)
                        )
                        self.assertIn(f"dataset={dataset.name}", url)
                        self.assertIn(f"field=field_{index:04d}", url)
                        review = self.client.get(url)
                        self.assertEqual(review.status_code, 200, review.text)
                        self.assertIn(
                            f'<option value="field_{index:04d}" selected', review.text
                        )
                        self.assertIn(f"Counts for {dataset.name}", review.text)
                        export = self.client.post(
                            f"/workspaces/{workspace}/mapping/transformation-impact.csv",
                            data={"csrf_token": self.csrf, "dataset": dataset.name, "field": f"field_{index:04d}"},
                            headers=POST_HEADERS,
                        )
                        self.assertEqual(export.status_code, 200)
                        self.assertEqual(len(export.text.splitlines()) - 1, counts.counts.impact_count)
                    draft = context.queries.get_mapping_working_draft(workspace)
                    mapping = revision.definition.datasets[0]
                    context.mapping_workspace.save_working_draft(
                        workspace,
                        datasets=(
                            replace(
                                mapping,
                                fields=(
                                    replace(
                                        mapping.fields[0],
                                        value_source=ScalarValueSource.CONSTANT,
                                        literal_value="Edited",
                                    ),
                                    *mapping.fields[1:],
                                ),
                            ),
                        ),
                        expected_version=draft.version,
                        actor=resolve_local_actor(context),
                    )
                    stale = self.client.get(
                        f"/workspaces/{workspace}/mapping?scalar_page_size=50"
                    )
                    self.assertEqual(
                        stale.text.count('data-field-effects-state="Needs refresh"'), 6
                    )

    def test_large_catalog_queries_only_visible_fields_inside_one_read_scope(self):
        workspace, _dataset, _revision, _snapshot = field_effects_workspace(
            self, field_count=1000
        )
        context = self.app.state.context
        query = context.field_effects.review
        with patch.object(context.field_effects, "review", wraps=query) as calls:
            for suffix, size in (
                ("?scalar_page_size=50", 50),
                ("/field-catalog?scalar_page=334", 1),
            ):
                with collect_duckdb_request_timings() as timing:
                    page = self.client.get(f"/workspaces/{workspace}/mapping{suffix}")
                self.assertEqual(page.status_code, 200, page.text)
                self.assertEqual(len(calls.call_args.args[2]), size)
                self.assertEqual(
                    page.text.count('data-field-effects-state="Current"'), size
                )
                self.assertLessEqual(timing.connection_count, 8)
                self.assertLessEqual(timing.schema_check_count, 6)
                self.assertEqual(timing.lock_retry_count, 0)

    def test_injected_worker_projection_does_not_read_local_impact_payloads(self):
        workspace, _dataset, _revision, _snapshot = field_effects_workspace(self)
        context = self.app.state.context
        with (
            patch.object(
                context.field_effects, "review", return_value=FieldEffectsProjection()
            ) as worker,
            patch.object(
                context.queries,
                "get_transformation_field_effects",
                side_effect=AssertionError("Server must use injected projection"),
            ),
        ):
            page = self.client.get(f"/workspaces/{workspace}/mapping")
        self.assertEqual(page.status_code, 200)
        worker.assert_called_once()
        self.assertIn('data-field-effects-state="Not checked"', page.text)
