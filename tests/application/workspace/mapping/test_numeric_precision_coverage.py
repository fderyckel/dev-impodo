from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

import polars as pl

from impodo.application.workspace.mapping.numeric_precision_coverage import (
    NumericPrecisionCoverageService,
)
from impodo.application.workspace.mapping.service import MappingWorkspaceService
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


class _Sources:
    def __init__(self, selection: SourceSelection) -> None:
        self.selection = selection

    def get_source_selection(self, _workspace_id: str) -> SourceSelection:
        return self.selection

    def get_current_source_snapshots(self, _workspace_id: str):
        return (SimpleNamespace(dataset_id=self.selection.datasets[0].dataset_id),)


class _RecordingService(NumericPrecisionCoverageService):
    def __init__(self, sources: _Sources, frame: pl.DataFrame) -> None:
        super().__init__(sources, object())
        self.frame = frame
        self.scan_calls: list[tuple[str, tuple[str, ...]]] = []

    def _scan_dataset(
        self,
        workspace_id,
        selection,
        dataset_id,
        source_column_keys,
    ) -> pl.DataFrame:
        self.scan_calls.append((dataset_id, tuple(source_column_keys)))
        return self.frame.select(source_column_keys)


class NumericPrecisionCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = "11111111-1111-4111-8111-111111111111"
        binding = FileSourceBinding(
            file_id="file:bom-lines",
            table_key="bom-lines",
            source_sha256="1" * 64,
            catalog_hash="sha256:" + "2" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        )
        source = SourceDataset(
            dataset_id="dataset:bom-lines",
            name="BOM Lines",
            source=binding,
            row_count=4,
            columns=(
                SourceDatasetColumn(1, "Quantity", "line.quantity", "decimal"),
                SourceDatasetColumn(2, "BOM", "line.bom", "string"),
            ),
        )
        self.selection = SourceSelection(
            selection_id="selection:bom-lines",
            version=1,
            data_version_id="22222222-2222-4222-8222-222222222222",
            created_at=NOW,
            created_by="manager",
            datasets=(source,),
            content_hash="sha256:" + "3" * 64,
        )
        self.schema = OdooSchemaCatalog(
            workspace_id=self.workspace_id,
            policy_hash="sha256:" + "4" * 64,
            captured_at=NOW,
            captured_by="manager",
            connection_mode="LOCAL",
            database="odoo20",
            odoo_version="20.0",
            models=(
                SchemaModel(
                    name="mrp.bom.line",
                    label="BOM Line",
                    fields=(
                        SchemaField(
                            name="product_qty",
                            label="Product Quantity",
                            type="float",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            digits=(16, 2),
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "5" * 64,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash="sha256:" + "6" * 64,
            read_principal_hash="sha256:" + "7" * 64,
            read_permission_hash="sha256:" + "8" * 64,
            read_context_hash="sha256:" + "9" * 64,
            connection_target_hash="sha256:" + "a" * 64,
        )
        self.field = ScalarFieldMapping(
            target_field="product_qty",
            source_column_key="line.quantity",
            value_type="decimal",
        )
        self.definition = MappingDefinition(
            mapping_id="mapping:bom-lines",
            source_selection_hash=self.selection.content_hash,
            schema_hash=self.schema.content_hash,
            datasets=(
                DatasetMapping(
                    dataset_id=source.dataset_id,
                    target_model="mrp.bom.line",
                    fields=(self.field,),
                ),
            ),
        )
        self.service = _RecordingService(
            _Sources(self.selection),
            pl.DataFrame(
                {
                    "line.quantity": ["2.9747", "2.9838", "0.004", "1.20"],
                    "line.bom": ["BOM-1", "BOM-1", "BOM-2", "BOM-2"],
                }
            ),
        )

    def test_warns_in_match_data_before_later_exact_write_blocker(self) -> None:
        issues = self.service.collect(
            self.workspace_id,
            self.definition,
            self.selection,
            self.schema,
        )

        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.code, "MAPPING_TARGET_NUMERIC_PRECISION_LOSS")
        self.assertEqual(issue.severity, "warning")
        self.assertEqual(issue.dataset_id, "dataset:bom-lines")
        self.assertEqual(issue.target_field, "product_qty")
        self.assertIn("3 mapped value(s)", issue.message)
        self.assertIn("Odoo precision (16, 2)", issue.message)
        self.assertIn("at least 4 decimal places", issue.message)
        self.assertIn("1 positive value(s) into zero", issue.message)
        self.assertIn("Load into Odoo will stop", issue.message)
        self.assertIn("Round to this Odoo precision", issue.remediation)
        self.assertEqual(
            self.service.scan_calls,
            [("dataset:bom-lines", ("line.quantity",))],
        )

    def test_explicit_target_rounding_clears_the_warning(self) -> None:
        rounded = replace(
            self.definition,
            datasets=(
                replace(
                    self.definition.datasets[0],
                    fields=(
                        replace(
                            self.field,
                            transform=ScalarTransformPolicy(
                                decimal_places=2,
                                rounding_mode="half_up",
                            ),
                        ),
                    ),
                ),
            ),
        )

        issues = self.service.collect(
            self.workspace_id,
            rounded,
            self.selection,
            self.schema,
        )

        self.assertEqual(issues, ())

    def test_mapping_check_persists_the_scan_as_a_reviewable_warning(self) -> None:
        precision_issue = self.service.collect(
            self.workspace_id,
            self.definition,
            self.selection,
            self.schema,
        )[0]
        base_validation = MappingValidationResult(
            mapping_content_hash=self.definition.content_hash,
            source_selection_hash=self.selection.content_hash,
            schema_hash=self.schema.content_hash,
            status=MappingValidationStatus.VALID,
            issues=(),
            coverage=(),
            deferred_runtime_checks=(),
        )
        service = MappingWorkspaceService(
            sources=SimpleNamespace(),
            schemas=SimpleNamespace(),
            mappings=SimpleNamespace(),
            authorization=SimpleNamespace(),
            categorical_coverage=SimpleNamespace(
                collect=lambda *_args: SimpleNamespace(
                    issues=(),
                    evidence=None,
                )
            ),
            numeric_precision_coverage=SimpleNamespace(
                collect=lambda *_args: (precision_issue,)
            ),
        )
        service.validator = SimpleNamespace(
            validate=lambda *_args: base_validation
        )

        checked = service._validate_definition(
            self.workspace_id,
            self.definition,
            self.selection,
            self.schema,
            None,
        )

        self.assertEqual(checked.status, MappingValidationStatus.VALID_WITH_WARNINGS)
        self.assertEqual(checked.issues, (precision_issue,))


if __name__ == "__main__":
    unittest.main()
