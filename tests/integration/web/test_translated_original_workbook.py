"""Recreate raw scoped-choice originals without replaying saved translations."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.preflight.fields_set_aside import FieldSetAsideReason
from impodo.domain.preflight.original_values import OriginalValueState
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.supporting_lookups import portable_supporting_value
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_comparison_original_workbook as workbooks
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class ScopedContactOdoo(HierarchyOdoo):
    """Expose named company domains and Odoo's many2one display-value shape."""

    def matches(self, model, identifier, domain):
        converted = []
        for token in domain:
            if (
                isinstance(token, (list, tuple))
                and len(token) == 3
                and token[0] == "company_id"
                and token[1] == "="
                and isinstance(token[2], str)
            ):
                company_ids = tuple(
                    record_id
                    for (record_model, record_id), values in self.records.items()
                    if record_model == "res.company" and values["name"] == token[2]
                )
                converted.append(("company_id", "in", company_ids))
            else:
                converted.append(token)
        return super().matches(model, identifier, converted)

    def compare(self, requirements):
        metadata, snapshot = super().compare(requirements)
        records = {}
        for model, rows in snapshot.records.items():
            rendered = []
            for row in rows:
                values = dict(row.values)
                company_id = values.get("company_id")
                if type(company_id) is int:
                    values["company_id"] = (
                        company_id,
                        self.records[("res.company", company_id)]["name"],
                    )
                rendered.append(replace(row, values=values))
            records[model] = tuple(rendered)
        return metadata, replace(snapshot, records=records)


class TranslatedOriginalWorkbookTests(HierarchyWorkflowFixture):
    def configure_translated(self, major, mode):
        workspace_id = self.freeze_files(
            {"notices": b"Code,Contact\nN-01,000745\nN-02,000746\nN-03,000747\n"}
        )

        def field(name, label, kind="char", relation=None):
            return SchemaField(name, label, kind, False, False, relation, None, ())

        self.schema(
            workspace_id,
            major=major,
            mode=mode,
            extra_models=(
                SchemaModel(
                    "x.notice",
                    "Notice",
                    (
                        field("name", "Name"),
                        field("contact_id", "Contact", "many2one", "x.contact"),
                    ),
                ),
                SchemaModel(
                    "x.contact",
                    "Contact",
                    (
                        field("ref", "Reference"),
                        field("company_id", "Company", "many2one", "res.company"),
                    ),
                ),
                SchemaModel("res.company", "Company", (field("name", "Name"),)),
            ),
            extra_keys=(
                BusinessKeyDefinition(
                    "notice:name",
                    "x.notice",
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "contact:ref-company",
                    "x.contact",
                    ("ref",),
                    scope_fields=("company_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "company:name",
                    "res.company",
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        (source,) = self.context.queries.get_source_selection(workspace_id).datasets
        columns = {column.source_name: column.stable_key for column in source.columns}
        relationship = RelationshipMapping(
            "contact_id",
            "many2one",
            (columns["Contact"],),
            resolver=RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model="x.contact",
                key_mappings=(ReferenceKeyMapping(columns["Contact"], "ref"),),
                scope_mappings=(ReferenceKeyMapping(columns["Contact"], "company_id"),),
                value_mappings=tuple(
                    ValueMapping(raw, portable_supporting_value((key, "Company A")))
                    for raw, key in (
                        ("000745", "MATCH-MISSING"),
                        ("000746", "MATCH-AMBIGUOUS"),
                        ("000747", "MATCH-OTHER-COMPANY"),
                    )
                ),
            ),
            categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
        )
        self.submit(
            workspace_id,
            (
                DatasetMapping(
                    source.dataset_id,
                    "x.notice",
                    source_identity_column_keys=(columns["Code"],),
                    target_identity=(
                        IdentityComponentMapping((columns["Code"],), ("name",)),
                    ),
                    relationships=(relationship,),
                ),
            ),
        )
        return workspace_id, columns

    def test_reopened_translated_choices_preserve_raw_cells_once_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self.exercise(major, mode)

    def exercise(self, major, mode):
        workspace_id, columns = self.configure_translated(major, mode)
        staging, _quality = self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = ScopedContactOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        target.records.update(
            {
                ("res.company", 701): {"name": "Company A"},
                ("res.company", 702): {"name": "Company B"},
                ("x.contact", 801): {"ref": "MATCH-AMBIGUOUS", "company_id": 701},
                ("x.contact", 802): {"ref": "MATCH-AMBIGUOUS", "company_id": 701},
                ("x.contact", 803): {"ref": "MATCH-OTHER-COMPANY", "company_id": 702},
            }
        )
        report = run_local_operation_sync(
            self.context.preflight.compare,
            workspace_id,
            reader=target.compare,
            actor=self.actor,
        )
        self.assertEqual(report.blocked_count, 3)
        self.assertEqual(report.ambiguous_count, 0)
        self.assertEqual(
            {item.technical_code for item in report.issue_counts},
            {"REFERENCE_NOT_FOUND", "REFERENCE_AMBIGUOUS"},
        )
        self.assertEqual(
            workbooks._remove_uploaded_sources(self.context, workspace_id), 1
        )
        self.reopen()
        with TemporaryDirectory() as temporary:
            exports = []
            for attempt in (1, 2):
                path = Path(temporary) / f"translated-{attempt}.xlsx"
                with (
                    patch.object(
                        self.context,
                        "readiness_reader",
                        side_effect=AssertionError("Export contacted Odoo"),
                    ),
                    patch.object(
                        self.context.preflight.artifacts,
                        "materialize_source",
                        side_effect=AssertionError("Export reopened upload"),
                    ),
                    patch.object(
                        self.context.preflight.artifacts,
                        "materialize_source_snapshot",
                        wraps=self.context.preflight.artifacts.materialize_source_snapshot,
                    ) as snapshots,
                    patch(
                        "impodo.domain.staging.evaluator._apply_relationship_value_mappings",
                        side_effect=AssertionError("Export replayed translations"),
                    ),
                    patch(
                        "impodo.domain.staging.evaluator.prepare_reference_value",
                        side_effect=AssertionError("Export evaluated reference rules"),
                    ),
                ):
                    evidence = workbooks._export_saved_workbook(
                        self.context, workspace_id, report, path
                    )
                snapshots.assert_called_once()
                fields = evidence.fields_set_aside
                self.assertEqual(len(fields.findings), 3)
                self.assertEqual(
                    tuple(finding.source_keys for finding in fields.findings),
                    ((columns["Contact"],),) * 3,
                )
                self.assertTrue(
                    all(finding.comparison_issue for finding in fields.findings)
                )
                self.assertTrue(
                    all(
                        tuple(item.coordinate.source_key for item in finding.source_inputs)
                        == (columns["Contact"],)
                        for finding in fields.findings
                    )
                )
                self.assertEqual(
                    tuple(
                        item.value.text
                        for finding in fields.findings
                        for item in finding.source_inputs
                    ),
                    ("000745", "000746", "000747"),
                )
                self.assertTrue(
                    all(
                        item.value.state is OriginalValueState.PRESENT
                        for finding in fields.findings
                        for item in finding.source_inputs
                    )
                )
                self.assertEqual(evidence.original_source_requests, ())
                with closing(load_workbook(path)) as workbook:
                    sheet = workbook["Fields set aside"]
                    rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
                    self.assertEqual(len(rows), 3)
                    self.assertEqual(tuple(row[3] for row in rows), ("Contact",) * 3)
                    self.assertEqual(
                        {row[4] for row in rows}, {"000745", "000746", "000747"}
                    )
                    self.assertEqual(
                        {row[0] for row in rows},
                        {
                            FieldSetAsideReason.UNRECOGNIZED.value,
                            FieldSetAsideReason.AMBIGUOUS.value,
                        },
                    )
                    self.assertEqual(sheet["A4"].value, "3 identified source cells")
                    self.assertEqual(sheet["G4"].value, "3 saved findings")
                    self.assertTrue(
                        all(sheet.cell(row, 5).data_type == "s" for row in (7, 8, 9))
                    )
                    self.assertFalse(any("N-0" in str(row[3:5]) for row in rows))
                    self.assertFalse(any("MATCH-" in str(row[3:5]) for row in rows))
                    self.assertFalse(any("Company A" in str(row[3:5]) for row in rows))
                    exports.append(rows)
            self.assertEqual(exports[0], exports[1])
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
