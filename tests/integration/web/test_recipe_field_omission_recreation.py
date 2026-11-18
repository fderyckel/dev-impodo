"""Recreate nonempty Recipe omission evidence from the same closed comparison."""

from __future__ import annotations

from contextlib import closing
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import load_workbook

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_recipe_comparison_recovery as recipes
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.identity import resolve_local_actor
from tests.support.recipe_lifecycle import metadata_for_schema

_SECRET = "EXCLUDED_PRIVATE_NOTE"


def _configured_recipe(definition, major):
    target = definition["odoo_target_contract"]
    target["odoo_major_version"] = major
    target["reference_policy_hash"] = reference_policy_hash(major)
    definition["source_shape"]["datasets"][0]["columns"].append(
        {
            "logical_column_id": "column:balances.notes",
            "source_name": "Internal note",
            "candidate_type": "STRING",
            "required_by": ["exclusion"],
        }
    )
    definition["mapping"]["datasets"][0]["excluded_source_column_ids"] = [
        "column:balances.notes"
    ]


def _reader(schema):
    def read(requirements):
        metadata = metadata_for_schema(
            schema, {item.model: item.fields for item in requirements.metadata_requests}
        )
        fields = {item.model: item.fields for item in requirements.record_requests}
        return metadata, RecordSnapshot(
            metadata.fingerprint, {model: () for model in fields}, fields
        )

    return read


class RecipeFieldOmissionRecreationTests(TestCase):
    def test_closed_recipe_recreates_the_same_explicit_omission_for_both_odoo_majors(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture = recipes.RecipeComparisonRecoveryBrowserTests()
                fixture.setUp()
                try:
                    self._assert_recreated(fixture, major)
                finally:
                    fixture.doCleanups()

    def _assert_recreated(self, fixture, major):
        fresh = fixture.fixture
        source = f"Name,Amount,Internal note\nAcme,100.00,{_SECRET}\nBeta,25.50,{_SECRET}\n".encode()
        url = fresh.registered_delivery(
            configure_recipe=lambda definition: _configured_recipe(definition, major),
            source_csv=source,
        )
        page = fixture.client.get(url)
        accepted = fixture.client.post(
            url + "/accept",
            data={
                "csrf_token": recipes.fixtures.field(page, "csrf_token"),
                "parameter_revision": "",
                "warnings_acknowledged": "1",
                "control_0": "125.50",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        application = fresh._activate_with_real_compiler(odoo_version=f"{major}.0")
        context, actor = fixture.context, resolve_local_actor(fixture.context)
        workspace_id = application.workspace_id
        summary, evaluation, _ = context.normalization.current_review(workspace_id)
        for group in evaluation.groups:
            if group.requires_decision:
                summary = context.normalization.decide_group(
                    workspace_id,
                    summary.run_id,
                    group.group_id,
                    approve=True,
                    expected_version=summary.lifecycle_version,
                    actor=actor,
                )
        context.normalization.approve(
            workspace_id,
            summary.run_id,
            expected_version=summary.lifecycle_version,
            actor=actor,
        )
        report = run_local_operation_sync(
            context.preflight.compare,
            workspace_id,
            reader=_reader(context.queries.get_odoo_schema_catalog(workspace_id)),
            actor=actor,
        )
        self.assertEqual(report.status, "READY")
        project = fixture.client.get(f"/projects/{application.project_id}")
        csrf = recipes.fixtures.field(project, "csrf_token")
        first = self._package(fixture, workspace_id, report.run_id, csrf)
        workspace = context.migration_workspaces.get(workspace_id, actor=actor)
        context.migration_workspaces.close(
            workspace_id, actor=actor, expected_revision=workspace.optimistic_revision
        )
        second = self._package(fixture, workspace_id, report.run_id, csrf)
        self.assertEqual(first, second)
        self.assertIn("Intentionally excluded", tuple(row[0] for row in first))
        self.assertIn("Internal note", tuple(row[3] for row in first))
        self.assertIn("Whole column", tuple(row[6] for row in first))
        current = run_local_operation_sync(
            context.preflight.current_report, workspace_id
        )
        self.assertEqual(
            (current.run_id, current.result_hash), (report.run_id, report.result_hash)
        )

    def _package(self, fixture, workspace_id, comparison_id, csrf):
        context, client = fixture.context, fixture.client
        prefix = f"/workspaces/{workspace_id}/summary"
        with (
            patch.object(
                context,
                "readiness_reader",
                side_effect=AssertionError("Export contacted Odoo"),
            ),
            patch.object(
                context.preflight.artifacts,
                "materialize_source",
                side_effect=AssertionError("Export reopened upload"),
            ),
            patch.object(
                context.preflight.artifacts,
                "materialize_source_snapshot",
                side_effect=AssertionError("Whole-column omission read original cells"),
            ),
            collect_duckdb_request_timings() as timings,
        ):
            generated = client.post(
                prefix + "/package",
                data={
                    "csrf_token": csrf,
                    "comparison_id": comparison_id,
                },
                headers={**POST_HEADERS, "Accept": "application/json"},
                follow_redirects=False,
            )
        self.assertEqual(generated.status_code, 200, generated.text)
        self.assertLessEqual(timings.connection_count, 6)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        downloaded = client.get(
            prefix + "/workbook", params={"comparison_id": comparison_id}
        )
        self.assertEqual(downloaded.status_code, 200)
        with ZipFile(BytesIO(downloaded.content)) as archive:
            self.assertNotIn(
                _SECRET,
                "".join(
                    archive.read(name).decode()
                    for name in archive.namelist()
                    if name.endswith(".xml")
                ),
            )
        with closing(load_workbook(BytesIO(downloaded.content))) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["D4"].value, "1 whole column")
            self.assertEqual(sheet["G4"].value, "1 saved findings")
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
            return tuple(sheet.iter_rows(min_row=7, values_only=True))
