from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

import polars as pl
from starlette.datastructures import FormData

from impodo.adapters.polars_transformation import (
    _provider_expression,
    _provider_invalid_expression,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarInputColumn,
    ColumnarOperationKind,
    ColumnarSupport,
    ColumnarValueProviderProgram,
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.contracts import (
    ConcatenationBlankHandling,
    DatasetMapping,
    MappingDefinition,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueRuleError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import source_value_column
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.web.presenters.mapping_forms import _scalar_concatenation_from_form


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
DATASET_ID = "dataset:0123456789abcdef01234567"


def _configuration(
    *,
    blank_handling: ConcatenationBlankHandling = (
        ConcatenationBlankHandling.SKIP_BLANK
    ),
) -> ScalarConcatenation:
    return ScalarConcatenation(
        source_column_keys=("contact.first_name", "contact.last_name"),
        separator=" ",
        blank_handling=blank_handling,
        trim_parts=True,
    )


def _mapping(
    *,
    blank_handling: ConcatenationBlankHandling = (
        ConcatenationBlankHandling.SKIP_BLANK
    ),
) -> ScalarFieldMapping:
    return ScalarFieldMapping(
        target_field="name",
        value_source=ScalarValueSource.CONCATENATE,
        concatenation=_configuration(blank_handling=blank_handling),
    )


def _selection() -> SourceSelection:
    dataset = SourceDataset(
        dataset_id=DATASET_ID,
        name="contacts",
        source=FileSourceBinding(
            file_id="file-contacts",
            table_key="csv",
            source_sha256=HASH_A,
            catalog_hash=HASH_B,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=3,
        columns=(
            SourceDatasetColumn(1, "First name", "contact.first_name", "string"),
            SourceDatasetColumn(2, "Last name", "contact.last_name", "string"),
        ),
    )
    return SourceSelection(
        selection_id="selection-contacts",
        version=1,
        data_version_id="project-contacts",
        created_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        created_by="tester",
        datasets=(dataset,),
        content_hash=HASH_A,
    )


class ScalarConcatenationTests(unittest.TestCase):
    def test_oracle_joins_in_order_and_skips_blank_parts(self) -> None:
        mapping = _mapping()

        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={
                    "contact.first_name": "  Ada ",
                    "contact.last_name": " Mensah  ",
                },
            ),
            "Ada Mensah",
        )
        self.assertEqual(
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={
                    "contact.first_name": " Luis ",
                    "contact.last_name": "   ",
                },
            ),
            "Luis",
        )
        self.assertIsNone(
            evaluate_scalar_mapping_value(
                mapping,
                None,
                source_values_by_key={
                    "contact.first_name": None,
                    "contact.last_name": "",
                },
            )
        )

    def test_block_blank_policy_raises_stable_row_issue(self) -> None:
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _mapping(
                    blank_handling=ConcatenationBlankHandling.BLOCK_ROW,
                ),
                None,
                source_values_by_key={
                    "contact.first_name": "Ada",
                    "contact.last_name": None,
                },
            )

        self.assertEqual(
            raised.exception.code,
            "SOURCE_CONCATENATION_PART_BLANK",
        )

    def test_output_limit_and_distinct_column_bounds_fail_closed(self) -> None:
        with self.assertRaises(ScalarValueRuleError) as raised:
            evaluate_scalar_mapping_value(
                _mapping(),
                None,
                source_values_by_key={
                    "contact.first_name": "a" * 600_000,
                    "contact.last_name": "b" * 600_000,
                },
            )
        self.assertEqual(raised.exception.code, "SOURCE_RULE_OUTPUT_TOO_LONG")

        with self.assertRaises(ValueError):
            ScalarConcatenation(source_column_keys=("contact.first_name",))
        with self.assertRaises(ValueError):
            ScalarConcatenation(
                source_column_keys=(
                    "contact.first_name",
                    "contact.first_name",
                )
            )

    def test_contract_round_trip_and_legacy_version_guard(self) -> None:
        selection = _selection()
        definition = MappingDefinition(
            mapping_id="mapping-contacts",
            source_selection_hash=selection.content_hash,
            schema_hash=HASH_B,
            datasets=(
                DatasetMapping(
                    dataset_id=DATASET_ID,
                    target_model="res.partner",
                    fields=(_mapping(),),
                ),
            ),
        )

        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        reversed_definition = MappingDefinition(
            mapping_id=definition.mapping_id,
            source_selection_hash=selection.content_hash,
            schema_hash=HASH_B,
            datasets=(
                DatasetMapping(
                    dataset_id=DATASET_ID,
                    target_model="res.partner",
                    fields=(
                        ScalarFieldMapping(
                            target_field="name",
                            value_source=ScalarValueSource.CONCATENATE,
                            concatenation=ScalarConcatenation(
                                source_column_keys=tuple(
                                    reversed(
                                        _configuration().source_column_keys
                                    )
                                )
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertNotEqual(
            reversed_definition.content_hash,
            definition.content_hash,
        )
        with self.assertRaises(ValueError):
            MappingDefinition(
                mapping_id="legacy-mapping",
                source_selection_hash=selection.content_hash,
                schema_hash=HASH_B,
                datasets=definition.datasets,
                contract_version=13,
            )

    def test_columnar_compiler_projects_all_contributing_columns_natively(self) -> None:
        selection = _selection()
        definition = MappingDefinition(
            mapping_id="mapping-contacts",
            source_selection_hash=selection.content_hash,
            schema_hash=HASH_B,
            datasets=(
                DatasetMapping(
                    dataset_id=DATASET_ID,
                    target_model="res.partner",
                    fields=(_mapping(),),
                ),
            ),
        )

        decision = compile_columnar_transformation_program(
            definition,
            selection,
            DATASET_ID,
        )

        self.assertIs(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        provider = decision.program.scalar_fields[0].provider
        self.assertIs(
            provider.operation,
            ColumnarOperationKind.CONCATENATE_SOURCE_COLUMNS,
        )
        self.assertEqual(
            tuple(item.stable_key for item in provider.sources),
            ("contact.first_name", "contact.last_name"),
        )

    def test_native_provider_matches_skip_and_block_blank_semantics(self) -> None:
        sources = (
            ColumnarInputColumn("contact.first_name", 1, "First name", "string"),
            ColumnarInputColumn("contact.last_name", 2, "Last name", "string"),
        )
        provider = ColumnarValueProviderProgram(
            operation=ColumnarOperationKind.CONCATENATE_SOURCE_COLUMNS,
            source=None,
            literal_value=None,
            value_mappings=(),
            sources=sources,
            separator=" ",
            trim_parts=True,
        )
        frame = pl.DataFrame(
            {
                source_value_column(1): ["  Ada ", "Luis", None],
                source_value_column(2): [" Mensah ", " ", ""],
            },
            schema={
                source_value_column(1): pl.String,
                source_value_column(2): pl.String,
            },
        )
        field = SimpleNamespace(provider=provider)
        expression, _matched = _provider_expression(field)

        self.assertEqual(
            frame.select(expression.alias("value"))["value"].to_list(),
            ["Ada Mensah", "Luis", None],
        )
        blocking = SimpleNamespace(
            provider=ColumnarValueProviderProgram(
                operation=provider.operation,
                source=None,
                literal_value=None,
                value_mappings=(),
                sources=sources,
                separator=" ",
                blank_handling="block_row",
                trim_parts=True,
            )
        )
        self.assertEqual(
            frame.select(
                _provider_invalid_expression(blocking).alias("invalid")
            )["invalid"].to_list(),
            [False, True, True],
        )

    def test_form_reads_fixed_slots_in_order_with_custom_separator(self) -> None:
        configuration = _scalar_concatenation_from_form(
            FormData(
                (
                    ("scalar_concat_source_0_2_0", "contact.last_name"),
                    ("scalar_concat_source_0_2_1", "contact.first_name"),
                    ("scalar_concat_separator_0_2", "custom"),
                    ("scalar_concat_custom_separator_0_2", " / "),
                    ("scalar_concat_blank_0_2", "block_row"),
                    ("scalar_concat_trim_0_2", "1"),
                )
            ),
            dataset_index=0,
            field_index=2,
            source_columns={"contact.first_name", "contact.last_name"},
        )

        self.assertEqual(
            configuration.source_column_keys,
            ("contact.last_name", "contact.first_name"),
        )
        self.assertEqual(configuration.separator, " / ")
        self.assertIs(
            configuration.blank_handling,
            ConcatenationBlankHandling.BLOCK_ROW,
        )
        self.assertTrue(configuration.trim_parts)


if __name__ == "__main__":
    unittest.main()
