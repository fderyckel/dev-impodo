"""Recover scalar formula originals with the direct export's bounded local I/O."""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.preflight_service import MANIFEST_NAME
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.preflight.original_values import OriginalValueState
from impodo.domain.recipe.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
)
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_comparison_original_workbook as workbooks
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class ScalarOriginalWorkbookTests(HierarchyWorkflowFixture):
    def configure_scalar(
        self, major, mode, formula, *, payload=None, concatenate=False
    ):
        workspace_id = self.freeze_files(
            {"measurements": payload or b"Code,Amount\nN-01, 000bad \nN-02,0002\n"}
        )
        self.schema(
            workspace_id,
            major=major,
            mode=mode,
            extra_models=(
                SchemaModel(
                    "x.measurement",
                    "Measurement",
                    (
                        SchemaField(
                            "name", "Name", "char", False, False, None, None, ()
                        ),
                        SchemaField(
                            "amount",
                            "Amount",
                            "char" if concatenate else "float",
                            False,
                            False,
                            None,
                            None,
                            (),
                        ),
                    ),
                ),
            ),
            extra_keys=(
                BusinessKeyDefinition(
                    "measurement:name",
                    "x.measurement",
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        (source,) = self.context.queries.get_source_selection(workspace_id).datasets
        columns = {column.source_name: column.stable_key for column in source.columns}
        self.submit(
            workspace_id,
            (
                DatasetMapping(
                    source.dataset_id,
                    "x.measurement",
                    source_identity_column_keys=(columns["Code"],),
                    target_identity=(
                        IdentityComponentMapping((columns["Code"],), ("name",)),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            "amount",
                            value_source=ScalarValueSource.CONCATENATE,
                            value_type="string",
                            validation=ScalarValidationPolicy(pattern="^[0-9]+$"),
                            concatenation=ScalarConcatenation(
                                (columns["Amount"], columns["Numerator"]),
                                separator="",
                            ),
                        )
                        if concatenate
                        else ScalarFieldMapping(
                            "amount",
                            columns["Amount"],
                            value_type="decimal",
                            transform=ScalarTransformPolicy(formula=formula),
                        ),
                    ),
                ),
            ),
        )
        return workspace_id, columns

    def export(self, workspace_id, report, path):
        def build():
            evidence = self.context.preflight.review_workbook_evidence(
                workspace_id, report.run_id
            )
            evidence = populate_workbook_original_values(
                evidence, self.context.preflight.artifacts
            )
            with self.context.artifacts.materialize_report(
                workspace_id, report.run_id, MANIFEST_NAME
            ) as manifest:
                write_review_workbook(manifest, path, review_evidence=evidence)
            return evidence

        sources = self.context.preflight.sources
        artifacts = self.context.preflight.artifacts
        with (
            patch.object(
                self.context,
                "readiness_reader",
                side_effect=AssertionError("Export contacted Odoo"),
            ),
            patch.object(
                artifacts,
                "materialize_source",
                side_effect=AssertionError("Export reopened upload"),
            ),
            patch.object(
                artifacts,
                "materialize_source_snapshot",
                wraps=artifacts.materialize_source_snapshot,
            ) as snapshots,
            patch.object(
                sources, "get_source_selection", wraps=sources.get_source_selection
            ) as selection,
            patch.object(
                sources,
                "get_current_source_snapshots",
                wraps=sources.get_current_source_snapshots,
            ) as manifests,
            patch(
                "impodo.domain.staging.evaluator._apply_scalar_mappings",
                side_effect=AssertionError("Export re-evaluated scalar mapping"),
            ),
            patch(
                "impodo.domain.recipe.value_rules.evaluate_formula",
                side_effect=AssertionError("Export evaluated formula"),
            ),
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            evidence = run_local_operation_sync(build)
        snapshots.assert_called_once()
        selection.assert_called_once_with(workspace_id)
        manifests.assert_called_once_with(workspace_id)
        self.assertEqual(timings.lock_retry_count, 0)
        # The complete synchronous export uses registry, workspace, engine, and
        # accepted Data version owners. No cell introduces another owner.
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 3)
        self.assertLessEqual(len(statements), 161)
        return evidence, (
            timings.connection_count,
            timings.schema_check_count,
            len(statements),
        )

    def test_reopened_formula_omissions_match_direct_export_io_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    direct = self.exercise(major, mode, "")
                    formula = self.exercise(major, mode, "value / 2")
                    self.assertEqual(formula[:2], direct[:2])
                    self.assertLessEqual(formula[2], direct[2])

    def test_executed_formula_and_combination_inputs_survive_reopen(self):
        payload = (
            b"Code,Amount,Numerator,Divisor,Unused\n"
            b"N-01,branch,0004,0000,private unused value\n"
            b"N-02,0002,0004,0002,another unused value\n"
        )
        cases = (
            ("column_3 / column_4", False, ("Numerator", "Divisor")),
            (
                "column_3 / column_4 if column_2 == 'branch' else value",
                False,
                ("Amount", "Numerator", "Divisor"),
            ),
            ("", True, ("Amount", "Numerator")),
        )
        for major, mode in ((19, "LOCAL"), (20, "REMOTE")):
            for formula, concatenate, names in cases:
                with self.subTest(
                    major=major, mode=mode, formula=formula, concatenate=concatenate
                ):
                    workspace_id, columns = self.configure_scalar(
                        major, mode, formula, payload=payload, concatenate=concatenate
                    )
                    self.prepare(workspace_id)
                    self.approve(workspace_id)
                    self.expect_business_writes(workspace_id)
                    target = HierarchyOdoo(
                        self.context.queries.get_odoo_schema_catalog(workspace_id)
                    )
                    report = run_local_operation_sync(
                        self.context.preflight.compare,
                        workspace_id,
                        reader=target.compare,
                        actor=self.actor,
                    )
                    self.assertEqual(report.ready_count, 1)
                    self.assertEqual(
                        workbooks._remove_uploaded_sources(self.context, workspace_id),
                        1,
                    )
                    self.reopen()
                    with TemporaryDirectory() as temporary:
                        saved_rows = []
                        for attempt in (1, 2):
                            path = Path(temporary) / f"executed-inputs-{attempt}.xlsx"
                            evidence, _ = self.export(workspace_id, report, path)
                            findings = evidence.fields_set_aside.findings
                            self.assertEqual(len(findings), 1)
                            self.assertEqual(
                                tuple(
                                    item.coordinate.source_key
                                    for item in findings[0].source_inputs
                                ),
                                tuple(columns[name] for name in names),
                            )
                            self.assertTrue(findings[0].source_inputs_known)
                            self.assertTrue(
                                all(
                                    item.value.state is OriginalValueState.PRESENT
                                    for item in findings[0].source_inputs
                                )
                            )
                            with closing(load_workbook(path)) as workbook:
                                sheet = workbook["Fields set aside"]
                                rows = tuple(
                                    sheet.iter_rows(min_row=7, values_only=True)
                                )
                                self.assertEqual(tuple(row[3] for row in rows), names)
                                self.assertNotIn("private unused value", repr(rows))
                                self.assertEqual(
                                    sheet["A4"].value,
                                    f"{len(names)} identified source cells",
                                )
                                self.assertEqual(sheet["G4"].value, "1 saved findings")
                                saved_rows.append(rows)
                        self.assertEqual(saved_rows[0], saved_rows[1])
                    self.assertFalse(target.writes)

    def exercise(self, major, mode, formula):
        workspace_id, columns = self.configure_scalar(major, mode, formula)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.quarantined_count, 1)
        saved_codes = {issue.code for row in staging.rows for issue in row.issues}
        self.assertIn(
            "SOURCE_FORMULA_INVALID" if formula else "SOURCE_TYPE_INVALID", saved_codes
        )
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        report = run_local_operation_sync(
            self.context.preflight.compare,
            workspace_id,
            reader=target.compare,
            actor=self.actor,
        )
        self.assertEqual(report.ready_count, 1)
        self.assertEqual(
            workbooks._remove_uploaded_sources(self.context, workspace_id), 1
        )
        self.reopen()
        exports = []
        costs = []
        with TemporaryDirectory() as temporary:
            for attempt in (1, 2):
                path = Path(temporary) / f"scalar-{attempt}.xlsx"
                evidence, cost = self.export(workspace_id, report, path)
                costs.append(cost)
                fields = evidence.fields_set_aside
                self.assertEqual(len(fields.findings), 1)
                finding = fields.findings[0]
                self.assertFalse(finding.comparison_issue)
                self.assertEqual(finding.target_field, "amount")
                self.assertEqual(finding.source_keys, (columns["Amount"],))
                self.assertEqual(
                    tuple(item.coordinate.source_key for item in finding.source_inputs),
                    (columns["Amount"],),
                )
                self.assertEqual(
                    finding.source_inputs[0].value.state, OriginalValueState.PRESENT
                )
                self.assertEqual(finding.source_inputs[0].value.text, " 000bad ")
                self.assertEqual(evidence.original_source_requests, ())
                with closing(load_workbook(path)) as workbook:
                    sheet = workbook["Fields set aside"]
                    rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0][3:5], ("Amount", " 000bad "))
                    self.assertEqual(sheet["E7"].data_type, "s")
                    self.assertEqual(sheet["A4"].value, "1 identified source cell")
                    self.assertEqual(sheet["G4"].value, "1 saved findings")
                    exports.append(rows)
            self.assertEqual(exports[0], exports[1])
            self.assertEqual(costs[0], costs[1])
        current = run_local_operation_sync(
            self.context.preflight.current_report, workspace_id
        )
        self.assertEqual(current.run_id, report.run_id)
        self.assertEqual(current.result_hash, report.result_hash)
        current_staging = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        self.assertEqual(current_staging.content_hash, staging.content_hash)
        self.assertFalse(target.writes)
        return costs[0]
