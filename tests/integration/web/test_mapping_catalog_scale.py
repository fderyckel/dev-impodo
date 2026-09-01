"""Large mapping-catalog browser evidence."""

from __future__ import annotations

import asyncio
import json
from statistics import median
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

import psutil

from impodo.web.diagnostics import parse_server_timing
from impodo.web.routers import mapping as mapping_router
from tests.support.browser_scenarios import (
    POST_HEADERS,
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ProjectSetupBrowserTestCase,
    ScalarFieldMapping,
    ScalarValueSource,
)


class LargeMappingCatalogBrowserTests(ProjectSetupBrowserTestCase):
    def test_large_mapping_catalog_is_paged_and_saved_sparsely(self) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1500
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        initial = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=tuple(
                        ScalarFieldMapping(
                            target_field=f"field_{index:04d}",
                            source_column_key=source_value.stable_key,
                            value_source=ScalarValueSource.SOURCE,
                        )
                        for index in range(1500)
                    ),
                ),
            ),
            expected_version=None,
            actor=context.actor,
        )
        self.assertEqual(initial.version, 1)

        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count("data-scalar-mapping-row"), 3)
        self.assertIn("Showing 3 of 1500 fields", page.text)
        self.assertIn("Page 1 of 500", page.text)
        self.assertIn("field_0000", page.text)
        self.assertNotIn("field_0003</code>", page.text)
        self.assertIn("template data-source-column-options", page.text)
        self.assertLess(page.text.count(source_value.stable_key), 100)

        last_page = self.client.get(
            f"/workspaces/{workspace_id}/mapping?scalar_page=500"
        )
        self.assertEqual(last_page.text.count("data-scalar-mapping-row"), 3)
        self.assertIn("field_1499", last_page.text)
        self.assertIn("scalar_page=500", last_page.text)

        expanded = self.client.get(
            f"/workspaces/{workspace_id}/mapping?scalar_page_size=50"
        )
        self.assertEqual(expanded.text.count("data-scalar-mapping-row"), 50)
        self.assertIn("field_0049", expanded.text)
        self.assertNotIn("field_0050</code>", expanded.text)
        self.assertIn('data-scalar-page-size="50"', expanded.text)

        rejected_size = self.client.get(
            f"/workspaces/{workspace_id}/mapping?scalar_page_size=100"
        )
        self.assertEqual(
            rejected_size.text.count("data-scalar-mapping-row"),
            3,
        )
        self.assertIn('data-scalar-page-size="3"', rejected_size.text)

        searched = self.client.get(
            f"/workspaces/{workspace_id}/mapping?field_query=field_1499"
        )
        self.assertEqual(searched.status_code, 200)
        self.assertEqual(searched.text.count("data-scalar-mapping-row"), 1)
        self.assertIn("field_1499", searched.text)
        self.assertNotIn("field_0000</code>", searched.text)

        fragment = self.client.get(
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            "?field_query=field_1499"
        )
        self.assertEqual(fragment.status_code, 200)
        self.assertEqual(fragment.text.count("data-scalar-mapping-row"), 1)
        self.assertIn("field_1499", fragment.text)
        self.assertNotIn("data-relation-field-catalog", fragment.text)
        self.assertNotIn("<main", fragment.text)
        self.assertEqual(fragment.headers["cache-control"], "no-store")
        self.assertEqual(
            fragment.headers["x-impodo-catalog-projection"],
            "miss",
        )
        self.assertIn("projection;dur=", fragment.headers["server-timing"])

        reused_projection = self.client.get(
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            "?field_query=field_1498"
        )
        self.assertEqual(reused_projection.status_code, 200)
        self.assertEqual(
            reused_projection.headers["x-impodo-catalog-projection"],
            "hit",
        )

        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", "1"],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "constant"],
            ["scalar_literal_0_1", "Updated safely"],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
        ]
        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn("redirect_url", saved.json())
        self.assertEqual(saved.json()["expected_working_draft_version"], 2)
        self.assertIn("saved_at", saved.json())
        self.assertEqual(
            saved.json()["redirect_url"],
            f"/workspaces/{workspace_id}/mapping#mapping-dataset-0",
        )
        changed_projection = self.client.get(
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            "?field_query=field_1498"
        )
        self.assertEqual(changed_projection.status_code, 200)
        self.assertEqual(
            changed_projection.headers["x-impodo-catalog-projection"],
            "miss",
        )
        saved_again_entries = [list(entry) for entry in entries]
        for entry in saved_again_entries:
            if entry[0] == "expected_working_draft_version":
                entry[1] = "2"
            elif entry[0] == "scalar_literal_0_1":
                entry[1] = "Updated safely again"
        saved_again = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": saved_again_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(saved_again.status_code, 200)
        self.assertEqual(
            saved_again.json()["expected_working_draft_version"],
            3,
        )
        working = context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
        self.assertEqual(working.version, 3)
        self.assertEqual(len(working.definition.datasets[0].fields), 1500)
        updated = {
            item.target_field: item
            for item in working.definition.datasets[0].fields
        }
        self.assertEqual(
            updated["field_0000"].literal_value,
            "Updated safely again",
        )
        self.assertEqual(
            updated["field_1499"].source_column_key,
            source_value.stable_key,
        )

        denied = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": "incorrect",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id).version,
            3,
        )

        invalid_entries = [
            list(item)
            for item in entries
            if item[0] not in {"source_identity_0", "identity_source_0_0"}
        ]
        for item in invalid_entries:
            if item[0] == "action":
                item[1] = "submit"
            elif item[0] == "expected_working_draft_version":
                item[1] = "3"
        invalid = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": invalid_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["expected_working_draft_version"], 3)
        self.assertIsNone(invalid.json()["expected_parent_version"])
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            ).version,
            3,
        )
        self.assertIsNone(
            context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )

        retry_entries = [list(item) for item in entries]
        for item in retry_entries:
            if item[0] == "expected_working_draft_version":
                item[1] = "3"
            elif item[0] == "expected_parent_version":
                item[1] = ""
        retried = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": retry_entries},
            headers={
                **POST_HEADERS,
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id).version,
            4,
        )

        oversized = b'{"entries":[["csrf_token","' + (
            b"x" * (5 * 1024 * 1024)
        ) + b'"]]}'
        rejected = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            content=oversized,
            headers={
                **POST_HEADERS,
                "Content-Type": "application/json",
                "X-CSRF-Token": self.csrf,
            },
        )
        self.assertEqual(rejected.status_code, 413)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id).version,
            4,
        )

        excessive_form = "&".join(
            [f"field_{index}=x" for index in range(25_001)]
        )
        recovered = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            content=excessive_form,
            headers={
                **POST_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        self.assertEqual(recovered.status_code, 303)
        self.assertIn("save_error=request_rejected", recovered.headers["location"])
        recovery_page = self.client.get(recovered.headers["location"])
        self.assertIn("No mapping change was saved", recovery_page.text)

    def test_complete_mapping_render_runs_outside_the_event_loop(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )
        original = mapping_router._render_mapping
        running_loop_observed: list[bool] = []

        def inspected_renderer(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                running_loop_observed.append(False)
            else:
                running_loop_observed.append(True)
            return original(*args, **kwargs)

        with patch.object(
            mapping_router,
            "_render_mapping",
            side_effect=inspected_renderer,
        ):
            page = self.client.get(f"/workspaces/{workspace_id}/mapping")

        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(running_loop_observed, [False])
        self.assertIn("queue_wait;dur=", page.headers["server-timing"])

    def test_obsolete_editor_generation_is_rejected_before_projection_work(
        self,
    ) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=20
        )
        editor_id = str(uuid4())
        original = mapping_router._render_mapping_field_catalog

        def search(generation: int):
            return self.client.get(
                f"/workspaces/{workspace_id}/mapping/field-catalog"
                f"?field_query=field_{generation:04d}"
                f"&editor_id={editor_id}&generation={generation}"
            )

        with patch.object(
            mapping_router,
            "_render_mapping_field_catalog",
            wraps=original,
        ) as renderer:
            current = search(3)
            obsolete = search(2)

        self.assertEqual(current.status_code, 200)
        self.assertEqual(obsolete.status_code, 204)
        self.assertEqual(
            current.headers["x-impodo-catalog-result"],
            "current",
        )
        self.assertEqual(
            obsolete.headers["x-impodo-catalog-result"],
            "superseded",
        )
        self.assertEqual(renderer.call_count, 1)
        self.assertIn("field_0003", current.text)

    def test_representative_mapping_responsiveness_baseline_is_reported(
        self,
    ) -> None:
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1000
        )
        source_identity, source_value = dataset.columns
        context = self.app.state.context
        process = psutil.Process()
        rss_before = process.memory_info().rss
        initial = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(source_identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(source_identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=tuple(
                        ScalarFieldMapping(
                            target_field=f"field_{index:04d}",
                            source_column_key=source_value.stable_key,
                            value_source=ScalarValueSource.SOURCE,
                        )
                        for index in range(1000)
                    ),
                ),
            ),
            expected_version=None,
            actor=context.actor,
        )
        self.assertEqual(initial.version, 1)

        def timed_get(url: str):
            started = perf_counter()
            response = self.client.get(url)
            return response, (perf_counter() - started) * 1000

        page_url = f"/workspaces/{workspace_id}/mapping"
        cold_page, cold_page_ms = timed_get(page_url)
        self.assertEqual(cold_page.status_code, 200)
        cold_phases = parse_server_timing(
            cold_page.headers.get("server-timing", "")
        )
        self.assertEqual(
            set(cold_phases),
            {
                "queue_wait",
                "workspace_read",
                "view_build",
                "render",
                "total",
            },
        )

        search_url = (
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            "?field_query=field_0999"
        )
        warmup, _warmup_ms = timed_get(search_url)
        self.assertEqual(warmup.status_code, 200)
        warm_search_ms = []
        warm_search_phases = {}
        for _index in range(3):
            response, elapsed_ms = timed_get(search_url)
            self.assertEqual(response.status_code, 200)
            warm_search_ms.append(elapsed_ms)
            warm_search_phases = parse_server_timing(
                response.headers.get("server-timing", "")
            )
        self.assertEqual(
            set(warm_search_phases),
            {
                "queue_wait",
                "workspace_read",
                "view_build",
                "projection",
                "render",
                "total",
            },
        )

        burst_editor_id = str(uuid4())
        burst_urls = tuple(
            f"/workspaces/{workspace_id}/mapping/field-catalog"
            f"?field_query=field_{index:04d}"
            f"&editor_id={burst_editor_id}&generation={generation}"
            for generation, index in zip(
                (4, 3, 2, 1),
                (998, 997, 996, 995),
                strict=True,
            )
        )
        burst_started = perf_counter()
        burst_results = tuple(timed_get(url) for url in burst_urls)
        burst_wall_ms = (perf_counter() - burst_started) * 1000
        self.assertEqual(
            [response.status_code for response, _elapsed in burst_results],
            [200, 204, 204, 204],
        )

        entries = [
            ["csrf_token", self.csrf],
            ["action", "save_progress"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", "1"],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "upsert"],
            ["on_existing_0", "block"],
            ["source_identity_0", source_identity.stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", source_identity.stable_key],
            ["visible_scalar_target_0", "field_0000"],
            ["scalar_value_source_0_1", "constant"],
            ["scalar_literal_0_1", "Measured save"],
            ["scalar_type_0_1", "string"],
            ["scalar_case_0_1", "preserve"],
            ["scalar_compare_0_1", "1"],
            ["scalar_null_0_1", "distinct"],
        ]
        save_started = perf_counter()
        saved = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        save_ms = (perf_counter() - save_started) * 1000
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["expected_working_draft_version"], 2)

        stale_started = perf_counter()
        stale = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        stale_ms = (perf_counter() - stale_started) * 1000
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            stale.json()["failure_code"],
            "MAPPING_VERSION_CONFLICT",
        )
        self.assertEqual(stale.json()["expected_working_draft_version"], 2)

        memory_info = process.memory_info()
        peak_memory_bytes = getattr(memory_info, "peak_wset", memory_info.rss)
        baseline = {
            "catalogue_scalar_fields": 1000,
            "cold_page_ms": round(cold_page_ms, 3),
            "cold_page_server_timings_ms": cold_phases,
            "warm_search_median_ms": round(median(warm_search_ms), 3),
            "warm_search_server_timings_ms": warm_search_phases,
            "coalesced_search_burst_wall_ms": round(burst_wall_ms, 3),
            "coalesced_search_burst_each_ms": [
                round(elapsed_ms, 3)
                for _response, elapsed_ms in burst_results
            ],
            "save_ms": round(save_ms, 3),
            "stale_version_ms": round(stale_ms, 3),
            "process_peak_memory_mib": round(
                peak_memory_bytes / (1024 * 1024),
                3,
            ),
            "workflow_rss_growth_mib": round(
                max(0, memory_info.rss - rss_before) / (1024 * 1024),
                3,
            ),
            "mapping_render_event_loop_bound": False,
        }
        print(
            "mapping_responsiveness_baseline="
            + json.dumps(baseline, sort_keys=True)
        )
        self.assertEqual(
            set(baseline),
            {
                "catalogue_scalar_fields",
                "cold_page_ms",
                "cold_page_server_timings_ms",
                "warm_search_median_ms",
                "warm_search_server_timings_ms",
                "coalesced_search_burst_wall_ms",
                "coalesced_search_burst_each_ms",
                "save_ms",
                "stale_version_ms",
                "process_peak_memory_mib",
                "workflow_rss_growth_mib",
                "mapping_render_event_loop_bound",
            },
        )
        self.assertLess(cold_page_ms, 20_000)
        self.assertLess(median(warm_search_ms), 5_000)
        self.assertLess(burst_wall_ms, 10_000)
        self.assertLess(save_ms, 5_000)
        self.assertLess(stale_ms, 5_000)
        self.assertLess(
            baseline["process_peak_memory_mib"],
            1_024,
        )
