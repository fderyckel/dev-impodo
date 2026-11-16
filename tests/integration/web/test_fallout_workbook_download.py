"""Download exact-cell workbooks from unchanged physical source evidence."""

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from openpyxl import load_workbook

from impodo.adapters.artifacts.fallout_workbook import FALLOUT_FILL
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import SourceDataset, SourceDatasetColumn
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.integration.web import (
    test_recipe_preparation_current_result as recipe_fixtures,
)
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements

HASH = "sha256:" + "a" * 64
EFFECTIVE_HASH = "sha256:" + "b" * 64


class FalloutWorkbookDownloadTests(ProjectSetupBrowserTestCase):
    def _evidence(self, workspace_id, *, context=None, patches=None):
        enter = patches.enter_context if patches is not None else self.enterContext
        source = Path(self.temporary.name) / "records.csv"
        source.write_bytes(b"Code,Name\nA1,Alpha\n")
        original = source.read_bytes()
        digest = sha256(original).hexdigest()
        dataset = SourceDataset(
            dataset_id="physical-records",
            name="records",
            source=FileSourceBinding(
                file_id="file-1",
                table_key="csv",
                source_sha256=f"sha256:{digest}",
                catalog_hash=HASH,
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            ),
            row_count=1,
            columns=(
                SourceDatasetColumn(1, "Code", "column:code", "string"),
                SourceDatasetColumn(2, "Name", "column:name", "string"),
            ),
        )
        selection = SimpleNamespace(
            content_hash=HASH,
            datasets=(dataset,),
            data_version_id="data-1",
        )
        staging = SimpleNamespace(
            physical_selection_hash=HASH,
            source_selection_hash=EFFECTIVE_HASH,
            rows=(
                SimpleNamespace(
                    row_id="row-1",
                    lineage=SimpleNamespace(
                        field_sources={"name": ("column:name",)},
                        physical_sources={dataset.dataset_id: (2,)},
                    ),
                ),
            ),
        )
        report = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            workspace_id=workspace_id,
            execution_run_id=str(uuid4()),
            snapshot_hash=HASH,
            target_hash=HASH,
            target_database="fixture",
            status=ReconciliationRunStatus.FALLOUT,
            verified_at=datetime.now(UTC),
            verified_by="Local operator",
            unchanged_count=0,
            rows=(
                ReconciliationRow(
                    row_id="row-1",
                    dataset="combined",
                    source_row=2,
                    target_model="res.partner",
                    operation="CREATE",
                    execution_status="COMMITTED",
                    status=ReconciliationRowStatus.DIFFERENT,
                    odoo_id=42,
                    differing_fields=("name",),
                ),
            ),
        )
        detail = SimpleNamespace(
            differences=(
                SimpleNamespace(
                    source_trace_id="row-1",
                    field="name",
                    target_model="res.partner",
                    odoo_id=42,
                    expected_value="Alpha",
                    observed_value="Changed by Odoo",
                    reason_code="VALUE_DIFFERENT",
                ),
            )
        )
        service = (context or self.app.state.context).fallout_workbooks

        @contextmanager
        def materialize(data_version_id, stored_name):
            self.assertEqual((data_version_id, stored_name), ("data-1", "records.csv"))
            yield source

        enter(
            patch.object(type(service._reconciliation), "current", return_value=report)
        )
        enter(
            patch.object(
                type(service._reconciliation), "current_detail", return_value=detail
            )
        )
        enter(
            patch.object(
                type(service._reconciliation.execution),
                "get_run",
                return_value=SimpleNamespace(preflight_run_id="preflight-1"),
            )
        )
        enter(
            patch.object(
                type(service._preflight),
                "execution_snapshot",
                return_value=SimpleNamespace(
                    staging_run_id="staging-1",
                    staging_content_hash=HASH,
                    rows=(
                        SimpleNamespace(
                            source_trace_id="row-1", business_identity=("A1",)
                        ),
                    ),
                ),
            )
        )
        enter(
            patch.object(
                type(service._staging),
                "get_canonical_staging_run",
                return_value=staging,
            )
        )
        enter(
            patch.object(
                service,
                "_sources",
                SimpleNamespace(
                    get_source_selection=lambda _id: selection,
                    get_source_catalogs=lambda _id: (),
                ),
            )
        )
        enter(
            patch.object(
                service,
                "_workspaces",
                SimpleNamespace(
                    get=lambda _id: SimpleNamespace(
                        source_files=(
                            SimpleNamespace(
                                file_id="file-1",
                                display_name="records.csv",
                                stored_name="records.csv",
                                sha256=digest,
                            ),
                        )
                    ),
                ),
            )
        )
        enter(
            patch.object(
                type(service._artifacts), "materialize_source", side_effect=materialize
            )
        )
        return source, original, selection, staging

    def test_combined_selection_exports_on_return_visits_for_both_odoo_versions(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode), ExitStack() as patches:
                    workspace_id, _, _ = self._mapping_ready_workspace(
                        scalar_field_count=0,
                        odoo_major=major,
                        connection_mode=mode,
                    )
                    source, original, _selection, _staging = self._evidence(
                        workspace_id,
                        patches=patches,
                    )
                    for _visit in range(2):
                        with (
                            collect_duckdb_request_timings() as timings,
                            count_duckdb_statements() as statements,
                        ):
                            response = self.client.get(
                                f"/workspaces/{workspace_id}/load/fallout.xlsx",
                                follow_redirects=False,
                            )
                        self.assertEqual(response.status_code, 200, response.text[:200])
                        self.assertIn(
                            "attachment; filename=",
                            response.headers["content-disposition"],
                        )
                        workbook = load_workbook(BytesIO(response.content))
                        self.assertEqual(workbook["records"]["B2"].value, "Alpha")
                        self.assertEqual(
                            workbook["records"]["B2"].fill.fgColor.rgb[-6:],
                            FALLOUT_FILL,
                        )
                        self.assertIn(
                            "Changed by Odoo", workbook["records"]["B2"].comment.text
                        )
                        self.assertIn("Impodo Fallout", workbook.sheetnames)
                        workbook.close()
                        self.assertEqual(source.read_bytes(), original)
                        # Admission opens the registry and workspace identity
                        # stores once each. Mocked export evidence opens no stores.
                        self.assertEqual(timings.connection_count, 2)
                        self.assertLessEqual(timings.schema_check_count, 1)
                        self.assertLessEqual(len(statements), 8)

    def test_changed_physical_selection_returns_to_saved_outcome(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=0)
        source, original, selection, _staging = self._evidence(workspace_id)
        selection.content_hash = EFFECTIVE_HASH
        response = self.client.get(
            f"/workspaces/{workspace_id}/load/fallout.xlsx",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"], f"/workspaces/{workspace_id}/load/outcome"
        )
        self.assertEqual(source.read_bytes(), original)

    def test_recipe_applications_can_download_for_both_odoo_versions(self):
        for major in (19, 20):
            with self.subTest(major=major), ExitStack() as patches:
                fixture, application, *_ = (
                    recipe_fixtures.RecipePreparationCurrentResultTests.prepared_case(
                        self, major
                    )
                )
                source, original, _, _ = self._evidence(
                    application.workspace_id,
                    context=fixture.context,
                    patches=patches,
                )
                response = fixture.client.get(
                    f"/workspaces/{application.workspace_id}/load/fallout.xlsx",
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 200, response.text[:200])
                workbook = load_workbook(BytesIO(response.content))
                self.assertEqual(workbook["records"]["B2"].value, "Alpha")
                workbook.close()
                self.assertEqual(source.read_bytes(), original)

    def test_denied_admission_does_not_open_protected_export_evidence(self):
        context = self.app.state.context
        with patch.object(context.fallout_workbooks, "generate") as generate:
            response = self.client.get(f"/workspaces/{uuid4()}/load/fallout.xlsx")
        self.assertEqual(response.status_code, 404)
        generate.assert_not_called()
