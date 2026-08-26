from __future__ import annotations

from collections.abc import Callable
import csv
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter, process_time, sleep
import tempfile
from threading import Event, Thread
import tracemalloc
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid5

from impodo.application import normalization_service as normalization_module
from impodo.application import bounded_normalization as bounded_normalization_module
from impodo.application import bounded_preparation as bounded_preparation_module
from impodo.application import preparation_service as preparation_module
from impodo.application import quality_service as quality_module
from impodo.adapters.polars_transformation import PolarsTransformationAdapter
from impodo.application.bounded_preparation import BOUNDED_SOURCE_BATCH_SIZE
from impodo.application.preparation_capability import (
    compile_preparation_capability,
)
from impodo.domain.coverage import (
    CoverageApplicability,
    CoverageDeclaration,
    CoverageFamily,
    CoverageScopeRevision,
    ReferenceBundle,
    ReferenceDataSet,
    ReferenceEntry,
    ReferenceValueKind,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.artifacts import MappingRevision, MappingSubmission
from impodo.domain.mapping.contracts import (
    BusinessControlDefinition,
    DatasetMapping,
    IdentityComponentMapping,
    MappingControlExpectation,
    MappingDefinition,
    MappingTargetMode,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.resolution import (
    FuzzyComparisonField,
    ResolutionPolicy,
    ResolutionRule,
    SimilarityAlgorithm,
)
from impodo.domain.staging.scale import (
    COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
)
from impodo.domain.staging import evaluator as evaluator_module
from impodo.domain.staging import canonical_projection as canonical_projection_module
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.intake import CHUNK_BYTES, MAX_SOURCE_BYTES
from impodo.workspace_state import (
    OdooConnectionMode,
    WorkspaceStatus,
    SourceFile,
)

from impodo import source as source_module
from impodo.normalization import (
    NormalizationCandidate,
    evaluate_normalization,
)
from impodo.quality import (
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRule,
    QualityRuleFamily,
    QualityRuleSet,
    QualityRuleSource,
    default_quality_ruleset,
    evaluate_quality,
)
from impodo.preparation_jobs import PreparationJobStatus, PreparationWorkspace
from impodo.value_rules import ScalarTransformPolicy
from impodo.web.app import create_local_app
from impodo.workspace_contracts import MappingWorkingDraft


ROOT = Path(__file__).resolve().parents[1]
COLUMNAR_TRANSFORMATIONS = PolarsTransformationAdapter()
PREPARATION_SCALE_ROWS = int(os.environ.get("IMPODO_PREPARATION_SCALE_ROWS", "100000"))
PREPARATION_SCALE_COLUMNS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_COLUMNS", "30")
)
PREPARATION_SCALE_MAPPED_FIELDS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_MAPPED_FIELDS", "20")
)
PREPARATION_SCALE_EFFECT_FIELDS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_EFFECT_FIELDS", "1")
)
PREPARATION_SCALE_WORKLOAD = os.environ.get(
    "IMPODO_PREPARATION_SCALE_WORKLOAD",
    "products",
).casefold()
PREPARATION_SCALE_PRODUCTS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_PRODUCTS", "16000")
)
PREPARATION_SCALE_BOM_LINES = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_BOM_LINES", "80000")
)
PREPARATION_SCALE_DIRTY = os.environ.get("IMPODO_PREPARATION_SCALE_DIRTY") == "1"
PREPARATION_BENCHMARK_PREFIX = "IMPODO_PREPARATION_BENCHMARK_JSON="
PREPARATION_WORKER_BENCHMARK_PREFIX = "IMPODO_PREPARATION_WORKER_BENCHMARK_JSON="


def _benchmark_uuid(label: str) -> UUID:
    """Return stable fixture identities so fresh-run hashes are comparable."""

    fixture_key = (
        f"{PREPARATION_SCALE_WORKLOAD}:"
        f"{PREPARATION_SCALE_ROWS}:"
        f"{PREPARATION_SCALE_PRODUCTS}:"
        f"{PREPARATION_SCALE_BOM_LINES}:"
        f"{PREPARATION_SCALE_COLUMNS}:"
        f"{PREPARATION_SCALE_MAPPED_FIELDS}:"
        f"{int(PREPARATION_SCALE_DIRTY)}"
    )
    return uuid5(NAMESPACE_URL, f"impodo:preparation-scale:{fixture_key}:{label}")


class _PeakWorkingSetSampler:
    """Sample cross-platform process working set during the timed operation."""

    def __init__(self, process, *, interval_seconds: float = 0.05) -> None:
        self._process = process
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_bytes = 0
        self.peak_tree_bytes = 0

    def __enter__(self) -> "_PeakWorkingSetSampler":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._sample()

    def _sample(self) -> None:
        try:
            memory = self._process.memory_info()
            working_set = getattr(memory, "peak_wset", memory.rss)
            self.peak_bytes = max(self.peak_bytes, working_set)
            tree_bytes = memory.rss
            for child in self._process.children(recursive=True):
                try:
                    tree_bytes += child.memory_info().rss
                except Exception:  # Process may exit between discovery and read.
                    continue
            self.peak_tree_bytes = max(self.peak_tree_bytes, tree_bytes)
        except Exception:  # The sampled process can exit at the end of a probe.
            return


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1",
    "100,000-row preparation scale probe is opt-in",
)
class PreparationWorkflowScaleTests(unittest.TestCase):
    """Exercise the real local preparation services and DuckDB repositories."""

    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.app = create_local_app(self.root)
        self.context = self.app.state.context
        self.artifacts = self.context.artifacts

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_preparation_workflow(self) -> None:
        import psutil

        if PREPARATION_SCALE_COLUMNS < PREPARATION_SCALE_MAPPED_FIELDS:
            self.fail("The source fixture must include every mapped field")
        if PREPARATION_SCALE_MAPPED_FIELDS < 3:
            self.fail("The benchmark requires identity, changed, and numeric fields")
        if not 1 <= PREPARATION_SCALE_EFFECT_FIELDS < PREPARATION_SCALE_MAPPED_FIELDS:
            self.fail("Effect fields must be positive and exclude the identity field")
        if PREPARATION_SCALE_WORKLOAD not in {"products", "bom", "customers"}:
            self.fail(
                "The benchmark workload must be 'products', 'bom', or 'customers'"
            )

        fixture_started = perf_counter()
        fixture_cpu_started = process_time()
        process = psutil.Process()
        with _PeakWorkingSetSampler(process) as fixture_memory_sampler:
            workspace_id, source_sha256, source_size_bytes = (
                self._prepare_project_and_evidence(
                    row_count=PREPARATION_SCALE_ROWS,
                    column_count=PREPARATION_SCALE_COLUMNS,
                    mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
                    dirty=PREPARATION_SCALE_DIRTY,
                )
            )
        if os.environ.get("IMPODO_PREPARATION_ADVANCED") == "1":
            self._enable_advanced_coverage(workspace_id)
        revision = self.context.preparation.mappings.get_mapping_revision(workspace_id)
        physical_selection = self.context.preparation.sources.get_source_selection(
            workspace_id
        )
        effective_selection = (
            self.context.preparation.sources.get_mapping_source_selection(workspace_id)
        )
        assert revision is not None
        assert physical_selection is not None
        assert effective_selection is not None
        route_manifest = compile_preparation_capability(
            definition=revision.definition,
            physical_selection=physical_selection,
            effective_selection=effective_selection,
            source_snapshots=(
                self.context.preparation.sources.get_current_source_snapshots(
                    workspace_id
                )
            ),
            derived_plan=(
                self.context.preparation.derived_entities.get_derived_entity_plan(
                    workspace_id
                )
            ),
            current_ruleset=(
                self.context.preparation.quality.current_ruleset(workspace_id)
            ),
            reference_bundle=(
                self.context.preparation.resolution.current_reference_bundle(workspace_id)
                if self.context.preparation.resolution is not None
                else None
            ),
        )
        fixture_seconds = perf_counter() - fixture_started
        fixture_cpu_seconds = process_time() - fixture_cpu_started
        fixture_peak_mib = fixture_memory_sampler.peak_bytes / (1024 * 1024)
        fixture_ending_mib = process.memory_info().rss / (1024 * 1024)

        phase_wall_seconds: dict[str, float] = {}
        phase_cpu_seconds: dict[str, float] = {}
        phase_calls: dict[str, int] = {}
        phase_checkpoint_rss_bytes: dict[str, int] = {}

        def record_phase(
            name: str,
            wall_seconds: float,
            cpu_seconds: float,
            *,
            replace_existing: bool = False,
        ) -> None:
            if replace_existing:
                phase_wall_seconds[name] = wall_seconds
                phase_cpu_seconds[name] = cpu_seconds
                phase_calls[name] = 1
                return
            phase_wall_seconds[name] = phase_wall_seconds.get(name, 0.0) + wall_seconds
            phase_cpu_seconds[name] = phase_cpu_seconds.get(name, 0.0) + cpu_seconds
            phase_calls[name] = phase_calls.get(name, 0) + 1

        def timed(name, callback):
            def invoke(*args, **kwargs):
                wall_started = perf_counter()
                cpu_started = process_time()
                try:
                    return callback(*args, **kwargs)
                finally:
                    record_phase(
                        name,
                        perf_counter() - wall_started,
                        process_time() - cpu_started,
                        replace_existing=True,
                    )
                    phase_checkpoint_rss_bytes[name] = max(
                        phase_checkpoint_rss_bytes.get(name, 0),
                        process.memory_info().rss,
                    )

            return invoke

        def accumulated(name, callback):
            def invoke(*args, **kwargs):
                wall_started = perf_counter()
                cpu_started = process_time()
                try:
                    return callback(*args, **kwargs)
                finally:
                    record_phase(
                        name,
                        perf_counter() - wall_started,
                        process_time() - cpu_started,
                    )

            return invoke

        original_iter_batches = source_module.SelectedSourceBatchStream.iter_batches

        def timed_iter_batches(stream):
            iterator = original_iter_batches(stream)
            while True:
                wall_started = perf_counter()
                cpu_started = process_time()
                try:
                    batch = next(iterator)
                except StopIteration:
                    return
                record_phase(
                    "source_batch_read",
                    perf_counter() - wall_started,
                    process_time() - cpu_started,
                )
                yield batch

        original_stage = preparation_module.stage_browser_mapping
        original_bounded_stage = preparation_module.prepare_bounded_direct_session
        original_publish = self.context.preparation.staging.publish_canonical_staging
        original_reload = self.context.preparation.staging.get_canonical_staging_run
        original_quality = self.context.preparation.quality.evaluate_and_publish
        original_quality_evaluation = quality_module.build_bounded_quality_run
        original_quality_persistence = (
            self.context.preparation.quality.quality._insert_quality_evidence
        )
        original_normalization = (
            self.context.preparation.normalization.evaluate_and_publish
        )
        original_normalization_aggregation = (
            normalization_module.build_bounded_normalization_evaluation
        )
        original_normalization_persistence = self.context.preparation.normalization.repository._insert_normalization_evidence
        original_normalization_effect = (
            bounded_normalization_module._BoundedNormalizationEffects._effect
        )
        original_append_rows = self.context.preparation.sessions.append_direct_rows
        original_append_impacts = self.context.preparation.sessions.append_impacts
        original_append_native_projection = (
            self.context.preparation.sessions.append_native_prepared_projection
        )
        original_finalize_session = (
            self.context.preparation.sessions.finalize_direct_session
        )
        original_projected_rows = (
            self.context.preparation.sessions._iter_projected_dataset_encoded_batches
        )
        original_project_row = evaluator_module.CompiledBrowserRowTransformer.project
        original_finish_row = evaluator_module.CompiledBrowserRowTransformer.finish
        original_scalar_value = evaluator_module.evaluate_scalar_mapping_value
        original_prepare_row = source_module.CompiledPreparedRowTransformer.transform
        original_canonical_row = bounded_preparation_module.canonical_row_from_prepared
        original_columnar_canonical_row = (
            bounded_preparation_module.canonical_prepared_session_row
        )
        original_native_batches = PolarsTransformationAdapter.iter_prepared_batches
        original_canonical_json = bounded_preparation_module.canonical_json_bytes
        original_projection_canonical_json = (
            canonical_projection_module.canonical_json_bytes
        )

        def instrumented_native_batches(*args, **kwargs):
            for batch in original_native_batches(*args, **kwargs):
                phase_calls["native_prepared_batches"] = (
                    phase_calls.get("native_prepared_batches", 0) + 1
                )
                phase_calls["python_row_adaptation"] = phase_calls.get(
                    "python_row_adaptation", 0
                ) + len(batch.source_rows)
                phase_calls["full_prepared_record_construction"] = phase_calls.get(
                    "full_prepared_record_construction", 0
                ) + len(batch.records)
                yield batch

        def instrumented_projected_rows(*args, **kwargs):
            phase_calls["prepared_value_projection_scans"] = (
                phase_calls.get("prepared_value_projection_scans", 0) + 1
            )
            projection = next(
                (
                    item
                    for item in (*args, *kwargs.values())
                    if isinstance(item, PreparedCanonicalProjection)
                ),
                None,
            )
            set_based = bool(projection is not None and projection.set_based_projection)
            for batch in original_projected_rows(*args, **kwargs):
                name = (
                    "bounded_encoded_projection_rows"
                    if set_based
                    else "prepared_value_projection_rows"
                )
                phase_calls[name] = phase_calls.get(name, 0) + len(batch)
                yield batch

        def instrumented_native_projection(*args, **kwargs):
            result = original_append_native_projection(*args, **kwargs)
            if result is not None:
                phase_calls["native_set_projection_datasets"] = (
                    phase_calls.get("native_set_projection_datasets", 0) + 1
                )
                phase_calls["native_set_projection_scans"] = (
                    phase_calls.get("native_set_projection_scans", 0)
                    + result.scan_count
                )
                phase_calls["native_set_projection_statements"] = (
                    phase_calls.get("native_set_projection_statements", 0)
                    + result.statement_count
                )
                phase_calls["optimized_plan_verified"] = int(
                    result.optimized_plan_verified
                )
                phase_calls["bounded_execution_plan_verified"] = int(
                    result.bounded_execution_plan_verified
                )
            return result

        def instrumented_normalization_effect(*args, **kwargs):
            built = original_normalization_effect(*args, **kwargs)
            if built is not None:
                phase_calls["normalization_effect_construction"] = (
                    phase_calls.get("normalization_effect_construction", 0) + 1
                )
            return built

        trace_python_allocations = (
            os.environ.get("IMPODO_PREPARATION_TRACE_PYTHON") == "1"
        )
        if trace_python_allocations:
            tracemalloc.start()
        started = perf_counter()
        cpu_started = process_time()
        process_cpu_started = process.cpu_times()
        with ExitStack() as stack:
            memory_sampler = stack.enter_context(_PeakWorkingSetSampler(process))
            stack.enter_context(
                patch(
                    "impodo.application.preparation_service."
                    "require_supported_browser_scale",
                )
            )
            stack.enter_context(
                patch(
                    "impodo.domain.staging.evaluator.require_supported_browser_scale",
                )
            )
            patches = (
                patch(
                    "impodo.application.preparation_service.stage_browser_mapping",
                    timed("load_and_evaluate", original_stage),
                ),
                patch(
                    "impodo.application.preparation_service."
                    "prepare_bounded_direct_session",
                    timed("bounded_load_and_evaluate", original_bounded_stage),
                ),
                patch.object(
                    self.context.preparation.staging,
                    "publish_canonical_staging",
                    timed("staging_publication", original_publish),
                ),
                patch.object(
                    self.context.preparation.staging,
                    "get_canonical_staging_run",
                    timed("staging_reload", original_reload),
                ),
                patch.object(
                    self.context.preparation.quality,
                    "evaluate_and_publish",
                    timed("quality", original_quality),
                ),
                patch(
                    "impodo.application.quality_service.build_bounded_quality_run",
                    timed("quality_evaluation", original_quality_evaluation),
                ),
                patch.object(
                    self.context.preparation.quality.quality,
                    "_insert_quality_evidence",
                    timed(
                        "quality_persistence_and_hash",
                        original_quality_persistence,
                    ),
                ),
                patch.object(
                    self.context.preparation.normalization,
                    "evaluate_and_publish",
                    timed("normalization", original_normalization),
                ),
                patch(
                    "impodo.application.normalization_service."
                    "build_bounded_normalization_evaluation",
                    timed(
                        "normalization_aggregation",
                        original_normalization_aggregation,
                    ),
                ),
                patch.object(
                    self.context.preparation.normalization.repository,
                    "_insert_normalization_evidence",
                    timed(
                        "normalization_persistence_and_hash",
                        original_normalization_persistence,
                    ),
                ),
                patch.object(
                    self.context.preparation.sessions,
                    "append_direct_rows",
                    accumulated("direct_canonical_append", original_append_rows),
                ),
                patch.object(
                    self.context.preparation.sessions,
                    "append_impacts",
                    accumulated("session_impact_append", original_append_impacts),
                ),
                patch.object(
                    self.context.preparation.sessions,
                    "append_native_prepared_projection",
                    instrumented_native_projection,
                ),
                patch.object(
                    self.context.preparation.sessions,
                    "finalize_direct_session",
                    timed("direct_finalization", original_finalize_session),
                ),
                patch.object(
                    self.context.preparation.sessions,
                    "_iter_projected_dataset_encoded_batches",
                    instrumented_projected_rows,
                ),
                patch.object(
                    source_module.SelectedSourceBatchStream,
                    "iter_batches",
                    timed_iter_batches,
                ),
                patch.object(
                    evaluator_module.CompiledBrowserRowTransformer,
                    "project",
                    accumulated("row_projection", original_project_row),
                ),
                patch.object(
                    evaluator_module.CompiledBrowserRowTransformer,
                    "finish",
                    accumulated("row_finish_inclusive", original_finish_row),
                ),
                patch.object(
                    evaluator_module,
                    "evaluate_scalar_mapping_value",
                    accumulated("scalar_value_evaluation", original_scalar_value),
                ),
                patch.object(
                    source_module.CompiledPreparedRowTransformer,
                    "transform",
                    accumulated(
                        "prepared_record_construction",
                        original_prepare_row,
                    ),
                ),
                patch.object(
                    bounded_preparation_module,
                    "canonical_row_from_prepared",
                    accumulated(
                        "canonical_row_construction",
                        original_canonical_row,
                    ),
                ),
                patch.object(
                    bounded_preparation_module,
                    "canonical_prepared_session_row",
                    accumulated(
                        "canonical_index_projection",
                        original_columnar_canonical_row,
                    ),
                ),
                patch.object(
                    PolarsTransformationAdapter,
                    "iter_prepared_batches",
                    staticmethod(instrumented_native_batches),
                ),
                patch.object(
                    bounded_preparation_module,
                    "canonical_json_bytes",
                    accumulated(
                        "canonical_serialization",
                        original_canonical_json,
                    ),
                ),
                patch.object(
                    canonical_projection_module,
                    "canonical_json_bytes",
                    accumulated(
                        "canonical_serialization",
                        original_projection_canonical_json,
                    ),
                ),
                patch.object(
                    bounded_normalization_module._BoundedNormalizationEffects,
                    "_effect",
                    instrumented_normalization_effect,
                ),
            )
            for active_patch in patches:
                stack.enter_context(active_patch)
            normalization = self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )
        elapsed = perf_counter() - started
        cpu_seconds = process_time() - cpu_started
        process_cpu_finished = process.cpu_times()
        if trace_python_allocations:
            python_traced_current, python_traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        else:
            python_traced_current = 0
            python_traced_peak = 0

        staging = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        quality = self.context.preparation.quality.current_summary(workspace_id)
        current_normalization = self.context.preparation.normalization.current_summary(
            workspace_id
        )
        self.assertIsNotNone(staging)
        self.assertIsNotNone(quality)
        self.assertIsNotNone(current_normalization)
        assert staging is not None
        assert quality is not None
        assert current_normalization is not None

        ending_mib = process.memory_info().rss / (1024 * 1024)
        peak_mib = memory_sampler.peak_bytes / (1024 * 1024)
        peak_tree_mib = memory_sampler.peak_tree_bytes / (1024 * 1024)
        database_path = (
            self.context.preparation.staging.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        source_snapshots = (
            self.context.preparation.sources.get_current_source_snapshots(workspace_id)
        )
        snapshot_bytes = sum(
            (self.root / workspace_id / snapshot.parquet_storage_key).stat().st_size
            for snapshot in source_snapshots
        )
        prepared_snapshots = (
            self.context.preparation.sessions.current_prepared_snapshots(workspace_id)
        )
        prepared_snapshot_bytes = sum(
            (self.root / workspace_id / snapshot.parquet_storage_key).stat().st_size
            for snapshot in prepared_snapshots
        )
        with self.context.preparation.staging._connect(database_path) as connection:
            connection.execute("CHECKPOINT")
            database_size_row = connection.execute("PRAGMA database_size").fetchone()
            memory_rows = connection.execute(
                "SELECT memory_usage_bytes, temporary_storage_bytes "
                "FROM duckdb_memory()"
            ).fetchall()
            serialized_rows = connection.execute(
                """
                SELECT 'canonical_staging_row',
                       COALESCE(SUM(LENGTH(row_json)), 0)
                  FROM canonical_staging_row
                UNION ALL
                SELECT 'quality_row_result',
                       COALESCE(SUM(LENGTH(row_json)), 0)
                  FROM quality_row_result
                UNION ALL
                SELECT 'quality_issue',
                       COALESCE(SUM(LENGTH(issue_json)), 0)
                  FROM quality_issue
                UNION ALL
                SELECT 'source_accounting_entry',
                       COALESCE(SUM(LENGTH(entry_json)), 0)
                  FROM source_accounting_entry
                UNION ALL
                SELECT 'quality_quarantine_entry',
                       COALESCE(SUM(LENGTH(entry_json)), 0)
                  FROM quality_quarantine_entry
                UNION ALL
                SELECT 'normalization_effect',
                       COALESCE(SUM(LENGTH(effect_json)), 0)
                  FROM normalization_effect
                UNION ALL
                SELECT 'normalization_group',
                       COALESCE(SUM(LENGTH(group_json)), 0)
                  FROM normalization_group
                ORDER BY 1
                """
            ).fetchall()
            counters = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COALESCE(MAX(row_count), 0) FROM quality_run),
                    (SELECT COUNT(*) FROM quality_issue),
                    (SELECT COUNT(*) FROM quality_quarantine_entry),
                    (SELECT COALESCE(MAX(source_count), 0) FROM quality_run),
                    (SELECT COUNT(*) FROM normalization_effect),
                    (SELECT COUNT(*) FROM normalization_group),
                    COALESCE((SELECT SUM(LENGTH(row_json)) FROM canonical_staging_row), 0)
                      + COALESCE((SELECT SUM(LENGTH(row_json)) FROM quality_row_result), 0)
                      + COALESCE((SELECT SUM(LENGTH(issue_json)) FROM quality_issue), 0)
                      + COALESCE((SELECT SUM(LENGTH(entry_json)) FROM source_accounting_entry), 0)
                      + COALESCE((SELECT SUM(LENGTH(entry_json)) FROM quality_quarantine_entry), 0)
                      + COALESCE((SELECT SUM(LENGTH(effect_json)) FROM normalization_effect), 0)
                      + COALESCE((SELECT SUM(LENGTH(group_json)) FROM normalization_group), 0)
                """
            ).fetchone()
            physical_quality_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM quality_row_result),
                    (SELECT COUNT(*) FROM source_accounting_entry)
                """
            ).fetchone()
        assert database_size_row is not None
        block_size = int(database_size_row[2])
        database_file_bytes = database_path.stat().st_size
        project_directory = self.root / workspace_id
        project_storage_bytes = sum(
            item.stat().st_size
            for item in project_directory.rglob("*")
            if item.is_file()
        )
        parquet_bytes = sum(
            item.stat().st_size
            for item in project_directory.rglob("*.parquet")
            if item.is_file()
        )
        database_mib = database_file_bytes / (1024 * 1024)
        storage_metrics = {
            "database_file_bytes": database_file_bytes,
            "database_free_bytes": block_size * int(database_size_row[5]),
            "database_used_bytes": block_size * int(database_size_row[4]),
            "duckdb_current_memory_bytes": sum(int(item[0]) for item in memory_rows),
            "duckdb_current_temporary_bytes": sum(int(item[1]) for item in memory_rows),
            "parquet_bytes": parquet_bytes,
            "prepared_snapshot_bytes": prepared_snapshot_bytes,
            "physical_quality_row_count": int(physical_quality_counts[0]),
            "physical_source_accounting_count": int(physical_quality_counts[1]),
            "project_storage_bytes": project_storage_bytes,
            "serialized_characters_by_table": {
                str(table): int(characters) for table, characters in serialized_rows
            },
            "source_snapshot_bytes": snapshot_bytes,
            "wal_size": str(database_size_row[6]),
        }
        assert counters is not None
        (
            canonical_rows,
            quality_rows,
            quality_issues,
            quarantine_entries,
            lineage_links,
            normalization_effects,
            normalization_groups,
            serialized_characters,
        ) = (int(item) for item in counters)
        phase_text = ", ".join(
            f"{name}={seconds:.3f}s" for name, seconds in phase_wall_seconds.items()
        )
        primary_phase_names = tuple(
            name
            for name in (
                "load_and_evaluate",
                "bounded_load_and_evaluate",
                "staging_publication",
                "quality",
                "normalization",
            )
            if name in phase_wall_seconds
        )
        primary_wall_seconds = sum(
            phase_wall_seconds[name] for name in primary_phase_names
        )
        primary_cpu_seconds = sum(
            phase_cpu_seconds[name] for name in primary_phase_names
        )
        route_by_stage = {
            item.stage: item.behavior.value for item in route_manifest.stages
        }
        vectorization_report = {
            "full_canonical_rows_constructed": phase_calls.get(
                "canonical_row_construction",
                0,
            )
            + phase_calls.get("prepared_value_projection_rows", 0),
            "full_prepared_records_constructed": phase_calls.get(
                "full_prepared_record_construction",
                0,
            ),
            "normalization_effects_constructed": phase_calls.get(
                "normalization_effect_construction",
                0,
            ),
            "python_cell_callbacks": phase_calls.get(
                "scalar_value_evaluation",
                0,
            ),
            "python_row_callbacks": (
                phase_calls.get("python_row_adaptation", 0)
                + phase_calls.get("row_finish_inclusive", 0)
                + phase_calls.get("prepared_record_construction", 0)
                + phase_calls.get("prepared_value_projection_rows", 0)
            ),
            "bounded_projection_rows": phase_calls.get(
                "bounded_encoded_projection_rows",
                0,
            ),
            "bounded_projection_scans": phase_calls.get(
                "prepared_value_projection_scans",
                0,
            ),
            "native_set_projection_scans": phase_calls.get(
                "native_set_projection_scans",
                0,
            ),
            "native_set_projection_statements": phase_calls.get(
                "native_set_projection_statements",
                0,
            ),
            "row_weighted_native_coverage_percent": (
                100.0
                if route_by_stage.get("transformation") == "NATIVE_COLUMNAR"
                else 0.0
            ),
            "rule_impact_python_replay_rows": phase_calls.get(
                "python_rule_impact_replay",
                0,
            ),
            "global_operations_classification": "SET_GLOBAL",
            "optimized_plan_verified": bool(
                phase_calls.get("optimized_plan_verified", 0)
            ),
            "bounded_execution_plan_verified": bool(
                phase_calls.get("bounded_execution_plan_verified", 0)
            ),
            "stage_routes": route_by_stage,
        }
        print(
            "Complete preparation scale probe: "
            f"workload={PREPARATION_SCALE_WORKLOAD}, "
            f"dirty={PREPARATION_SCALE_DIRTY}, "
            f"rows={PREPARATION_SCALE_ROWS:,}, "
            f"columns={PREPARATION_SCALE_COLUMNS}, "
            f"mapped_fields={PREPARATION_SCALE_MAPPED_FIELDS}, "
            f"effect_fields={PREPARATION_SCALE_EFFECT_FIELDS}, "
            "normalization_fact_route=durable, "
            f"fixture={fixture_seconds:.3f}s, total={elapsed:.3f}s, "
            f"fixture_peak={fixture_peak_mib:.1f} MiB, "
            f"peak={peak_mib:.1f} MiB, ending_rss={ending_mib:.1f} MiB, "
            f"peak_tree={peak_tree_mib:.1f} MiB, "
            f"database={database_mib:.1f} MiB, "
            f"database_used={storage_metrics['database_used_bytes'] / (1024 * 1024):.1f} MiB, "
            f"project_storage={project_storage_bytes / (1024 * 1024):.1f} MiB, "
            f"snapshots={len(source_snapshots)}, snapshot_bytes={snapshot_bytes}, "
            f"source_bytes={source_size_bytes}, source_sha256={source_sha256}, "
            f"selected_cells={PREPARATION_SCALE_ROWS * PREPARATION_SCALE_COLUMNS}, "
            f"mapped_scalar_evaluations={PREPARATION_SCALE_ROWS * PREPARATION_SCALE_MAPPED_FIELDS}, "
            f"canonical_rows={canonical_rows}, quality_rows={quality_rows}, "
            f"quality_issues={quality_issues}, quarantine_entries={quarantine_entries}, "
            f"lineage_links={lineage_links}, effects={normalization_effects}, "
            f"normalization_groups={normalization_groups}, "
            f"serialized_characters={serialized_characters}, "
            f"{phase_text}, staging_hash={staging.content_hash}, "
            f"quality_hash={quality.content_hash}, "
            f"normalization_hash={normalization.content_hash}"
        )

        if os.environ.get("IMPODO_PREPARATION_SCALE_JSON") == "1":
            result = {
                "batch_sizes": {
                    "source_rows": BOUNDED_SOURCE_BATCH_SIZE,
                },
                "columns": PREPARATION_SCALE_COLUMNS,
                "counts": {
                    "canonical_rows": canonical_rows,
                    "lineage_links": lineage_links,
                    "normalization_effects": normalization_effects,
                    "normalization_groups": normalization_groups,
                    "quality_issues": quality_issues,
                    "quality_rows": quality_rows,
                    "quarantine_entries": quarantine_entries,
                    "serialized_characters": serialized_characters,
                },
                "cpu_seconds": cpu_seconds,
                "cpu_system_seconds": (
                    process_cpu_finished.system - process_cpu_started.system
                ),
                "cpu_user_seconds": (
                    process_cpu_finished.user - process_cpu_started.user
                ),
                "database_mib": database_mib,
                "dirty": PREPARATION_SCALE_DIRTY,
                "effect_fields": PREPARATION_SCALE_EFFECT_FIELDS,
                "ending_rss_mib": ending_mib,
                "fixture": {
                    "cpu_seconds": fixture_cpu_seconds,
                    "ending_rss_mib": fixture_ending_mib,
                    "peak_working_set_mib": fixture_peak_mib,
                    "sha256": source_sha256,
                    "size_bytes": source_size_bytes,
                    "snapshot_bytes": snapshot_bytes,
                    "snapshot_count": len(source_snapshots),
                    "wall_seconds": fixture_seconds,
                },
                "hashes": {
                    "normalization": normalization.content_hash,
                    "quality": quality.content_hash,
                    "staging": staging.content_hash,
                },
                "mapped_fields": PREPARATION_SCALE_MAPPED_FIELDS,
                "normalization_fact_route": "durable",
                "peak_working_set_mib": peak_mib,
                "peak_process_tree_mib": peak_tree_mib,
                "phase_calls": phase_calls,
                "phase_cpu_seconds": phase_cpu_seconds,
                "phase_checkpoint_rss_mib": {
                    name: value / (1024 * 1024)
                    for name, value in sorted(phase_checkpoint_rss_bytes.items())
                },
                "phase_wall_seconds": phase_wall_seconds,
                "phase_reconciliation": {
                    "primary_cpu_seconds": primary_cpu_seconds,
                    "primary_phase_names": list(primary_phase_names),
                    "primary_wall_seconds": primary_wall_seconds,
                    "unattributed_cpu_seconds": max(
                        0.0,
                        cpu_seconds - primary_cpu_seconds,
                    ),
                    "unattributed_wall_seconds": max(
                        0.0,
                        elapsed - primary_wall_seconds,
                    ),
                },
                "platform": platform.platform(),
                "python_traced_allocations": {
                    "current_mib": python_traced_current / (1024 * 1024),
                    "enabled": trace_python_allocations,
                    "peak_mib": python_traced_peak / (1024 * 1024),
                },
                "python": sys.version,
                "revision": os.environ.get(
                    "IMPODO_PREPARATION_BENCHMARK_REVISION",
                    "unknown",
                ),
                "rows": PREPARATION_SCALE_ROWS,
                "route_manifest": route_manifest.to_portable_dict(),
                "runtime_versions": {
                    name: _installed_version(name)
                    for name in ("duckdb", "openpyxl", "polars", "psutil")
                },
                "schema_version": 2,
                "storage": storage_metrics,
                "vectorization_report": vectorization_report,
                "wall_seconds": elapsed,
                "workload": PREPARATION_SCALE_WORKLOAD,
            }
            print(
                PREPARATION_BENCHMARK_PREFIX
                + json.dumps(result, separators=(",", ":"), sort_keys=True)
            )

        self.assertEqual(staging.total_rows, PREPARATION_SCALE_ROWS)
        collision_groups = (
            (PREPARATION_SCALE_ROWS + 98) // 100
            if PREPARATION_SCALE_DIRTY and PREPARATION_SCALE_ROWS >= 2
            else 0
        )
        expected_quarantine = collision_groups * 2
        self.assertEqual(
            quality.ready_count,
            PREPARATION_SCALE_ROWS - expected_quarantine,
        )
        self.assertEqual(quality.review_count, 0)
        self.assertEqual(quality.quarantined_count, expected_quarantine)
        self.assertEqual(quality.blocked_count, 0)
        self.assertEqual(
            current_normalization.content_hash,
            normalization.content_hash,
        )
        self.assertEqual(
            normalization.eligible_record_count,
            PREPARATION_SCALE_ROWS - expected_quarantine,
        )
        self.assertEqual(
            normalization.changed_record_count,
            PREPARATION_SCALE_ROWS - expected_quarantine,
        )
        self.assertEqual(
            current_normalization.set_aside_record_count,
            expected_quarantine,
        )
        self.assertEqual(
            phase_calls.get("normalization_effect_construction", 0),
            normalization_effects,
        )
        self.assertEqual(staging.failed_control_total_count, 0)

    def test_bounded_source_preparation_phase(self) -> None:
        """Measure P3 independently from still-materializing P4 stages."""

        import psutil

        workspace_id, source_sha256, source_size_bytes = (
            self._prepare_project_and_evidence(
                row_count=PREPARATION_SCALE_ROWS,
                column_count=PREPARATION_SCALE_COLUMNS,
                mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
            )
        )
        workspace_state = self.context.preparation.workspaces.get(workspace_id)
        revision = self.context.preparation.mappings.get_mapping_revision(workspace_id)
        physical = self.context.preparation.sources.get_source_selection(workspace_id)
        effective = self.context.preparation.sources.get_mapping_source_selection(
            workspace_id
        )
        assert revision is not None
        assert physical is not None
        assert effective is not None
        reference_bundle = (
            self.context.preparation.resolution.current_reference_bundle(workspace_id)
            if self.context.preparation.resolution is not None
            else None
        )
        process = psutil.Process()
        source_snapshots = (
            self.context.preparation.sources.get_current_source_snapshots(workspace_id)
        )
        phase_seconds: dict[str, float] = {}

        def timed_call(name, callback):
            def invoke(*args, **kwargs):
                phase_started = perf_counter()
                try:
                    return callback(*args, **kwargs)
                finally:
                    phase_seconds[name] = phase_seconds.get(name, 0.0) + (
                        perf_counter() - phase_started
                    )

            return invoke

        original_prepared_writer = PolarsTransformationAdapter.write_prepared_snapshot
        original_prepared_batches = PolarsTransformationAdapter.iter_prepared_batches
        original_project_row = evaluator_module.CompiledBrowserRowTransformer.project
        original_finish_row = evaluator_module.CompiledBrowserRowTransformer.finish
        original_prepare_row = source_module.CompiledPreparedRowTransformer.transform
        original_canonical_adapter = bounded_preparation_module._canonical_session_row

        def timed_prepared_batches(*args, **kwargs):
            iterator = original_prepared_batches(*args, **kwargs)
            while True:
                phase_started = perf_counter()
                try:
                    batch = next(iterator)
                except StopIteration:
                    return
                finally:
                    phase_seconds["prepared_snapshot_read_and_adapt"] = (
                        phase_seconds.get("prepared_snapshot_read_and_adapt", 0.0)
                        + perf_counter()
                        - phase_started
                    )
                yield batch

        started = perf_counter()
        with ExitStack() as stack:
            memory_sampler = stack.enter_context(_PeakWorkingSetSampler(process))
            stack.enter_context(
                patch.object(
                    PolarsTransformationAdapter,
                    "write_prepared_snapshot",
                    staticmethod(timed_call(
                        "prepared_snapshot_transform_and_write",
                        original_prepared_writer,
                    )),
                )
            )
            stack.enter_context(
                patch.object(
                    PolarsTransformationAdapter,
                    "iter_prepared_batches",
                    staticmethod(timed_prepared_batches),
                )
            )
            stack.enter_context(
                patch.object(
                    evaluator_module.CompiledBrowserRowTransformer,
                    "project",
                    timed_call("python_row_projection", original_project_row),
                )
            )
            stack.enter_context(
                patch.object(
                    evaluator_module.CompiledBrowserRowTransformer,
                    "finish",
                    timed_call("python_row_transformation", original_finish_row),
                )
            )
            stack.enter_context(
                patch.object(
                    source_module.CompiledPreparedRowTransformer,
                    "transform",
                    timed_call("python_prepared_record", original_prepare_row),
                )
            )
            stack.enter_context(
                patch.object(
                    bounded_preparation_module,
                    "_canonical_session_row",
                    timed_call("canonical_adaptation", original_canonical_adapter),
                )
            )
            stack.enter_context(
                patch.object(
                    self.context.preparation.sessions,
                    "append_direct_rows",
                    timed_call(
                        "canonical_append",
                        self.context.preparation.sessions.append_direct_rows,
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    self.context.preparation.sessions,
                    "finalize_direct_session",
                    timed_call(
                        "session_finalization",
                        self.context.preparation.sessions.finalize_direct_session,
                    ),
                )
            )
            bounded = preparation_module.prepare_bounded_direct_session(
                workspace_state,
                revision.definition,
                revision.version,
                physical,
                effective,
                self.context.preparation.sources.get_source_catalogs(workspace_id),
                self.artifacts,
                reference_bundle,
                self.context.preparation.sessions,
                COLUMNAR_TRANSFORMATIONS,
                actor=self.context.actor,
                source_snapshots=source_snapshots,
            )
        elapsed = perf_counter() - started
        peak_mib = memory_sampler.peak_bytes / (1024 * 1024)
        ending_mib = process.memory_info().rss / (1024 * 1024)
        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        database_mib = database_path.stat().st_size / (1024 * 1024)
        self.assertEqual(len(bounded.run.rows), PREPARATION_SCALE_ROWS)
        for forbidden_phase in (
            "python_row_projection",
            "python_row_transformation",
            "python_prepared_record",
        ):
            self.assertNotIn(forbidden_phase, phase_seconds)
        print(
            "Bounded source preparation probe: "
            "backend=polars, "
            f"workload={PREPARATION_SCALE_WORKLOAD}, "
            f"rows={PREPARATION_SCALE_ROWS:,}, "
            f"columns={PREPARATION_SCALE_COLUMNS}, "
            f"mapped_fields={PREPARATION_SCALE_MAPPED_FIELDS}, "
            f"total={elapsed:.3f}s, peak={peak_mib:.1f} MiB, "
            f"ending_rss={ending_mib:.1f} MiB, database={database_mib:.1f} MiB, "
            f"phases={json.dumps(phase_seconds, sort_keys=True)}, "
            f"source_bytes={source_size_bytes}, source_sha256={source_sha256}, "
            f"session={bounded.session_id}"
        )

    def test_background_worker_releases_its_working_memory(self) -> None:
        """Verify fresh-process first/repeat preparation and worker reclamation."""

        import psutil

        related_product_bom = PREPARATION_SCALE_WORKLOAD == "product-bom"
        effective_rows = (
            PREPARATION_SCALE_PRODUCTS + PREPARATION_SCALE_BOM_LINES
            if related_product_bom
            else PREPARATION_SCALE_ROWS
        )
        if effective_rows > COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
            self.skipTest(
                "The production background probe honors the current "
                f"{COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:,}-row "
                "columnar-direct safety limit"
            )

        if related_product_bom:
            workspace_id, source_sha256, source_size_bytes = (
                self._prepare_related_product_bom_project_and_evidence(
                    product_count=PREPARATION_SCALE_PRODUCTS,
                    bom_line_count=PREPARATION_SCALE_BOM_LINES,
                    column_count=PREPARATION_SCALE_COLUMNS,
                    mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
                )
            )
        else:
            workspace_id, source_sha256, source_size_bytes = (
                self._prepare_project_and_evidence(
                    row_count=PREPARATION_SCALE_ROWS,
                    column_count=PREPARATION_SCALE_COLUMNS,
                    mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
                    dirty=PREPARATION_SCALE_DIRTY,
                )
            )
        workspace_state = self.context.queries.get(workspace_id)
        selection = self.context.queries.get_source_selection(workspace_id)
        assert selection is not None
        manager = self.context.preparation_jobs
        assert manager is not None
        workspace = PreparationWorkspace.from_resolution(
            self.context.recipes.resolve_workspace(
                workspace_id,
                actor=self.context.actor,
            )
        )
        parent_process = psutil.Process()
        parent_rss_before_jobs = parent_process.memory_info().rss

        def run_attempt() -> tuple[float, float, int]:
            job = manager.enqueue(
                workspace_id,
                workspace_state.name,
                sum(item.row_count for item in selection.datasets),
                actor=self.context.actor,
                workspace=workspace,
            )
            started = perf_counter()
            peak_worker_bytes = 0
            worker_cpu_seconds = 0.0
            deadline = started + 600
            while perf_counter() < deadline:
                current = manager.get(workspace_id, job.job_id)
                worker_pid = manager.worker_pid(job.job_id)
                if worker_pid is not None:
                    try:
                        worker_process = psutil.Process(worker_pid)
                        memory = worker_process.memory_info()
                        working_set = getattr(memory, "peak_wset", memory.rss)
                        peak_worker_bytes = max(peak_worker_bytes, working_set)
                        cpu_times = worker_process.cpu_times()
                        worker_cpu_seconds = max(
                            worker_cpu_seconds,
                            float(cpu_times.user + cpu_times.system),
                        )
                    except psutil.NoSuchProcess:
                        pass
                if current.terminal:
                    break
                sleep(0.05)
            else:
                self.fail("Background preparation did not finish within ten minutes")
            self.assertEqual(
                current.status,
                PreparationJobStatus.SUCCEEDED,
                msg=f"{current.failure_code}: {current.failure_message}",
            )
            worker_deadline = perf_counter() + 5
            while manager.worker_alive(job.job_id) and perf_counter() < worker_deadline:
                sleep(0.01)
            self.assertFalse(manager.worker_alive(job.job_id))
            return (
                perf_counter() - started,
                worker_cpu_seconds,
                peak_worker_bytes,
            )

        def storage_evidence() -> dict[str, int]:
            database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
            with self.context.preparation.staging._connect(database_path) as connection:
                connection.execute("CHECKPOINT")
                database_size_row = connection.execute(
                    "PRAGMA database_size"
                ).fetchone()
            assert database_size_row is not None
            block_size = int(database_size_row[2])
            project_directory = self.root / workspace_id
            return {
                "database_file_bytes": database_path.stat().st_size,
                "database_free_bytes": block_size * int(database_size_row[5]),
                "database_used_bytes": block_size * int(database_size_row[4]),
                "project_storage_bytes": sum(
                    item.stat().st_size
                    for item in project_directory.rglob("*")
                    if item.is_file()
                ),
            }

        def vectorization_evidence() -> dict[str, object]:
            database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
            with self.context.preparation.staging._connect(database_path) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE CAST(json_extract(
                               projection.projection_json,
                               '$.set_based_projection'
                           ) AS BOOLEAN))
                      FROM canonical_staging_current AS current
                      JOIN canonical_prepared_projection AS projection
                        ON projection.run_id = current.run_id
                    """
                ).fetchone()
            projection_count = int(row[0]) if row is not None else 0
            set_based_count = int(row[1]) if row is not None else 0
            expected_count = 2 if related_product_bom else 1
            set_based = (
                projection_count == expected_count and set_based_count == expected_count
            )
            return {
                "bounded_execution_plan_verified": set_based,
                "full_canonical_rows_constructed": 0 if set_based else None,
                "full_prepared_records_constructed": 0 if set_based else None,
                "global_operations_classification": (
                    "SET_GLOBAL" if set_based else "UNVERIFIED"
                ),
                "optimized_plan_verified": set_based,
                "python_cell_callbacks": 0 if set_based else None,
                "python_row_callbacks": 0 if set_based else None,
                "row_weighted_native_coverage_percent": (100.0 if set_based else 0.0),
                "rule_impact_python_replay_rows": 0 if set_based else None,
                "set_based_projection_datasets": set_based_count,
            }

        first_seconds, first_cpu_seconds, first_peak = run_attempt()
        first_staging = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        first_quality = self.context.preparation.quality.current_summary(workspace_id)
        first_normalization = self.context.preparation.normalization.current_summary(
            workspace_id
        )
        first_storage = storage_evidence()
        parent_rss_after_first = parent_process.memory_info().rss
        prepared = self.context.preparation.sessions.current_prepared_snapshots(
            workspace_id
        )
        expected_prepared_count = 2 if related_product_bom else 1
        self.assertEqual(len(prepared), expected_prepared_count)
        prepared_modified = {
            item.parquet_storage_key: (
                self.root / workspace_id / item.parquet_storage_key
            )
            .stat()
            .st_mtime_ns
            for item in prepared
        }
        for source_file in workspace_state.source_files:
            self.artifacts.delete_source(
                workspace_id,
                source_file.stored_name,
            )

        repeat_seconds, repeat_cpu_seconds, repeat_peak = run_attempt()
        repeated_staging = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        repeated_quality = self.context.preparation.quality.current_summary(workspace_id)
        repeated_normalization = self.context.preparation.normalization.current_summary(
            workspace_id
        )
        repeat_storage = storage_evidence()
        parent_rss_after_repeat = parent_process.memory_info().rss
        self.assertIsNotNone(first_staging)
        self.assertIsNotNone(first_quality)
        self.assertIsNotNone(first_normalization)
        self.assertIsNotNone(repeated_staging)
        self.assertIsNotNone(repeated_quality)
        self.assertIsNotNone(repeated_normalization)
        assert first_staging is not None
        assert first_quality is not None
        assert first_normalization is not None
        assert repeated_staging is not None
        assert repeated_quality is not None
        assert repeated_normalization is not None
        self.assertEqual(first_staging.total_rows, effective_rows)
        self.assertEqual(repeated_staging.total_rows, effective_rows)
        self.assertEqual(repeated_staging.content_hash, first_staging.content_hash)
        self.assertEqual(repeated_quality.content_hash, first_quality.content_hash)
        self.assertEqual(
            repeated_normalization.content_hash,
            first_normalization.content_hash,
        )
        self.assertEqual(
            self.context.preparation.sessions.current_prepared_snapshots(workspace_id),
            prepared,
        )
        self.assertEqual(
            {
                item.parquet_storage_key: (
                    self.root / workspace_id / item.parquet_storage_key
                )
                .stat()
                .st_mtime_ns
                for item in prepared
            },
            prepared_modified,
        )
        print(
            "Background preparation probe: "
            f"rows={effective_rows:,}, "
            f"first={first_seconds:.3f}s/{first_peak / (1024 * 1024):.1f} MiB, "
            f"repeat={repeat_seconds:.3f}s/{repeat_peak / (1024 * 1024):.1f} MiB, "
            "workers_exited=yes, source_reopened=no"
        )
        if os.environ.get("IMPODO_PREPARATION_WORKER_JSON") == "1":
            result = {
                "columns": PREPARATION_SCALE_COLUMNS,
                "dirty": PREPARATION_SCALE_DIRTY,
                "effect_fields": PREPARATION_SCALE_EFFECT_FIELDS,
                "first": {
                    "cpu_seconds": first_cpu_seconds,
                    "peak_worker_mib": first_peak / (1024 * 1024),
                    "storage": first_storage,
                    "wall_seconds": first_seconds,
                },
                "fixture": {
                    "sha256": source_sha256,
                    "size_bytes": source_size_bytes,
                },
                "hashes": {
                    "normalization": first_normalization.content_hash,
                    "quality": first_quality.content_hash,
                    "staging": first_staging.content_hash,
                },
                "mapped_fields": PREPARATION_SCALE_MAPPED_FIELDS,
                "parent_rss": {
                    "after_first_mib": parent_rss_after_first / (1024 * 1024),
                    "after_repeat_mib": parent_rss_after_repeat / (1024 * 1024),
                    "before_jobs_mib": parent_rss_before_jobs / (1024 * 1024),
                    "repeat_delta_mib": (
                        parent_rss_after_repeat - parent_rss_before_jobs
                    )
                    / (1024 * 1024),
                },
                "platform": platform.platform(),
                "prepared_snapshot_reused": True,
                "python": sys.version,
                "repeat": {
                    "cpu_seconds": repeat_cpu_seconds,
                    "peak_worker_mib": repeat_peak / (1024 * 1024),
                    "storage": repeat_storage,
                    "wall_seconds": repeat_seconds,
                },
                "revision": os.environ.get(
                    "IMPODO_PREPARATION_BENCHMARK_REVISION",
                    "unknown",
                ),
                "rows": effective_rows,
                "runtime_versions": {
                    name: _installed_version(name)
                    for name in ("duckdb", "openpyxl", "polars", "psutil")
                },
                "schema_version": 1,
                "source_reopened": False,
                "vectorization_report": vectorization_evidence(),
                "workers_exited": True,
                "workload": PREPARATION_SCALE_WORKLOAD,
            }
            print(
                PREPARATION_WORKER_BENCHMARK_PREFIX
                + json.dumps(result, separators=(",", ":"), sort_keys=True)
            )
        if effective_rows == COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
            self.assertLess(first_seconds, 120)
            self.assertLess(repeat_seconds, 120)
            self.assertLess(first_peak / (1024 * 1024), 900)
            self.assertLess(repeat_peak / (1024 * 1024), 900)

    def _prepare_related_product_bom_project_and_evidence(
        self,
        *,
        product_count: int,
        bom_line_count: int,
        column_count: int,
        mapped_field_count: int,
    ) -> tuple[str, str, int]:
        """Create two direct datasets with a real Product/BOM resolver."""

        if column_count < 4 or mapped_field_count < 4:
            self.fail("The related fixture requires at least four columns")
        benchmark_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        authoring = self.context.project_authoring.create(
            actor=self.context.actor,
            display_name="96k related Product/BOM preparation benchmark",
            source_mode="FILE",
            creation_request_id=str(_benchmark_uuid("related-project-create")),
            source_system_identity="Deterministic related CSV fixtures",
        )
        workspace_state = authoring.workspace_state

        fixture_specs = (
            (
                "products",
                product_count,
                _related_product_headers(column_count),
                lambda index: _related_product_values(index, column_count),
            ),
            (
                "bom-lines",
                bom_line_count,
                _related_bom_headers(column_count),
                lambda index: _related_bom_values(
                    index,
                    column_count,
                    product_count=product_count,
                ),
            ),
        )
        sources: list[SourceFile] = []
        catalogs: list[SourceFileCatalog] = []
        for fixture_name, row_count, headers, values_for in fixture_specs:
            source_path = self.root / f"{fixture_name}.csv"
            with source_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(headers)
                for index in range(row_count):
                    writer.writerow(values_for(index))
            file_id = str(_benchmark_uuid(f"{fixture_name}-source-file"))
            with source_path.open("rb") as stream:
                stored = self.artifacts.store_source(
                    authoring.data_version.data_version_id,
                    artifact_id=file_id,
                    suffix=".csv",
                    stream=stream,
                    maximum_bytes=MAX_SOURCE_BYTES,
                    chunk_bytes=CHUNK_BYTES,
                    validator=lambda _path: None,
                )
            source = SourceFile(
                file_id=file_id,
                display_name=f"{fixture_name}.csv",
                stored_name=stored.storage_key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                received_at=benchmark_now,
            )
            workspace_state = self.context.workspace_states.add_source_file(
                workspace_state.workspace_id,
                actor=self.context.actor,
                expected_revision=workspace_state.revision,
                source_file=source,
            )
            sources.append(source)
            catalogs.append(
                _related_catalog(
                    source,
                    row_count=row_count,
                    headers=headers,
                    values_for=values_for,
                    inspected_at=benchmark_now,
                    decimal_column=(2 if fixture_name == "products" else 3),
                )
            )

        registered_workspace_state = replace(
            workspace_state,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_scale",
            intended_models=("product.template", "mrp.bom.line"),
            status=WorkspaceStatus.REGISTERED,
            revision=workspace_state.revision + 1,
            updated_at=benchmark_now,
            registered_at=benchmark_now,
        )
        self.context.workspace_states.repository.save(
            registered_workspace_state,
            expected_revision=workspace_state.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="96k related Product/BOM preparation benchmark",
            actor=self.context.actor,
        )
        self.context.sources.sources.save_source_catalogs(
            registered_workspace_state.workspace_id,
            tuple(catalogs),
            actor=self.context.actor,
        )
        for source in sources:
            self.context.sources.confirm_source(
                registered_workspace_state.workspace_id,
                source.file_id,
                selected_table_keys=("csv",),
                warnings_acknowledged=False,
                actor=self.context.actor,
            )
        selection = self.context.sources.freeze_selection(
            registered_workspace_state.workspace_id,
            dataset_names={
                (sources[0].file_id, "csv"): "products",
                (sources[1].file_id, "csv"): "bom_lines",
            },
            actor=self.context.actor,
        )
        self.context.data_version_source_projection.accept_file_selection(
            registered_workspace_state.workspace_id,
            selection,
            actor=self.context.actor,
        )
        datasets = {item.name: item for item in selection.datasets}
        products = datasets["products"]
        bom_lines = datasets["bom_lines"]

        product_fields = tuple(
            ScalarFieldMapping(
                target_field=(
                    "default_code"
                    if index == 0
                    else "name"
                    if index == 1
                    else "list_price"
                    if index == 2
                    else f"x_scale_{index:02d}"
                ),
                source_column_key=products.columns[index].stable_key,
                transform=(
                    ScalarTransformPolicy(trim=True)
                    if 1 <= index <= PREPARATION_SCALE_EFFECT_FIELDS
                    else ScalarTransformPolicy()
                ),
                value_type="decimal" if index == 2 else "string",
                required=index in {0, 1},
            )
            for index in range(mapped_field_count)
        )
        bom_scalar_indexes = (0, 1, 3, *range(4, mapped_field_count))
        bom_fields = tuple(
            ScalarFieldMapping(
                target_field=(
                    "x_bom_reference"
                    if index == 0
                    else "x_line_reference"
                    if index == 1
                    else "product_qty"
                    if index == 3
                    else f"x_scale_{index:02d}"
                ),
                source_column_key=bom_lines.columns[index].stable_key,
                transform=(
                    ScalarTransformPolicy(trim=True)
                    if 1 <= index <= PREPARATION_SCALE_EFFECT_FIELDS
                    else ScalarTransformPolicy()
                ),
                value_type="decimal" if index == 3 else "string",
                required=index in {0, 1},
            )
            for index in bom_scalar_indexes
        )
        definition = MappingDefinition(
            mapping_id=str(_benchmark_uuid("mapping")),
            source_selection_hash=selection.content_hash,
            schema_hash="sha256:" + "5" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=products.dataset_id,
                    target_model="product.template",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(products.columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(products.columns[0].stable_key,),
                            target_fields=("default_code",),
                        ),
                    ),
                    fields=product_fields,
                    control_definitions=(
                        BusinessControlDefinition(
                            control_id="sales-price-total",
                            name="Sales price total",
                            target_field="list_price",
                            unit="EUR",
                        ),
                    ),
                    control_expectations=(
                        MappingControlExpectation(
                            control_id="sales-price-total",
                            expected_total=str(product_count),
                        ),
                    ),
                ),
                DatasetMapping(
                    dataset_id=bom_lines.dataset_id,
                    target_model="mrp.bom.line",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(
                        bom_lines.columns[0].stable_key,
                        bom_lines.columns[1].stable_key,
                    ),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(
                                bom_lines.columns[0].stable_key,
                                bom_lines.columns[1].stable_key,
                            ),
                            target_fields=(
                                "x_bom_reference",
                                "x_line_reference",
                            ),
                        ),
                    ),
                    fields=bom_fields,
                    relationships=(
                        RelationshipMapping(
                            target_field="product_id",
                            kind="many2one",
                            source_column_keys=(bom_lines.columns[2].stable_key,),
                            resolver=RelationshipResolver(
                                origin=ResolverOrigin.DATASET,
                                dataset_id=products.dataset_id,
                            ),
                            required=True,
                        ),
                    ),
                    control_definitions=(
                        BusinessControlDefinition(
                            control_id="component-quantity-total",
                            name="Component quantity total",
                            target_field="product_qty",
                            unit="unit",
                        ),
                    ),
                    control_expectations=(
                        MappingControlExpectation(
                            control_id="component-quantity-total",
                            expected_total=str(bom_line_count),
                        ),
                    ),
                ),
            ),
        )
        validation = MappingValidationResult(
            mapping_content_hash=definition.content_hash,
            source_selection_hash=definition.source_selection_hash,
            schema_hash=definition.schema_hash,
            status=MappingValidationStatus.VALID,
            issues=(),
            coverage=(),
            deferred_runtime_checks=(),
        )
        revision = MappingRevision(
            mapping_id=definition.mapping_id,
            version=1,
            parent_version=None,
            definition=definition,
            created_at=benchmark_now,
            created_by=self.context.actor.identity.display_name,
        )
        mapping_repository = self.context.mapping_workspace.mappings
        mapping_repository.save_mapping_revision(
            registered_workspace_state.workspace_id,
            revision,
            validation=validation,
            expected_parent_version=None,
            expected_working_draft_version=None,
            checked_draft=MappingWorkingDraft(
                mapping_id=definition.mapping_id,
                version=1,
                workspace_id=registered_workspace_state.workspace_id,
                base_mapping_version=revision.version,
                definition=definition,
                updated_at=benchmark_now,
                updated_by=self.context.actor.identity.display_name,
            ),
            actor=self.context.actor,
        )
        mapping_repository.save_mapping_submission(
            registered_workspace_state.workspace_id,
            MappingSubmission(
                submission_id=str(_benchmark_uuid("mapping-submission")),
                mapping_id=definition.mapping_id,
                version=revision.version,
                mapping_content_hash=definition.content_hash,
                validation_hash=validation.validation_hash,
                warning_acknowledgements=(),
                submitted_at=benchmark_now,
                submitted_by=self.context.actor.identity.display_name,
            ),
            actor=self.context.actor,
        )
        fixture_digest = sha256()
        for source in sources:
            fixture_digest.update(source.sha256.encode("ascii"))
            fixture_digest.update(str(source.size_bytes).encode("ascii"))
        return (
            registered_workspace_state.workspace_id,
            "sha256:" + fixture_digest.hexdigest(),
            sum(source.size_bytes for source in sources),
        )

    def _prepare_project_and_evidence(
        self,
        *,
        row_count: int,
        column_count: int,
        mapped_field_count: int,
        dirty: bool = False,
    ) -> tuple[str, str, int]:
        benchmark_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        authoring = self.context.project_authoring.create(
            actor=self.context.actor,
            display_name="100k complete preparation benchmark",
            source_mode="FILE",
            creation_request_id=str(_benchmark_uuid("project-create")),
            source_system_identity="Deterministic CSV fixture",
        )
        workspace_state = authoring.workspace_state
        source_path = self.root / "preparation-scale-input.csv"
        headers = _headers(column_count, PREPARATION_SCALE_WORKLOAD)
        with source_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(headers)
            for index in range(row_count):
                writer.writerow(
                    _source_values(
                        index,
                        column_count,
                        PREPARATION_SCALE_WORKLOAD,
                        dirty=dirty,
                    )
                )

        file_id = str(_benchmark_uuid("source-file"))
        with source_path.open("rb") as stream:
            stored = self.artifacts.store_source(
                authoring.data_version.data_version_id,
                artifact_id=file_id,
                suffix=".csv",
                stream=stream,
                maximum_bytes=MAX_SOURCE_BYTES,
                chunk_bytes=CHUNK_BYTES,
                validator=lambda _path: None,
            )
        source = SourceFile(
            file_id=file_id,
            display_name="preparation-scale.csv",
            stored_name=stored.storage_key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            received_at=benchmark_now,
        )
        workspace_state = self.context.workspace_states.add_source_file(
            workspace_state.workspace_id,
            actor=self.context.actor,
            expected_revision=workspace_state.revision,
            source_file=source,
        )
        now = benchmark_now
        registered_workspace_state = replace(
            workspace_state,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_scale",
            intended_models=(_target_model(PREPARATION_SCALE_WORKLOAD),),
            status=WorkspaceStatus.REGISTERED,
            revision=workspace_state.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        self.context.workspace_states.repository.save(
            registered_workspace_state,
            expected_revision=workspace_state.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="100k complete preparation benchmark",
            actor=self.context.actor,
        )

        catalog = _catalog(
            source,
            row_count=row_count,
            column_count=column_count,
            inspected_at=now,
        )
        self.context.sources.sources.save_source_catalogs(
            registered_workspace_state.workspace_id,
            (catalog,),
            actor=self.context.actor,
        )
        self.context.sources.confirm_source(
            registered_workspace_state.workspace_id,
            source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=self.context.actor,
        )
        selection = self.context.sources.freeze_selection(
            registered_workspace_state.workspace_id,
            dataset_names={
                (source.file_id, "csv"): (_dataset_name(PREPARATION_SCALE_WORKLOAD))
            },
            actor=self.context.actor,
        )
        self.context.data_version_source_projection.accept_file_selection(
            registered_workspace_state.workspace_id,
            selection,
            actor=self.context.actor,
        )
        dataset = selection.datasets[0]
        columns = dataset.columns
        fields = tuple(
            ScalarFieldMapping(
                target_field=(
                    _identity_target_fields(PREPARATION_SCALE_WORKLOAD)[0]
                    if index == 0
                    else (
                        "name"
                        if PREPARATION_SCALE_WORKLOAD in {"products", "customers"}
                        else "x_line_reference"
                    )
                    if index == 1
                    else (
                        "list_price"
                        if PREPARATION_SCALE_WORKLOAD == "products"
                        else "product_qty"
                        if PREPARATION_SCALE_WORKLOAD == "bom"
                        else "credit_limit"
                    )
                    if index == 2
                    else f"x_scale_{index:02d}"
                ),
                source_column_key=columns[index].stable_key,
                transform=(
                    ScalarTransformPolicy(trim=True)
                    if 1 <= index <= PREPARATION_SCALE_EFFECT_FIELDS
                    else ScalarTransformPolicy()
                ),
                value_type="decimal" if index == 2 else "string",
                required=index in {0, 1},
            )
            for index in range(mapped_field_count)
        )
        definition = MappingDefinition(
            mapping_id=str(_benchmark_uuid("mapping")),
            source_selection_hash=selection.content_hash,
            schema_hash="sha256:" + "5" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model=_target_model(PREPARATION_SCALE_WORKLOAD),
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(
                        (
                            columns[0].stable_key,
                            columns[1].stable_key,
                        )
                        if PREPARATION_SCALE_WORKLOAD == "bom"
                        else (columns[0].stable_key,)
                    ),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(
                                (
                                    columns[0].stable_key,
                                    columns[1].stable_key,
                                )
                                if PREPARATION_SCALE_WORKLOAD == "bom"
                                else (columns[0].stable_key,)
                            ),
                            target_fields=_identity_target_fields(
                                PREPARATION_SCALE_WORKLOAD
                            ),
                        ),
                    ),
                    fields=fields,
                    control_definitions=(
                        BusinessControlDefinition(
                            control_id="prepared-value-total",
                            name=(
                                "Sales price total"
                                if PREPARATION_SCALE_WORKLOAD == "products"
                                else "Component quantity total"
                                if PREPARATION_SCALE_WORKLOAD == "bom"
                                else "Credit limit total"
                            ),
                            target_field=(
                                "list_price"
                                if PREPARATION_SCALE_WORKLOAD == "products"
                                else "product_qty"
                                if PREPARATION_SCALE_WORKLOAD == "bom"
                                else "credit_limit"
                            ),
                            unit=(
                                "unit" if PREPARATION_SCALE_WORKLOAD == "bom" else "EUR"
                            ),
                        ),
                    ),
                    control_expectations=(
                        MappingControlExpectation(
                            control_id="prepared-value-total",
                            expected_total=str(row_count),
                        ),
                    ),
                ),
            ),
        )
        validation = MappingValidationResult(
            mapping_content_hash=definition.content_hash,
            source_selection_hash=definition.source_selection_hash,
            schema_hash=definition.schema_hash,
            status=MappingValidationStatus.VALID,
            issues=(),
            coverage=(),
            deferred_runtime_checks=(),
        )
        revision = MappingRevision(
            mapping_id=definition.mapping_id,
            version=1,
            parent_version=None,
            definition=definition,
            created_at=now,
            created_by=self.context.actor.identity.display_name,
        )
        mapping_repository = self.context.mapping_workspace.mappings
        mapping_repository.save_mapping_revision(
            registered_workspace_state.workspace_id,
            revision,
            validation=validation,
            expected_parent_version=None,
            expected_working_draft_version=None,
            checked_draft=MappingWorkingDraft(
                mapping_id=definition.mapping_id,
                version=1,
                workspace_id=registered_workspace_state.workspace_id,
                base_mapping_version=revision.version,
                definition=definition,
                updated_at=now,
                updated_by=self.context.actor.identity.display_name,
            ),
            actor=self.context.actor,
        )
        mapping_repository.save_mapping_submission(
            registered_workspace_state.workspace_id,
            MappingSubmission(
                submission_id=str(_benchmark_uuid("mapping-submission")),
                mapping_id=definition.mapping_id,
                version=revision.version,
                mapping_content_hash=definition.content_hash,
                validation_hash=validation.validation_hash,
                warning_acknowledgements=(),
                submitted_at=now,
                submitted_by=self.context.actor.identity.display_name,
            ),
            actor=self.context.actor,
        )
        return registered_workspace_state.workspace_id, stored.sha256, stored.size_bytes

    def _enable_advanced_coverage(self, workspace_id: str) -> None:
        """Install deterministic Slice 6 inputs for the supported-scale probe."""

        selection = self.context.preparation.sources.get_source_selection(workspace_id)
        revision = self.context.preparation.mappings.get_mapping_revision(workspace_id)
        assert selection is not None
        assert revision is not None
        dataset_mapping = revision.definition.datasets[0]
        dataset_name = selection.datasets[0].name
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        scope = CoverageScopeRevision(
            scope_id=str(_benchmark_uuid("coverage-scope")),
            workspace_id=workspace_id,
            version=1,
            parent_version=None,
            source_selection_hash=selection.content_hash,
            declarations=tuple(
                CoverageDeclaration(
                    family=family,
                    applicability=(
                        CoverageApplicability.APPLICABLE
                        if family
                        in {
                            CoverageFamily.TC_09,
                            CoverageFamily.TC_14,
                            CoverageFamily.TC_15,
                            CoverageFamily.TC_20,
                            CoverageFamily.TC_23,
                        }
                        else CoverageApplicability.INAPPLICABLE
                    ),
                    rationale=(
                        "Included in the deterministic Slice 6 scale fixture."
                        if family
                        in {
                            CoverageFamily.TC_09,
                            CoverageFamily.TC_14,
                            CoverageFamily.TC_15,
                            CoverageFamily.TC_20,
                            CoverageFamily.TC_23,
                        }
                        else "Not required by this deterministic scale fixture."
                    ),
                    datasets=(dataset_name,)
                    if family
                    in {
                        CoverageFamily.TC_09,
                        CoverageFamily.TC_14,
                        CoverageFamily.TC_15,
                        CoverageFamily.TC_20,
                        CoverageFamily.TC_23,
                    }
                    else (),
                )
                for family in CoverageFamily
            ),
            approved_by=self.context.actor.identity,
            approved_at=now,
        )
        reference_entries = tuple(
            sorted(
                (
                    ReferenceEntry(
                        key=(f"value-03-{index:02d}",),
                        values={"approved_value": f"value-03-{index:02d}"},
                    )
                    for index in range(100)
                ),
                key=lambda item: item.key_hash,
            )
        )
        reference = ReferenceDataSet(
            reference_id=str(_benchmark_uuid("reference-bundle")),
            version=1,
            name="Scale approved values",
            key_fields=("value",),
            value_kinds={"approved_value": ReferenceValueKind.BUSINESS_KEY},
            entries=reference_entries,
            owner="Performance tester",
            classification="internal",
            effective_label="Deterministic scale fixture",
        )
        bundle = ReferenceBundle(workspace_id=workspace_id, datasets=(reference,))
        target_fields = tuple(
            sorted(item.target_field for item in dataset_mapping.fields)
        )
        policy = ResolutionPolicy(
            policy_id=str(_benchmark_uuid("resolution-policy")),
            workspace_id=workspace_id,
            version=1,
            parent_version=None,
            coverage_scope_hash=scope.content_hash,
            mapping_hash=revision.definition.content_hash,
            schema_hash=revision.definition.schema_hash,
            reference_bundle_hash=bundle.content_hash,
            rules=(
                ResolutionRule(
                    rule_id=str(_benchmark_uuid("resolution-rule")),
                    dataset=dataset_name,
                    blocking_fields=(target_fields[0],),
                    comparison_fields=(
                        FuzzyComparisonField(
                            field=target_fields[1],
                            algorithm=SimilarityAlgorithm.NORMALIZED_LEVENSHTEIN,
                        ),
                    ),
                    candidate_threshold="0.95",
                    survivor_fields=target_fields,
                    correctable_fields=(target_fields[1],),
                ),
            ),
        )
        advanced = self.context.resolution.repository
        advanced.save_coverage_scope(
            workspace_id,
            scope,
            expected_parent_version=None,
            actor=self.context.actor,
        )
        advanced.save_reference_bundle(
            workspace_id,
            bundle,
            actor=self.context.actor,
        )
        advanced.save_resolution_policy(
            workspace_id,
            policy,
            expected_parent_version=None,
            actor=self.context.actor,
        )
        base = default_quality_ruleset(
            workspace_id=workspace_id,
            mapping_hash=revision.definition.content_hash,
            schema_hash=revision.definition.schema_hash,
            datasets=(dataset_name,),
        )
        approved_code = QualityRule(
            rule_id="sha256:" + "8" * 64,
            dataset=dataset_name,
            family=QualityRuleFamily.APPROVED_CODE_LIST,
            name="Approved scale values",
            explanation="Require a value from the deterministic governed list.",
            input_fields=("x_scale_03",),
            parameters={
                "reference_id": reference.reference_id,
                "reference_content_hash": reference.content_hash,
            },
            outcome=QualityOutcomePolicy.QUARANTINE,
            owner_role=QualityOwnerRole.DATA_MANAGER,
            source=QualityRuleSource.SCOPE_APPROVED,
        )
        count_boundary = QualityRule(
            rule_id="sha256:" + "9" * 64,
            dataset=dataset_name,
            family=QualityRuleFamily.METRIC_BOUNDARY,
            name="Expected scale population",
            explanation="Require the approved deterministic row count.",
            input_fields=(),
            parameters={
                "metric": "count",
                "minimum": str(PREPARATION_SCALE_ROWS),
                "maximum": str(PREPARATION_SCALE_ROWS),
            },
            outcome=QualityOutcomePolicy.BLOCK,
            owner_role=QualityOwnerRole.DATA_MANAGER,
            source=QualityRuleSource.SCOPE_APPROVED,
        )
        ruleset = QualityRuleSet(
            ruleset_id=base.ruleset_id,
            workspace_id=workspace_id,
            version=1,
            parent_version=None,
            mapping_hash=base.mapping_hash,
            schema_hash=base.schema_hash,
            rules=tuple(
                sorted(
                    (*base.rules, approved_code, count_boundary),
                    key=lambda item: item.rule_id,
                )
            ),
            coverage_scope_hash=scope.content_hash,
            reference_bundle_hash=bundle.content_hash,
        )
        self.context.quality.publish_ruleset(
            workspace_id,
            ruleset,
            actor=self.context.actor,
        )


class BoundedPreparationParityTests(unittest.TestCase):
    """Prove the direct durable path preserves the materialized Stage-E bytes."""

    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.app = create_local_app(self.root)
        self.context = self.app.state.context
        self.artifacts = self.context.artifacts

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_session_matches_materialized_canonical_evidence(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=37,
                column_count=8,
                mapped_field_count=5,
                dirty=True,
            )
        )
        workspace_state = self.context.preparation.workspaces.get(workspace_id)
        revision = self.context.preparation.mappings.get_mapping_revision(workspace_id)
        physical = self.context.preparation.sources.get_source_selection(workspace_id)
        effective = self.context.preparation.sources.get_mapping_source_selection(
            workspace_id
        )
        self.assertIsNotNone(revision)
        self.assertIsNotNone(physical)
        self.assertIsNotNone(effective)
        assert revision is not None
        assert physical is not None
        assert effective is not None
        with patch(
            "impodo.domain.staging.evaluator._compile_reference_indexes",
            create=True,
            return_value={},
        ):
            materialized_impacts = []
            materialized = preparation_module.stage_browser_mapping(
                workspace_state,
                revision.definition,
                physical,
                effective,
                None,
                self.context.preparation.sources.get_source_catalogs(workspace_id),
                self.artifacts,
                collect_transformation_impact=True,
                transformation_detail_limit=0,
                transformation_impact_sink=materialized_impacts.append,
            )

        from impodo.adapters.duckdb import (
            preparation_session_repository,
            quality_repository,
        )

        quality_transport_batches: dict[str, list[int]] = {
            "quality_rows": [],
            "source_entries": [],
            "source_links": [],
        }
        original_quality_batches = quality_repository.iter_encoded_json_batches

        def count_quality_batches(*args, **kwargs):
            for batch in original_quality_batches(*args, **kwargs):
                keys = set(json.loads(batch.payload)[0])
                if "effective_disposition" in keys:
                    family = "quality_rows"
                elif "physical_dataset_id" in keys:
                    family = "source_entries"
                elif "accounting_ordinal" in keys:
                    family = "source_links"
                else:
                    raise AssertionError("Unexpected quality transport shape")
                quality_transport_batches[family].append(batch.row_count)
                yield batch

        preparation_transport_batches: dict[str, list[int]] = {
            "canonical_rows": [],
            "identities": [],
            "lineage": [],
            "physical_rows": [],
            "impacts": [],
        }
        original_impact_batches = (
            preparation_session_repository.iter_encoded_json_batches
        )

        def count_preparation_batches(*args, **kwargs):
            for batch in original_impact_batches(*args, **kwargs):
                keys = set(json.loads(batch.payload)[0])
                if "row_json" in keys:
                    family = "canonical_rows"
                elif "impact_json" in keys:
                    family = "impacts"
                elif "identity_hash" in keys:
                    family = "identities"
                elif "physical_source_row" in keys:
                    family = "lineage"
                elif keys == {"physical_dataset_id", "source_row"}:
                    family = "physical_rows"
                else:
                    raise AssertionError("Unexpected preparation transport shape")
                preparation_transport_batches[family].append(batch.row_count)
                yield batch

        with (
            patch.object(
                self.context.preparation.staging,
                "get_canonical_staging_run",
                side_effect=AssertionError(
                    "bounded preparation must not reload every canonical row"
                ),
            ),
            patch(
                "impodo.application.normalization_service.evaluate_normalization",
                side_effect=AssertionError(
                    "bounded preparation must not materialize normalization"
                ),
            ),
            patch(
                "impodo.adapters.duckdb.quality_repository.DUCKDB_JSON_BATCH_MAX_BYTES",
                2_000,
            ),
            patch.object(
                quality_repository,
                "iter_encoded_json_batches",
                count_quality_batches,
            ),
            patch.object(
                preparation_session_repository,
                "iter_encoded_json_batches",
                count_preparation_batches,
            ),
            patch.object(
                preparation_session_repository,
                "DUCKDB_JSON_BATCH_MAX_BYTES",
                2_000,
            ),
            patch.object(
                preparation_session_repository,
                "DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES",
                10_000,
            ),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )
        self.assertEqual(
            sum(quality_transport_batches["quality_rows"]),
            2,
        )
        self.assertEqual(quality_transport_batches["source_entries"], [])
        self.assertEqual(quality_transport_batches["source_links"], [])
        canonical_batches = preparation_transport_batches["canonical_rows"]
        self.assertEqual(canonical_batches, [])
        for family in ("identities", "lineage", "physical_rows"):
            self.assertEqual(preparation_transport_batches[family], [])
        impact_batches = preparation_transport_batches["impacts"]
        self.assertEqual(impact_batches, [])

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            native_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*)
                       FROM canonical_prepared_projection
                      WHERE CAST(json_extract(
                          projection_json,
                          '$.set_based_projection'
                      ) AS BOOLEAN))
                """
            ).fetchone()
        self.assertEqual(native_counts, (37, 1))

        summary = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        stored = self.context.preparation.staging.get_canonical_staging_run(
            workspace_id,
            summary.run_id,
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(summary.content_hash, materialized.canonical_run.content_hash)
        self.assertEqual(stored.to_json(), materialized.canonical_run.to_json())
        ruleset = self.context.preparation.quality.current_ruleset(workspace_id)
        quality_summary = self.context.preparation.quality.current_summary(workspace_id)
        self.assertIsNotNone(ruleset)
        self.assertIsNotNone(quality_summary)
        assert ruleset is not None
        assert quality_summary is not None
        expected_quality = evaluate_quality(
            workspace_state=workspace_state,
            staging=materialized.canonical_run,
            physical_rows=dict(materialized.physical_rows),
            ruleset=ruleset,
            published_staging_content_hash=summary.content_hash,
        )
        self.assertEqual(quality_summary.content_hash, expected_quality.content_hash)
        stored_quality = self.context.preparation.quality.current_run(workspace_id)
        self.assertIsNotNone(stored_quality)
        assert stored_quality is not None
        self.assertEqual(stored_quality.to_json(), expected_quality.to_json())
        quality_page = self.context.preparation.quality.quality.get_quality_review_page(
            workspace_id,
            quality_summary.run_id,
            page_size=17,
        )
        self.assertEqual(quality_page.matching_count, 37)
        self.assertEqual(
            tuple(item.row for item in quality_page.items),
            expected_quality.row_results[:17],
        )
        quarantined_page = (
            self.context.preparation.quality.quality.get_quality_review_page(
                workspace_id,
                quality_summary.run_id,
                status="quarantined",
            )
        )
        self.assertEqual(quarantined_page.matching_count, 2)
        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.quality.quality._connect(
            database_path
        ) as connection:
            sparse_storage = connection.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(LENGTH(row_json)), 0)
                       FROM quality_row_result),
                    (SELECT COALESCE(SUM(LENGTH(entry_json)), 0)
                       FROM source_accounting_entry),
                    (SELECT COUNT(*) FROM quality_evidence_projection),
                    (SELECT COUNT(*) FROM quality_row_result),
                    (SELECT COUNT(*) FROM source_accounting_entry)
                """
            ).fetchone()
        self.assertEqual(sparse_storage, (0, 0, 1, 2, 0))
        datasets_by_id = {item.dataset_id: item for item in effective.datasets}
        mappings = {
            datasets_by_id[item.dataset_id].name: item
            for item in revision.definition.datasets
        }
        expected_normalization = evaluate_normalization(
            workspace_state=workspace_state,
            staging=materialized.canonical_run,
            quality=expected_quality,
            mappings=mappings,
            candidates=(
                NormalizationCandidate(
                    dataset=item.dataset,
                    source_row=item.source_row,
                    source_label=item.source_column,
                    target_field=item.target_field,
                    raw_display=item.raw_value,
                    proposed_display=item.proposed_value,
                    rules=item.rules,
                    outcome=item.outcome,
                    message=item.message,
                )
                for item in materialized_impacts
            ),
            published_staging_content_hash=summary.content_hash,
            published_quality_content_hash=quality_summary.content_hash,
        )
        normalization_summary = self.context.preparation.normalization.current_summary(
            workspace_id
        )
        self.assertIsNotNone(normalization_summary)
        assert normalization_summary is not None
        self.assertEqual(
            normalization_summary.content_hash,
            expected_normalization.content_hash,
        )
        stored_normalization = self.context.preparation.normalization.repository.get_normalization_evaluation(
            workspace_id,
            normalization_summary.run_id,
        )
        self.assertIsNotNone(stored_normalization)
        assert stored_normalization is not None
        self.assertEqual(
            stored_normalization.to_json(),
            expected_normalization.to_json(),
        )
        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            sessions = connection.execute(
                """
                SELECT status, staged_row_count, canonical_row_count,
                       impact_row_count
                  FROM preparation_session
                """
            ).fetchall()
            temporary_rows = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM preparation_lineage),
                    (SELECT COUNT(*) FROM preparation_impact_row),
                    (SELECT COUNT(*) FROM preparation_direct_identity),
                    (SELECT COUNT(*)
                       FROM preparation_normalization_group_seed),
                    (SELECT COUNT(*)
                       FROM preparation_normalization_finding),
                    (SELECT COALESCE(SUM(LENGTH(row_json)), 0)
                       FROM canonical_staging_row)
                """
            ).fetchone()
        self.assertEqual(sessions, [("PUBLISHED", 37, 37, 37)])
        self.assertEqual(temporary_rows, (0, 0, 0, 0, 0, 0))

        repeated = self.context.preparation.prepare(
            workspace_id,
            actor=self.context.actor,
        )
        repeated_staging = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        self.assertIsNotNone(repeated_staging)
        assert repeated_staging is not None
        self.assertEqual(repeated_staging.run_id, summary.run_id)
        self.assertEqual(
            repeated.content_hash,
            normalization_summary.content_hash,
        )
        with self.context.preparation.staging._connect(database_path) as connection:
            durable_runs = connection.execute(
                """
                SELECT status, COUNT(*)
                  FROM canonical_staging_run
                 GROUP BY status
                 ORDER BY status
                """
            ).fetchall()
            pending_rows = connection.execute(
                """
                SELECT COUNT(*)
                  FROM canonical_staging_row AS row
                  JOIN canonical_staging_run AS run ON run.run_id = row.run_id
                 WHERE run.status = 'PENDING'
                """
            ).fetchone()
        self.assertEqual(durable_runs, [("PUBLISHED", 1)])
        self.assertEqual(pending_rows, (0,))

    def test_quality_row_transport_failure_rolls_back_pending_evidence(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import quality_repository

        original_batches = quality_repository.iter_encoded_json_batches

        def fail_after_first_batch(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                yield batch
                raise RuntimeError("injected quality transport failure")

        with (
            patch.object(
                quality_repository,
                "iter_encoded_json_batches",
                fail_after_first_batch,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected quality transport failure",
            ),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM quality_row_result),
                    (SELECT COUNT(*) FROM quality_run),
                    (SELECT COUNT(*) FROM quality_current)
                """
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0))

    def test_native_projection_failure_cleans_pending_preparation(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        with (
            patch(
                "impodo.adapters.duckdb.preparation_session_repository."
                "append_clean_native_projection",
                side_effect=RuntimeError("injected native projection failure"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected native projection failure",
            ),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            session = connection.execute(
                "SELECT status, failure_code FROM preparation_session"
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*) FROM canonical_staging_run),
                    (SELECT COUNT(*) FROM canonical_staging_current)
                """
            ).fetchone()
        self.assertEqual(session, ("FAILED", "BOUNDED_PREPARATION_FAILED"))
        self.assertEqual(counts, (0, 0, 0))

    def test_large_canonical_rows_use_scalar_fallback(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import preparation_session_repository

        original_batches = preparation_session_repository.iter_encoded_json_batches

        def reject_canonical_json_batches(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                if '"row_json"' in batch.payload:
                    raise AssertionError(
                        "large canonical rows must bypass JSON transport"
                    )
                yield batch

        with (
            patch(
                "impodo.adapters.duckdb.preparation_session_repository."
                "supports_clean_native_projection",
                return_value=False,
            ),
            patch.object(
                preparation_session_repository,
                "_CANONICAL_ROW_SCALAR_FALLBACK_BYTES",
                1,
            ),
            patch.object(
                preparation_session_repository,
                "iter_encoded_json_batches",
                reject_canonical_json_batches,
            ),
        ):
            summary = self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        staging = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        self.assertIsNotNone(staging)
        assert staging is not None
        self.assertEqual(staging.total_rows, 5)
        self.assertEqual(summary.workspace_id, workspace_id)

    def test_native_projection_miss_above_python_limit_fails_closed(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )

        with (
            patch(
                "impodo.adapters.duckdb.preparation_session_repository."
                "supports_clean_native_projection",
                return_value=False,
            ),
            patch.object(
                bounded_preparation_module,
                "BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT",
                4,
            ),
            self.assertRaisesRegex(
                ReadinessError,
                "requires the bounded compatibility checker",
            ),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            session = connection.execute(
                "SELECT status, failure_code FROM preparation_session"
            ).fetchone()
            current = connection.execute(
                "SELECT COUNT(*) FROM canonical_staging_current"
            ).fetchone()
        self.assertEqual(session, ("FAILED", "BOUNDED_PREPARATION_FAILED"))
        self.assertEqual(current, (0,))

    def test_sparse_quality_manifest_failure_rolls_back_quality(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb.quality_repository import QualityRepository

        original_insert = QualityRepository._insert_quality_evidence

        def fail_after_sparse_manifest(connection, run_id, run):
            original_insert(connection, run_id, run)
            raise RuntimeError("injected sparse quality failure")

        with (
            patch.object(
                QualityRepository,
                "_insert_quality_evidence",
                staticmethod(fail_after_sparse_manifest),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected sparse quality failure",
            ),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM quality_row_result),
                    (SELECT COUNT(*) FROM source_accounting_entry),
                    (SELECT COUNT(*) FROM source_accounting_link),
                    (SELECT COUNT(*) FROM quality_evidence_projection),
                    (SELECT COUNT(*) FROM quality_run),
                    (SELECT COUNT(*) FROM quality_current)
                """
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0, 0, 0, 0))

    def test_normalization_transport_failure_rolls_back_pending_evidence(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        original_copy = (
            self.context.preparation.sessions._copy_normalization_effects_to_run
        )

        def fail_after_copy(*args, **kwargs):
            original_copy(*args, **kwargs)
            raise RuntimeError("injected transport failure")

        with (
            patch.object(
                self.context.preparation.sessions,
                "_copy_normalization_effects_to_run",
                side_effect=fail_after_copy,
            ),
            self.assertRaisesRegex(RuntimeError, "injected transport failure"),
        ):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM normalization_effect),
                    (SELECT COUNT(*) FROM normalization_run),
                    (SELECT COUNT(*) FROM normalization_current),
                    (SELECT COUNT(*)
                       FROM preparation_normalization_group_seed),
                    (SELECT COUNT(*)
                       FROM preparation_normalization_finding)
                """
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0, 0, 0))

    def test_normalization_effects_are_constructed_once_and_reused_directly(
        self,
    ) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=17,
                column_count=8,
                mapped_field_count=5,
            )
        )
        constructed = 0
        original_effect = (
            bounded_normalization_module._BoundedNormalizationEffects._effect
        )

        def count_constructed(*args, **kwargs):
            nonlocal constructed
            built = original_effect(*args, **kwargs)
            if built is not None:
                constructed += 1
            return built

        with (
            patch.object(
                bounded_normalization_module._BoundedNormalizationEffects,
                "_effect",
                count_constructed,
            ),
            patch.object(
                self.context.preparation.sessions,
                "mark_published",
            ),
        ):
            summary = self.context.preparation.prepare(
                workspace_id,
                actor=self.context.actor,
            )

        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            session_id = str(
                connection.execute(
                    "SELECT run_id FROM canonical_staging_current"
                ).fetchone()[0]
            )
            effects = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT effect_json
                      FROM normalization_effect
                     WHERE run_id = ?
                     ORDER BY effect_id
                    """,
                    [session_id],
                ).fetchall()
            )
            changed = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT row_id)
                      FROM normalization_effect
                     WHERE run_id = ? AND eligible
                    """,
                    [session_id],
                ).fetchone()[0]
            )

        self.assertEqual(summary.run_id, session_id)
        self.assertEqual(constructed, len(effects))
        self.assertEqual(summary.changed_record_count, changed)

    def test_direct_publication_failure_preserves_current_run(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=17,
                column_count=8,
                mapped_field_count=5,
            )
        )
        self.context.preparation.prepare(
            workspace_id,
            actor=self.context.actor,
        )
        current = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        self.assertIsNotNone(current)
        assert current is not None

        with patch.object(
            self.context.preparation.staging,
            "publish_canonical_staging",
            side_effect=RuntimeError("injected publication failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected publication failure",
            ):
                self.context.preparation.prepare(
                    workspace_id,
                    actor=self.context.actor,
                )

        unchanged = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        self.assertIsNotNone(unchanged)
        assert unchanged is not None
        self.assertEqual(unchanged.run_id, current.run_id)
        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            state = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_run WHERE status = 'PENDING'),
                    (SELECT COUNT(*) FROM canonical_staging_run WHERE status = 'PUBLISHED'),
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*) FROM preparation_session WHERE status = 'FAILED')
                """
            ).fetchone()
        self.assertEqual(state, (0, 1, 17, 1))

    def test_direct_promotion_rolls_back_atomically(self) -> None:
        workspace_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=13,
                column_count=8,
                mapped_field_count=5,
            )
        )
        with patch.object(
            self.context.preparation.staging,
            "_insert_workspace_audit",
            side_effect=RuntimeError("injected commit failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected commit failure",
            ):
                self.context.preparation.prepare(
                    workspace_id,
                    actor=self.context.actor,
                )

        self.assertIsNone(
            self.context.preparation.staging.get_current_staging_summary(workspace_id)
        )
        database_path = self.context.preparation.staging.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            state = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_current),
                    (SELECT COUNT(*) FROM canonical_staging_run),
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*) FROM preparation_session WHERE status = 'FAILED')
                """
            ).fetchone()
        self.assertEqual(state, (0, 0, 0, 1))


def _headers(column_count: int, workload: str) -> tuple[str, ...]:
    return (
        "product_reference"
        if workload == "products"
        else "bom_reference"
        if workload == "bom"
        else "customer_reference",
        "line_reference" if workload == "bom" else "name",
        "list_price"
        if workload == "products"
        else "quantity"
        if workload == "bom"
        else "credit_limit",
        *(f"field_{index:02d}" for index in range(3, column_count)),
    )


def _related_product_headers(column_count: int) -> tuple[str, ...]:
    return _headers(column_count, "products")


def _related_product_values(index: int, column_count: int) -> tuple[str, ...]:
    return _source_values(index, column_count, "products")


def _related_bom_headers(column_count: int) -> tuple[str, ...]:
    return (
        "bom_reference",
        "line_reference",
        "product_reference",
        "quantity",
        *(f"field_{index:02d}" for index in range(4, column_count)),
    )


def _related_bom_values(
    index: int,
    column_count: int,
    *,
    product_count: int,
) -> tuple[str, ...]:
    return (
        f"BOM{index // 5:06d}",
        f" L{index:06d} ",
        f"P{index % product_count:06d}",
        " 1 " if PREPARATION_SCALE_EFFECT_FIELDS >= 3 else "1",
        *(
            (
                f" value-{column:02d}-{index % 100:02d} "
                if column <= PREPARATION_SCALE_EFFECT_FIELDS
                else f"value-{column:02d}-{index % 100:02d}"
            )
            for column in range(4, column_count)
        ),
    )


def _related_catalog(
    source: SourceFile,
    *,
    row_count: int,
    headers: tuple[str, ...],
    values_for: Callable[[int], tuple[str, ...]],
    inspected_at: datetime,
    decimal_column: int,
) -> SourceFileCatalog:
    profiles = tuple(
        SourceColumnProfile(
            ordinal=index + 1,
            name=name,
            candidate_type="decimal" if index == decimal_column else "string",
            null_count=0,
            non_null_count=row_count,
            distinct_count=(
                1
                if index == decimal_column
                else row_count
                if index < 3
                else min(row_count, 100)
            ),
            distinct_count_is_exact=True,
            duplicate_count=(
                row_count - 1
                if index == decimal_column
                else 0
                if index < 3
                else max(0, row_count - 100)
            ),
            minimum="1" if index == decimal_column else None,
            maximum="1" if index == decimal_column else None,
            minimum_length=1 if index == decimal_column else None,
            maximum_length=1 if index == decimal_column else None,
        )
        for index, name in enumerate(headers)
    )
    return SourceFileCatalog(
        contract_version=2,
        file_id=source.file_id,
        display_name=source.display_name,
        source_sha256=source.sha256,
        source_size_bytes=source.size_bytes,
        format="CSV",
        inspected_at=inspected_at,
        encoding="utf-8",
        delimiter=",",
        tables=(
            SourceTableCatalog(
                table_key="csv",
                name=source.display_name.removesuffix(".csv"),
                kind="CSV",
                hidden=False,
                header_row=1,
                row_count=row_count,
                column_count=len(headers),
                columns=profiles,
                preview_rows=tuple(
                    values_for(index) for index in range(min(5, row_count))
                ),
            ),
        ),
    )


def _source_values(
    index: int,
    column_count: int,
    workload: str,
    *,
    dirty: bool = False,
) -> tuple[str, ...]:
    identity_index = index - 1 if dirty and index % 100 == 1 else index
    return (
        (
            f"P{identity_index:06d}"
            if workload == "products"
            else f"BOM{identity_index // 10:06d}"
            if workload == "bom"
            else f"C{identity_index:06d}"
        ),
        (
            f" L{identity_index:06d} "
            if workload == "bom"
            else f" {workload.removesuffix('s').title()} {index:06d} "
        ),
        " 1 " if PREPARATION_SCALE_EFFECT_FIELDS >= 2 else "1",
        *(
            (
                f" value-{column:02d}-{index % 100:02d} "
                if column <= PREPARATION_SCALE_EFFECT_FIELDS
                else f"value-{column:02d}-{index % 100:02d}"
            )
            for column in range(3, column_count)
        ),
    )


def _dataset_name(workload: str) -> str:
    return {
        "bom": "bom_lines",
        "customers": "customers",
        "products": "products",
    }[workload]


def _target_model(workload: str) -> str:
    return {
        "bom": "mrp.bom.line",
        "customers": "res.partner",
        "products": "product.template",
    }[workload]


def _identity_target_fields(workload: str) -> tuple[str, ...]:
    return {
        "bom": ("x_bom_reference", "x_line_reference"),
        "customers": ("ref",),
        "products": ("default_code",),
    }[workload]


def _catalog(
    source: SourceFile,
    *,
    row_count: int,
    column_count: int,
    inspected_at: datetime,
) -> SourceFileCatalog:
    headers = _headers(column_count, PREPARATION_SCALE_WORKLOAD)
    profiles = tuple(
        SourceColumnProfile(
            ordinal=index + 1,
            name=name,
            candidate_type="decimal" if index == 2 else "string",
            null_count=0,
            non_null_count=row_count,
            distinct_count=(row_count if index in {0, 1} else 1 if index == 2 else 100),
            distinct_count_is_exact=True,
            duplicate_count=(
                0
                if index in {0, 1}
                else row_count - 1
                if index == 2
                else max(0, row_count - 100)
            ),
            minimum="1" if index == 2 else None,
            maximum="1" if index == 2 else None,
            minimum_length=1 if index == 2 else None,
            maximum_length=1 if index == 2 else None,
        )
        for index, name in enumerate(headers)
    )
    return SourceFileCatalog(
        contract_version=2,
        file_id=source.file_id,
        display_name=source.display_name,
        source_sha256=source.sha256,
        source_size_bytes=source.size_bytes,
        format="CSV",
        inspected_at=inspected_at,
        encoding="utf-8",
        delimiter=",",
        tables=(
            SourceTableCatalog(
                table_key="csv",
                name="contacts",
                kind="CSV",
                hidden=False,
                header_row=1,
                row_count=row_count,
                column_count=column_count,
                columns=profiles,
                preview_rows=tuple(
                    _source_values(index, column_count, PREPARATION_SCALE_WORKLOAD)
                    for index in range(min(5, row_count))
                ),
            ),
        ),
    )


def _installed_version(distribution: str) -> str:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return "missing"


if __name__ == "__main__":
    unittest.main()
