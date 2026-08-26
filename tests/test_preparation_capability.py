from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from impodo.application.workspace.preparation.preparation_capability import (
    PreparationRouteBehavior,
    compile_preparation_capability,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


class PreparationCapabilityTests(unittest.TestCase):
    def test_single_native_dataset_admits_the_existing_columnar_limit(self) -> None:
        selection = _selection((100_000,))

        with patch(
            "impodo.application.workspace.preparation.preparation_capability."
            "direct_preparation_row_limit",
            return_value=COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        ):
            manifest = compile_preparation_capability(
                definition=_definition(selection),
                physical_selection=selection,
                effective_selection=selection,
                source_snapshots=(),
                derived_plan=None,
                current_ruleset=None,
                reference_bundle=None,
            )

        self.assertTrue(manifest.admitted)
        self.assertEqual(
            manifest.supported_rows,
            COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )
        routes = {item.stage: item.behavior for item in manifest.stages}
        self.assertEqual(
            routes["transformation"],
            PreparationRouteBehavior.NATIVE_COLUMNAR,
        )
        self.assertEqual(
            routes["quality"],
            PreparationRouteBehavior.BOUNDED_RUNTIME_GUARDED,
        )

    def test_unqualified_high_volume_multi_dataset_run_is_not_admitted(self) -> None:
        selection = _selection(
            (16_000, 80_000),
            names=("products", "bom_lines"),
        )

        with patch(
            "impodo.application.workspace.preparation.preparation_capability."
            "direct_preparation_row_limit",
            return_value=COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        ):
            manifest = compile_preparation_capability(
                definition=_definition(selection),
                physical_selection=selection,
                effective_selection=selection,
                source_snapshots=(),
                derived_plan=None,
                current_ruleset=None,
                reference_bundle=None,
            )

        self.assertFalse(manifest.admitted)
        self.assertFalse(manifest.permits_materialized_fallback)
        self.assertEqual(
            manifest.supported_rows,
            BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )
        quality = next(
            item for item in manifest.stages if item.stage == "quality"
        )
        self.assertEqual(
            quality.behavior,
            PreparationRouteBehavior.BOUNDED_RUNTIME_GUARDED,
        )
        relationships = next(
            item for item in manifest.stages if item.stage == "relationships"
        )
        self.assertEqual(
            relationships.reason_codes,
            ("MULTI_DATASET_OR_RELATIONSHIP_SCALE_UNQUALIFIED",),
        )
        with self.assertRaisesRegex(
            ReadinessError,
            "safely check up to 50,000 rows",
        ):
            manifest.require_supported()

    def test_multi_dataset_direct_run_is_admitted_inside_proven_boundary(self) -> None:
        selection = _selection(
            (10_000, 40_000),
            names=("products", "bom_lines"),
        )

        with patch(
            "impodo.application.workspace.preparation.preparation_capability."
            "direct_preparation_row_limit",
            return_value=COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        ):
            manifest = compile_preparation_capability(
                definition=_definition(selection),
                physical_selection=selection,
                effective_selection=selection,
                source_snapshots=(),
                derived_plan=None,
                current_ruleset=None,
                reference_bundle=None,
            )

        self.assertTrue(manifest.admitted)
        self.assertEqual(
            manifest.supported_rows,
            BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )
        manifest.require_supported()

    def test_single_dataset_relationship_uses_unqualified_boundary(self) -> None:
        selection = _selection((60_000,))

        with patch(
            "impodo.application.workspace.preparation.preparation_capability."
            "direct_preparation_row_limit",
            return_value=COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        ):
            manifest = compile_preparation_capability(
                definition=_definition(selection, related=True),
                physical_selection=selection,
                effective_selection=selection,
                source_snapshots=(),
                derived_plan=None,
                current_ruleset=None,
                reference_bundle=None,
            )

        self.assertFalse(manifest.admitted)
        self.assertEqual(
            manifest.supported_rows,
            BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        )

    def test_current_advanced_ruleset_selects_the_truthful_lower_route(self) -> None:
        selection = _selection((30_000,))
        definition = _definition(selection)
        advanced_ruleset = SimpleNamespace(
            mapping_hash=definition.content_hash,
            schema_hash=definition.schema_hash,
            reference_bundle_hash=None,
            rules=(),
        )

        with patch(
            "impodo.application.workspace.preparation.preparation_capability."
            "direct_preparation_row_limit",
            return_value=COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
        ):
            manifest = compile_preparation_capability(
                definition=definition,
                physical_selection=selection,
                effective_selection=selection,
                source_snapshots=(),
                derived_plan=None,
                current_ruleset=advanced_ruleset,
                reference_bundle=None,
            )

        self.assertFalse(manifest.admitted)
        quality = next(
            item for item in manifest.stages if item.stage == "quality"
        )
        self.assertEqual(
            quality.reason_codes,
            ("ADVANCED_QUALITY_RULES_MATERIALIZE",),
        )


def _selection(
    row_counts: tuple[int, ...],
    *,
    names: tuple[str, ...] | None = None,
) -> SourceSelection:
    dataset_names = names or tuple(
        f"dataset_{index}" for index in range(1, len(row_counts) + 1)
    )
    if len(dataset_names) != len(row_counts):
        raise ValueError("Every fixture dataset requires one name")
    datasets = tuple(
        SourceDataset(
            dataset_id=f"dataset:{index}",
            name=dataset_names[index - 1],
            source=FileSourceBinding(
                file_id=f"file:{index}",
                table_key="csv",
                source_sha256="sha256:" + str(index) * 64,
                catalog_hash="sha256:" + "a" * 64,
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            ),
            row_count=row_count,
            columns=(
                SourceDatasetColumn(
                    ordinal=1,
                    source_name="Code",
                    stable_key="column:1",
                    candidate_type="text",
                ),
            ),
        )
        for index, row_count in enumerate(row_counts, 1)
    )
    return SourceSelection(
        selection_id="selection:1",
        version=1,
        data_version_id="project:1",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_by="tester",
        datasets=datasets,
        content_hash="sha256:" + "b" * 64,
    )


def _definition(
    selection: SourceSelection,
    *,
    related: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        content_hash="sha256:" + "c" * 64,
        schema_hash="sha256:" + "d" * 64,
        source_selection_hash=selection.content_hash,
        datasets=tuple(
            SimpleNamespace(
                relationships=(("parent_id",) if related and index == 0 else ()),
            )
            for index, _dataset in enumerate(selection.datasets)
        ),
    )
