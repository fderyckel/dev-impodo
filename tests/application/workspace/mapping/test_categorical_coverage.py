from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

import polars as pl

from impodo.domain.shared.access import ActorIdentity
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.mapping.control_expectations import EditionControlExpectation
from impodo.domain.mapping.validation.evidence import (
    CategoricalCoverageEvidence,
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class _Sources:
    def __init__(self, selection: SourceSelection) -> None:
        self.selection = selection
        self.snapshot = SimpleNamespace(
            dataset_id=selection.datasets[0].dataset_id,
            logical_hash="sha256:" + "4" * 64,
            parquet_sha256="5" * 64,
        )

    def get_source_selection(self, _project_id: str) -> SourceSelection:
        return self.selection

    def get_mapping_source_selection(self, _project_id: str) -> SourceSelection:
        return self.selection

    def get_current_source_snapshots(self, _project_id: str):
        return (self.snapshot,)


class _RecordingCoverageService(CategoricalCoverageService):
    def __init__(self, sources: _Sources, frame: pl.DataFrame) -> None:
        super().__init__(sources, object())
        self.frame = frame
        self.scan_calls: list[tuple[str, tuple[str, ...]]] = []

    def _scan_dataset(
        self,
        project_id,
        selection,
        dataset_id,
        source_column_keys,
    ) -> pl.DataFrame:
        self.scan_calls.append((dataset_id, tuple(source_column_keys)))
        return self.frame.select(source_column_keys)


class CategoricalCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = "11111111-1111-4111-8111-111111111111"
        binding = FileSourceBinding(
            file_id="file:customers",
            table_key="csv",
            source_sha256="1" * 64,
            catalog_hash="sha256:" + "2" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        )
        self.selection = SourceSelection(
            selection_id="selection:customers",
            version=1,
            data_version_id="22222222-2222-4222-8222-222222222222",
            created_at=NOW,
            created_by="manager",
            datasets=(
                SourceDataset(
                    dataset_id="dataset:customers",
                    name="Customers",
                    source=binding,
                    row_count=3,
                    columns=(
                        SourceDatasetColumn(1, "language", "language", "string"),
                        SourceDatasetColumn(2, "country", "country", "string"),
                    ),
                ),
            ),
            content_hash="sha256:" + "3" * 64,
        )
        self.schema = OdooSchemaCatalog(
            workspace_id=self.workspace_id,
            policy_hash="sha256:" + "6" * 64,
            captured_at=NOW,
            captured_by="manager",
            connection_mode="LOCAL",
            database="odoo19",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="res.partner",
                    label="Contact",
                    fields=(
                        SchemaField(
                            name="lang",
                            label="Language",
                            type="selection",
                            required=False,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(("en", "English"), ("de", "German")),
                        ),
                        SchemaField(
                            name="country_id",
                            label="Country",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="res.country",
                            relation_field=None,
                            selection=(),
                        ),
                    ),
                ),
                SchemaModel(
                    name="res.country",
                    label="Country",
                    fields=(
                        SchemaField(
                            name="code",
                            label="Code",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "7" * 64,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash="sha256:" + "8" * 64,
            read_principal_hash="sha256:" + "9" * 64,
            read_permission_hash="sha256:" + "a" * 64,
            read_context_hash="sha256:" + "b" * 64,
            connection_target_hash="sha256:" + "c" * 64,
        )
        self.definition = MappingDefinition(
            mapping_id="mapping:customers",
            source_selection_hash=self.selection.content_hash,
            schema_hash=self.schema.content_hash,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:customers",
                    target_model="res.partner",
                    fields=(
                        ScalarFieldMapping(
                            target_field="lang",
                            source_column_key="language",
                            value_mappings=(ValueMapping("English", "en"),),
                            categorical_policy=(
                                CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH
                            ),
                        ),
                    ),
                    relationships=(
                        RelationshipMapping(
                            target_field="country_id",
                            kind="many2one",
                            source_column_keys=("country",),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.TARGET_CATALOG,
                                model="res.country",
                                key_mappings=(
                                    ReferenceKeyMapping("country", "code"),
                                ),
                                value_mappings=(ValueMapping("LUX", "LU"),),
                            ),
                            categorical_policy=(
                                CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH
                            ),
                        ),
                    ),
                ),
            ),
        )

    def test_collect_scans_dataset_once_and_hashes_incomplete_coverage(self) -> None:
        service = _RecordingCoverageService(
            _Sources(self.selection),
            pl.DataFrame(
                {
                    "language": ["English", " German ", "English"],
                    "country": ["LUX", "DE", "LUX"],
                }
            ),
        )

        collected = service.collect(
            self.workspace_id,
            self.definition,
            self.selection,
            self.schema,
        )

        self.assertEqual(
            service.scan_calls,
            [("dataset:customers", ("country", "language"))],
        )
        self.assertEqual(
            [item.code for item in collected.issues],
            [
                "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE",
                "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE",
            ],
        )
        uncovered = {
            item.target_field: item.uncovered_values
            for item in collected.evidence.field_results
        }
        self.assertEqual(uncovered["lang"], (("German",),))
        self.assertEqual(uncovered["country_id"], (("DE",),))
        restored = CategoricalCoverageEvidence.from_dict(
            collected.evidence.to_dict()
        )
        self.assertEqual(restored, collected.evidence)
        with self.assertRaisesRegex(ValueError, "duplicated"):
            replace(
                collected.evidence,
                field_results=(
                    collected.evidence.field_results[0],
                    collected.evidence.field_results[0],
                ),
            )

        base = MappingValidationResult(
            mapping_content_hash=self.definition.content_hash,
            source_selection_hash=self.selection.content_hash,
            schema_hash=self.schema.content_hash,
            status=MappingValidationStatus.INVALID,
            issues=collected.issues,
            coverage=(),
            deferred_runtime_checks=(),
        )
        bound = replace(base, categorical_coverage=collected.evidence)
        self.assertNotEqual(base.validation_hash, bound.validation_hash)
        self.assertEqual(MappingValidationResult.from_json(bound.to_json()), bound)

    def test_collect_without_categorical_rules_needs_no_physical_snapshot(self) -> None:
        definition = replace(
            self.definition,
            datasets=(
                replace(
                    self.definition.datasets[0],
                    fields=(
                        ScalarFieldMapping(
                            target_field="lang",
                            source_column_key="language",
                        ),
                    ),
                    relationships=(),
                ),
            ),
        )

        def unexpected_read(_workspace_id):
            self.fail("categorical-free mappings must not read physical snapshots")

        service = CategoricalCoverageService(
            SimpleNamespace(
                get_source_selection=unexpected_read,
                get_current_source_snapshots=unexpected_read,
            ),
            object(),
        )

        collected = service.collect(
            self.workspace_id,
            definition,
            self.selection,
            self.schema,
        )

        self.assertEqual(collected.issues, ())
        self.assertEqual(collected.evidence.source_snapshot_hashes, ())
        self.assertEqual(collected.evidence.field_results, ())

    def test_target_first_relationship_uses_incoming_fallback_for_unmatched_choices(
        self,
    ) -> None:
        relationship = self.definition.datasets[0].relationships[0]
        target_first = replace(
            self.definition,
            datasets=(
                replace(
                    self.definition.datasets[0],
                    fields=(),
                    relationships=(
                        replace(
                            relationship,
                            resolver=replace(
                                relationship.resolver,
                                origin=ResolverOrigin.TARGET_THEN_DATASET,
                                dataset_id="dataset:countries",
                            ),
                        ),
                    ),
                ),
            ),
        )
        service = _RecordingCoverageService(
            _Sources(self.selection),
            pl.DataFrame(
                {
                    "language": ["English", "German", "English"],
                    "country": ["LUX", "DE", "PCE"],
                }
            ),
        )

        collected = service.collect(
            self.workspace_id,
            target_first,
            self.selection,
            self.schema,
        )

        self.assertEqual(service.scan_calls, [("dataset:customers", ("country",))])
        self.assertEqual(collected.issues, ())
        self.assertEqual(
            collected.evidence.field_results[0].uncovered_values,
            (),
        )

    def test_exact_target_coverage_uses_runtime_transformation_semantics(self) -> None:
        partner_model = self.schema.models[0]
        language = partner_model.fields[0]
        schema = replace(
            self.schema,
            models=(
                replace(
                    partner_model,
                    fields=(
                        replace(
                            language,
                            selection=(("EN", "English"), ("DE", "German")),
                        ),
                        partner_model.fields[1],
                    ),
                ),
                self.schema.models[1],
            ),
        )
        exact = replace(
            self.definition,
            schema_hash=schema.content_hash,
            datasets=(
                replace(
                    self.definition.datasets[0],
                    fields=(
                        ScalarFieldMapping(
                            target_field="lang",
                            source_column_key="language",
                            transform=ScalarTransformPolicy(
                                trim=True,
                                case_mode="uppercase",
                            ),
                            categorical_policy=(
                                CategoricalCoveragePolicy.EXACT_TARGET_VALUE
                            ),
                        ),
                    ),
                    relationships=(),
                ),
            ),
        )
        service = _RecordingCoverageService(
            _Sources(self.selection),
            pl.DataFrame(
                {
                    "language": [" en ", "de", " fr "],
                    "country": ["LU", "DE", "FR"],
                }
            ),
        )

        collected = service.collect(
            self.workspace_id,
            exact,
            self.selection,
            schema,
        )

        self.assertEqual(
            collected.evidence.field_results[0].uncovered_values,
            ((" fr ",),),
        )
        self.assertEqual(service.scan_calls, [("dataset:customers", ("language",))])

    def test_current_parser_rejects_unknown_nested_fields(self) -> None:
        payload = self.definition.to_dict()
        payload["datasets"][0]["fields"][0]["unknown_field"] = True
        with self.assertRaisesRegex(ValueError, "current contract"):
            MappingDefinition.from_dict(payload)

        payload = self.definition.to_dict()
        payload["datasets"][0]["relationships"][0]["resolver"][
            "unknown_field"
        ] = True
        with self.assertRaisesRegex(ValueError, "current contract"):
            MappingDefinition.from_dict(payload)

    def test_edition_control_expectation_binds_actor_and_fresh_value(self) -> None:
        expectation = EditionControlExpectation(
            workspace_id="f6e1b16e-e78d-42a9-a430-5f361acdc388",
            logical_control_id="control:customers.open_balance",
            expected_value="5100000.00",
            source="OPERATOR_ENTERED",
            reason="Confirmed for the August replacement export",
            actor=ActorIdentity(
                issuer="urn:impodo:test",
                subject_id="manager-1",
                display_name="Data Manager",
            ),
            recorded_at=NOW,
        )

        restored = EditionControlExpectation.from_dict(expectation.to_dict())

        self.assertEqual(restored, expectation)
        self.assertEqual(restored.expected_value, "5100000.00")


if __name__ == "__main__":
    unittest.main()
