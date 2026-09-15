from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from impodo.application.workspace.preparation.preparation_capability import (
    PreparationRouteBehavior,
    compile_preparation_capability,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping, IdentityComponentMapping, MappingDefinition, MappingTargetMode,
    ReferenceKeyMapping, RelationshipMapping, RelationshipResolver, ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.application.workspace.preparation import preparation_capability as capability_module
from impodo.application.workspace.preparation import bounded_preparation as bounded_module
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
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
    def test_dataset_routes_share_one_compilation_and_explain_mixed_storage(self) -> None:
        selection = _selection((3, 8), names=("documents", "details"))
        definition = _definition(selection)
        child = replace(definition.datasets[1], fields=(ScalarFieldMapping(
            target_field="x_amount", source_column_key="column:1", value_type="decimal",
            transform=ScalarTransformPolicy(formula="value / 1000"),
        ),))
        definition = replace(definition, datasets=(definition.datasets[0], child))
        snapshots = (SimpleNamespace(dataset_id=item.dataset_id) for item in selection.datasets)
        with (
            patch.object(capability_module, "compile_columnar_transformation_programs", wraps=capability_module.compile_columnar_transformation_programs) as compiler,
            patch.object(capability_module, "validate_snapshot_for_dataset"),
            patch.object(bounded_module, "validate_snapshot_for_dataset"),
        ):
            manifest = compile_preparation_capability(
                definition=definition, physical_selection=selection, effective_selection=selection,
                source_snapshots=snapshots, derived_plan=None, current_ruleset=None, reference_bundle=None,
            )
        self.assertEqual(compiler.call_count, 1)
        self.assertEqual(tuple(item.behavior for item in manifest.datasets), (
            PreparationRouteBehavior.NATIVE_COLUMNAR, PreparationRouteBehavior.BOUNDED_PYTHON,
        ))
        transformation = next(item for item in manifest.stages if item.stage == "transformation")
        self.assertEqual(transformation.behavior, PreparationRouteBehavior.MIXED_BOUNDED)
        self.assertEqual(manifest.supported_rows, 50_000)

    def test_incoming_identity_uses_direct_limit_and_retains_small_run_fallback(self) -> None:
        for row_count in (25_000, 25_001, 50_000, 50_001):
            selection = _selection((row_count,))
            definition = _definition(selection)
            dataset = replace(definition.datasets[0], target_scope=(IdentityComponentMapping(
                source_column_keys=("column:1",), target_fields=("ancestor",),
                resolver=RelationshipResolver(origin=ResolverOrigin.DATASET, dataset_id=selection.datasets[0].dataset_id),
            ),))
            manifest = _manifest(replace(definition, datasets=(dataset,)), selection)
            self.assertEqual(manifest.admitted, row_count <= 50_000)
            self.assertEqual(manifest.permits_materialized_fallback, row_count == 25_000)

    def test_incoming_identity_limit_is_generic_and_checked_before_rows(self) -> None:
        for model, role in (
            ("sale.order.line", "target_identity"),
            ("account.move.line", "target_scope"),
            ("x_custom.child", "target_scope"),
        ):
            with self.subTest(model=model, role=role):
                selection = _selection((2_000, 24_000), names=("parents", "children"))
                definition = _definition(selection)
                child = definition.datasets[1]
                component = IdentityComponentMapping(
                    source_column_keys=("column:1",), target_fields=("x_parent",),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.DATASET,
                        dataset_id=selection.datasets[0].dataset_id,
                    ),
                )
                child = replace(child, target_model=model, **{role: (component,)})
                definition = replace(definition, datasets=(definition.datasets[0], child))
                manifest = _manifest(definition, selection)

                self.assertTrue(manifest.admitted)
                self.assertEqual(manifest.supported_rows, 50_000)
                quality = next(item for item in manifest.stages if item.stage == "quality")
                self.assertNotIn("INCOMING_IDENTITY_GROUP_MATERIALIZES", quality.reason_codes)
                self.assertEqual(manifest.datasets[1].incoming_identity_fields, ("x_parent",))
                self.assertFalse(manifest.permits_materialized_fallback)
                manifest.require_supported()

    def test_ordinary_incoming_link_does_not_select_identity_group_limit(self) -> None:
        selection = _selection((2_000, 24_000), names=("parents", "children"))
        definition = _definition(selection)
        child = replace(definition.datasets[1], relationships=(RelationshipMapping(
            target_field="x_parent", kind="many2one", source_column_keys=("column:1",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET, dataset_id=selection.datasets[0].dataset_id,
            ),
        ),))
        manifest = _manifest(replace(definition, datasets=(definition.datasets[0], child)), selection)
        self.assertTrue(manifest.admitted)
        self.assertEqual(manifest.supported_rows, 50_000)

    def test_target_catalog_identity_keeps_bounded_route(self) -> None:
        selection = _selection((26_000,))
        definition = _definition(selection)
        dataset = replace(definition.datasets[0], target_scope=(IdentityComponentMapping(
            source_column_keys=("column:1",), target_fields=("x_parent",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG, model="x_parent",
                key_mappings=(ReferenceKeyMapping("column:1", "code"),),
            ),
        ),))
        manifest = _manifest(replace(definition, datasets=(dataset,)), selection)
        self.assertTrue(manifest.admitted)

    def test_formula_route_reports_its_dataset_and_rule_without_values(self) -> None:
        selection = _selection((3, 8), names=("documents", "details"))
        definition = _definition(selection)
        child = replace(definition.datasets[1], fields=(ScalarFieldMapping(
            target_field="x_amount", source_column_key="column:1", value_type="decimal",
            transform=ScalarTransformPolicy(formula="value / 1000"),
        ),))
        manifest = _manifest(replace(definition, datasets=(definition.datasets[0], child)), selection)
        details = next(item for item in manifest.datasets if item.dataset_name == "details")
        self.assertEqual(details.behavior, PreparationRouteBehavior.BOUNDED_PYTHON)
        self.assertIn("COLUMNAR_FORMULA_UNSUPPORTED", details.reason_codes)
        self.assertIn("/fields/x_amount/transform/formula", details.rule_paths)
        self.assertNotIn("value / 1000", str(manifest.to_portable_dict()))

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
) -> MappingDefinition:
    return MappingDefinition(
        mapping_id="mapping:capability",
        schema_hash="sha256:" + "d" * 64,
        source_selection_hash=selection.content_hash,
        datasets=tuple(
            DatasetMapping(
                dataset_id=dataset.dataset_id, target_model="x_record",
                mode=MappingTargetMode.UPSERT,
                source_identity_column_keys=("column:1",),
                target_identity=(IdentityComponentMapping(("column:1",), ("code",)),),
                fields=(ScalarFieldMapping("code", "column:1"),),
                relationships=((RelationshipMapping(
                    target_field="parent_id", kind="many2one", source_column_keys=("column:1",),
                    resolver=RelationshipResolver(origin=ResolverOrigin.DATASET, dataset_id=dataset.dataset_id),
                ),) if related and index == 0 else ()),
            )
            for index, dataset in enumerate(selection.datasets)
        ),
    )


def _manifest(definition: MappingDefinition, selection: SourceSelection):
    return compile_preparation_capability(
        definition=definition, physical_selection=selection, effective_selection=selection,
        source_snapshots=(), derived_plan=None, current_ruleset=None, reference_bundle=None,
    )
