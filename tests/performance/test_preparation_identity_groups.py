"""Qualify generic document groups through preparation and normalization."""

from __future__ import annotations

from dataclasses import replace
import os
from time import perf_counter
import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb import preparation_identity_group_quality as group_adapter
from impodo.adapters.duckdb.preparation_identity_group_quality import _materialize_unsafe_group_rows
from impodo.application.workspace.preparation import (
    normalization_service as normalization_module,
    preparation_capability as capability_module,
    quality_service as quality_module,
)
from impodo.application.workspace.preparation.bounded_quality import BoundedQualityUnsupported
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    IdentityComponentMapping, ReferenceKeyMapping, RelationshipMapping,
    RelationshipResolver, ResolverOrigin, ValueMapping,
)
from impodo.domain.preparation.quality import evaluate_quality
from tests.performance import test_preparation_scale as fixtures


class PreparationIdentityGroupTests(unittest.TestCase):
    """Exercise stored groups without changing any running project or reader."""

    setUp = fixtures.BoundedPreparationParityTests.setUp
    tearDown = fixtures.BoundedPreparationParityTests.tearDown

    def _check_group_preparation(self, parent_count, child_count, target_models,
                                 hybrid_native_lookup=False, clean_children=False,
                                 duplicate_target_identity=False):
        dataset_mapping = fixtures.DatasetMapping
        source_values = fixtures._related_bom_values

        def generic_mapping(**kwargs):
            relationships = kwargs.get("relationships", ())
            if relationships:
                relation = replace(relationships[0], target_field="x_document")
                kwargs.update(
                    target_model=target_models[1], relationships=(relation,),
                    target_scope=(IdentityComponentMapping(
                        source_column_keys=relation.source_column_keys,
                        target_fields=(relation.target_field,), resolver=relation.resolver,
                    ),),
                )
                if duplicate_target_identity:
                    kwargs["target_identity"] = (IdentityComponentMapping(
                        source_column_keys=(kwargs["source_identity_column_keys"][0],),
                        target_fields=("x_bom_reference",),
                    ),)
            else:
                kwargs["target_model"] = target_models[0]
                if hybrid_native_lookup:
                    source_key = kwargs["source_identity_column_keys"][0]
                    kwargs["relationships"] = (RelationshipMapping(
                        target_field="x_lookup", kind="many2one", source_column_keys=(source_key,),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_THEN_DATASET, dataset_id=kwargs["dataset_id"],
                            model=target_models[0], key_mappings=(ReferenceKeyMapping(source_key, "default_code"),),
                            value_mappings=(ValueMapping("P000001", "ONLY_IN_ODOO"),),
                        ),
                    ),)
            return dataset_mapping(**kwargs)

        def dirty_child(index, *args, **kwargs):
            values = source_values(index, *args, **kwargs)
            return (*values[:3], "invalid", *values[4:]) if index == 0 else values

        with (
            patch.object(fixtures, "DatasetMapping", generic_mapping),
            patch.object(fixtures, "_related_bom_values",
                         source_values if clean_children else dirty_child),
        ):
            workspace_id, _, _ = (
                fixtures.PreparationWorkflowScaleTests._prepare_related_product_bom_project_and_evidence(
                    self, product_count=parent_count, bom_line_count=child_count,
                    column_count=5, mapped_field_count=5, dataset_names=("documents", "entries"),
                )
            )
        service = self.context.preparation
        workspace = service.workspaces.get(workspace_id)
        revision = service.mappings.get_mapping_revision(workspace_id)
        physical = service.sources.get_source_selection(workspace_id)
        effective = service.sources.get_mapping_source_selection(workspace_id)
        assert revision is not None and physical is not None and effective is not None
        # The isolated oracle may materialize this synthetic fixture. The
        # service under test must retain its real limits and never call it.
        with (
            patch.object(fixtures.preparation_module, "require_supported_browser_scale"),
            patch("impodo.domain.staging.evaluator.require_supported_browser_scale"),
        ):
            expected = fixtures.preparation_module.stage_browser_mapping(
                workspace, revision.definition, physical, effective, None,
                service.sources.get_source_catalogs(workspace_id), self.artifacts,
            )
        quality_builder = quality_module.build_bounded_quality_run

        def indexed_quality_only(**kwargs):
            with patch.object(
                type(kwargs["staging"].rows), "__iter__",
                side_effect=AssertionError("quality reconstructed complete canonical rows"),
            ):
                return quality_builder(**kwargs)

        started = perf_counter()
        native_dependency_sql = group_adapter.projected_hybrid_dependency_rows_sql
        native_dependency_calls = []

        def native_dependencies(projection):
            native_dependency_calls.append(projection.dataset)
            return native_dependency_sql(projection)

        with (
            patch.object(group_adapter, "projected_hybrid_dependency_rows_sql", native_dependencies),
            patch.object(
                fixtures.preparation_module,
                "stage_browser_mapping",
                side_effect=AssertionError("whole-run transformation fallback"),
            ),
            patch.object(quality_module, "build_bounded_quality_run", indexed_quality_only),
            patch.object(quality_module, "evaluate_quality", side_effect=AssertionError("whole-run quality fallback")),
            patch.object(normalization_module, "evaluate_normalization", side_effect=AssertionError("whole-run normalization fallback")),
            patch.object(capability_module, "MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT", 1),
        ):
            with (
                patch.object(quality_module, "build_bounded_quality_run", side_effect=BoundedQualityUnsupported),
                self.assertRaisesRegex(ReadinessError, "Whole-run fallback is disabled"),
            ):
                service.prepare(workspace_id, actor=self.context.actor)
            self.assertIsNone(service.quality.current_summary(workspace_id))
            result = service.prepare(workspace_id, actor=self.context.actor)
            repeated = service.prepare(workspace_id, actor=self.context.actor)
        elapsed = perf_counter() - started

        staging = service.staging.get_current_staging_summary(workspace_id)
        ruleset = service.quality.current_ruleset(workspace_id)
        actual_quality = service.quality.current_run(workspace_id)
        assert staging is not None and ruleset is not None and actual_quality is not None
        expected_canonical = expected.canonical_run
        self.assertEqual(staging.content_hash, expected_canonical.content_hash)
        if hybrid_native_lookup or clean_children:
            actual = service.staging.get_canonical_staging_run(workspace_id, staging.run_id)
            assert actual is not None
            self.assertEqual(actual.to_portable_dict(include_hash=False),
                             expected_canonical.to_portable_dict(include_hash=False))
            self.assertGreater(len(native_dependency_calls), 0)
            if clean_children:
                database_path = service.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
                with duckdb.connect(str(database_path), read_only=True) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM canonical_staging_row WHERE run_id = ? "
                        "AND dataset = 'entries' AND row_json = ''",
                        [staging.run_id],
                    ).fetchone()[0], child_count)
        expected_quality = evaluate_quality(
            workspace_state=workspace, staging=expected_canonical,
            physical_rows=dict(expected.physical_rows), ruleset=ruleset,
            published_staging_content_hash=staging.content_hash,
        )
        self.assertEqual(actual_quality.to_json(), expected_quality.to_json())
        self.assertEqual(result.content_hash, repeated.content_hash)
        self.assertEqual(service.mappings.get_mapping_revision(workspace_id), revision)
        self.assertEqual(service.sources.get_source_selection(workspace_id), physical)
        expected_quarantined = expected_quality.quarantined_count if clean_children else (
            1 + (child_count - 1) // parent_count + 1
        )
        if hybrid_native_lookup and not clean_children:
            expected_quarantined += 1 + (child_count - 2) // parent_count + 1
        self.assertEqual(actual_quality.quarantined_count, expected_quarantined)
        if duplicate_target_identity:
            self.assertGreater(actual_quality.quarantined_count, 0)
        self.assertEqual(len(actual_quality.row_results), parent_count + child_count)
        if parent_count + child_count > 25_000:
            print(f"Generic identity-group qualification: {parent_count + child_count:,} rows; "
                  f"failure and two complete preparations in {elapsed:.3f}s")

    def test_document_models_use_the_complete_route(self):
        self._check_group_preparation(3, 8, ("sale.order", "sale.order.line"))

    def test_accounting_models_use_the_same_complete_route(self):
        self._check_group_preparation(3, 8, ("account.move", "account.move.line"))

    def test_clean_custom_model_identity_groups_use_set_based_projection(self):
        self._check_group_preparation(3, 8, ("x_custom.parent", "x_custom.child"),
                                      clean_children=True)

    def test_clean_colliding_child_identity_propagates_to_native_parent_group(self):
        self._check_group_preparation(3, 8, ("x_custom.parent", "x_custom.child"),
                                      clean_children=True, duplicate_target_identity=True)

    def test_native_hybrid_lookup_rebuilds_canonical_dependency_keys(self):
        self._check_group_preparation(3, 8, ("x_custom.document", "x_custom.entry"),
                                      hybrid_native_lookup=True)

    @unittest.skipUnless(os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1", "opt-in preparation scale qualification")
    def test_custom_document_groups_above_materialized_limit(self):
        self._check_group_preparation(1_000, 25_001, ("x_custom.document", "x_custom.entry"))

    @unittest.skipUnless(os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1", "opt-in preparation scale qualification")
    def test_clean_custom_document_groups_above_materialized_limit(self):
        self._check_group_preparation(1_000, 25_001,
                                      ("x_custom.document", "x_custom.entry"),
                                      clean_children=True)


@unittest.skipUnless(os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1", "opt-in preparation scale qualification")
class GroupPropagationScaleTests(unittest.TestCase):
    def test_fifty_thousand_level_group_converges_without_python_steps_per_level(self):
        with duckdb.connect() as connection:
            connection.execute("SET threads=1")
            connection.execute("SET memory_limit='192MB'")
            connection.execute("""
                CREATE TEMP TABLE canonical_staging_row AS
                SELECT i AS ordinal, CAST(i AS VARCHAR) AS row_id, 'session' AS run_id
                  FROM range(50000) AS rows(i)
            """)
            connection.execute("CREATE TEMP TABLE group_propagating AS SELECT ordinal FROM canonical_staging_row")
            connection.execute("CREATE TEMP TABLE group_match (child BIGINT, match_count BIGINT)")
            connection.execute("""
                CREATE TEMP TABLE group_arc AS
                SELECT i AS source, i+1 AS destination FROM range(49999) AS rows(i)
                UNION ALL
                SELECT i+1, i FROM range(49999) AS rows(i)
            """)
            started = perf_counter()
            _materialize_unsafe_group_rows(connection, "session", ("0",))
            self.assertEqual(connection.execute("SELECT COUNT(*), COUNT(DISTINCT ordinal) FROM group_unsafe").fetchone(),
                             (50_000, 50_000))
            print(f"50,000-level group closure under 192MB DuckDB budget: {perf_counter() - started:.3f}s")
