from __future__ import annotations

import csv
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter, process_time, sleep
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.application import normalization_service as normalization_module
from impodo.application import bounded_preparation as bounded_preparation_module
from impodo.application import preparation_service as preparation_module
from impodo.application import quality_service as quality_module
from impodo.application.bounded_preparation import BOUNDED_SOURCE_BATCH_SIZE
from impodo.artifacts import LocalArtifactStore
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
from impodo.domain.mapping.artifacts import MappingRevision, MappingSubmission
from impodo.domain.mapping.contracts import (
    BusinessControlTotal,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingTargetMode,
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
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.intake import CHUNK_BYTES, MAX_SOURCE_BYTES
from impodo.projects import (
    OdooConnectionMode,
    ProjectStatus,
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
from impodo.preparation_jobs import PreparationJobStatus
from impodo.value_rules import ScalarTransformPolicy
from impodo.web.app import create_local_app
from impodo.workspace_contracts import MappingWorkingDraft


ROOT = Path(__file__).resolve().parents[1]
PREPARATION_SCALE_ROWS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_ROWS", "100000")
)
PREPARATION_SCALE_COLUMNS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_COLUMNS", "30")
)
PREPARATION_SCALE_MAPPED_FIELDS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_MAPPED_FIELDS", "20")
)
PREPARATION_SCALE_WORKLOAD = os.environ.get(
    "IMPODO_PREPARATION_SCALE_WORKLOAD",
    "products",
).casefold()
PREPARATION_SCALE_DIRTY = (
    os.environ.get("IMPODO_PREPARATION_SCALE_DIRTY") == "1"
)
PREPARATION_BENCHMARK_PREFIX = "IMPODO_PREPARATION_BENCHMARK_JSON="


class _PeakWorkingSetSampler:
    """Sample cross-platform process working set during the timed operation."""

    def __init__(self, process, *, interval_seconds: float = 0.01) -> None:
        self._process = process
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._sample_until_stopped, daemon=True)
        self.peak_bytes = 0

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
        memory = self._process.memory_info()
        working_set = getattr(memory, "peak_wset", memory.rss)
        self.peak_bytes = max(self.peak_bytes, working_set)


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
        self.artifacts = LocalArtifactStore(self.root)
        self.app = create_local_app(
            self.root,
            artifact_store=self.artifacts,
        )
        self.context = self.app.state.context

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_preparation_workflow(self) -> None:
        import psutil

        if PREPARATION_SCALE_COLUMNS < PREPARATION_SCALE_MAPPED_FIELDS:
            self.fail("The source fixture must include every mapped field")
        if PREPARATION_SCALE_MAPPED_FIELDS < 3:
            self.fail("The benchmark requires identity, changed, and numeric fields")
        if PREPARATION_SCALE_WORKLOAD not in {"products", "bom"}:
            self.fail("The benchmark workload must be 'products' or 'bom'")

        fixture_started = perf_counter()
        fixture_cpu_started = process_time()
        process = psutil.Process()
        with _PeakWorkingSetSampler(process) as fixture_memory_sampler:
            project_id, source_sha256, source_size_bytes = (
                self._prepare_project_and_evidence(
                    row_count=PREPARATION_SCALE_ROWS,
                    column_count=PREPARATION_SCALE_COLUMNS,
                    mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
                    dirty=PREPARATION_SCALE_DIRTY,
                )
            )
        if os.environ.get("IMPODO_PREPARATION_ADVANCED") == "1":
            self._enable_advanced_coverage(project_id)
        fixture_seconds = perf_counter() - fixture_started
        fixture_cpu_seconds = process_time() - fixture_cpu_started
        fixture_peak_mib = fixture_memory_sampler.peak_bytes / (1024 * 1024)
        fixture_ending_mib = process.memory_info().rss / (1024 * 1024)

        phase_wall_seconds: dict[str, float] = {}
        phase_cpu_seconds: dict[str, float] = {}
        phase_calls: dict[str, int] = {}

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
            phase_wall_seconds[name] = (
                phase_wall_seconds.get(name, 0.0) + wall_seconds
            )
            phase_cpu_seconds[name] = (
                phase_cpu_seconds.get(name, 0.0) + cpu_seconds
            )
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
        original_bounded_stage = (
            preparation_module.prepare_bounded_direct_session
        )
        original_publish = (
            self.context.preparation.staging.publish_canonical_staging
        )
        original_reload = (
            self.context.preparation.staging.get_canonical_staging_run
        )
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
        original_normalization_persistence = (
            self.context.preparation.normalization.repository
            ._insert_normalization_evidence
        )
        original_append_rows = (
            self.context.preparation.sessions.append_direct_rows
        )
        original_append_impacts = self.context.preparation.sessions.append_impacts
        original_finalize_session = (
            self.context.preparation.sessions.finalize_direct_session
        )
        original_project_row = evaluator_module.CompiledBrowserRowTransformer.project
        original_finish_row = evaluator_module.CompiledBrowserRowTransformer.finish
        original_scalar_value = evaluator_module.evaluate_scalar_mapping_value
        original_prepare_row = source_module.CompiledPreparedRowTransformer.transform
        original_canonical_row = (
            bounded_preparation_module.canonical_row_from_prepared
        )
        original_canonical_json = bounded_preparation_module.canonical_json_bytes

        started = perf_counter()
        cpu_started = process_time()
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
                    "impodo.domain.staging.evaluator."
                    "require_supported_browser_scale",
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
                    "impodo.application.quality_service."
                    "build_bounded_quality_run",
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
                    "finalize_direct_session",
                    timed("direct_finalization", original_finalize_session),
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
                    "canonical_json_bytes",
                    accumulated(
                        "canonical_serialization",
                        original_canonical_json,
                    ),
                ),
            )
            for active_patch in patches:
                stack.enter_context(active_patch)
            normalization = self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )
        elapsed = perf_counter() - started
        cpu_seconds = process_time() - cpu_started

        staging = (
            self.context.preparation.staging.get_current_staging_summary(
                project_id
            )
        )
        quality = self.context.preparation.quality.current_summary(project_id)
        current_normalization = (
            self.context.preparation.normalization.current_summary(project_id)
        )
        self.assertIsNotNone(staging)
        self.assertIsNotNone(quality)
        self.assertIsNotNone(current_normalization)
        assert staging is not None
        assert quality is not None
        assert current_normalization is not None

        ending_mib = process.memory_info().rss / (1024 * 1024)
        peak_mib = memory_sampler.peak_bytes / (1024 * 1024)
        database_path = self.root / project_id / "project.duckdb"
        database_mib = database_path.stat().st_size / (1024 * 1024)
        source_snapshots = (
            self.context.preparation.sources.get_current_source_snapshots(project_id)
        )
        snapshot_bytes = sum(
            (
                self.root
                / project_id
                / snapshot.parquet_storage_key
            ).stat().st_size
            for snapshot in source_snapshots
        )
        with self.context.preparation.staging._connect(database_path) as connection:
            counters = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*) FROM quality_row_result),
                    (SELECT COUNT(*) FROM quality_issue),
                    (SELECT COUNT(*) FROM quality_quarantine_entry),
                    (SELECT COUNT(*) FROM source_accounting_link),
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
            f"{name}={seconds:.3f}s"
            for name, seconds in phase_wall_seconds.items()
        )
        print(
            "Complete preparation scale probe: "
            f"workload={PREPARATION_SCALE_WORKLOAD}, "
            f"dirty={PREPARATION_SCALE_DIRTY}, "
            f"rows={PREPARATION_SCALE_ROWS:,}, "
            f"columns={PREPARATION_SCALE_COLUMNS}, "
            f"mapped_fields={PREPARATION_SCALE_MAPPED_FIELDS}, "
            f"fixture={fixture_seconds:.3f}s, total={elapsed:.3f}s, "
            f"fixture_peak={fixture_peak_mib:.1f} MiB, "
            f"peak={peak_mib:.1f} MiB, ending_rss={ending_mib:.1f} MiB, "
            f"database={database_mib:.1f} MiB, "
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
                "database_mib": database_mib,
                "dirty": PREPARATION_SCALE_DIRTY,
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
                "peak_working_set_mib": peak_mib,
                "phase_calls": phase_calls,
                "phase_cpu_seconds": phase_cpu_seconds,
                "phase_wall_seconds": phase_wall_seconds,
                "platform": platform.platform(),
                "python": sys.version,
                "revision": os.environ.get(
                    "IMPODO_PREPARATION_BENCHMARK_REVISION",
                    "unknown",
                ),
                "rows": PREPARATION_SCALE_ROWS,
                "runtime_versions": {
                    name: _installed_version(name)
                    for name in ("duckdb", "openpyxl", "polars", "psutil")
                },
                "schema_version": 1,
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
        self.assertEqual(staging.failed_control_total_count, 0)

    def test_bounded_source_preparation_phase(self) -> None:
        """Measure P3 independently from still-materializing P4 stages."""

        import psutil

        project_id, source_sha256, source_size_bytes = (
            self._prepare_project_and_evidence(
                row_count=PREPARATION_SCALE_ROWS,
                column_count=PREPARATION_SCALE_COLUMNS,
                mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
            )
        )
        project = self.context.preparation.projects.get(project_id)
        revision = self.context.preparation.mappings.get_mapping_revision(project_id)
        physical = self.context.preparation.sources.get_source_selection(project_id)
        effective = self.context.preparation.sources.get_mapping_source_selection(
            project_id
        )
        assert revision is not None
        assert physical is not None
        assert effective is not None
        reference_bundle = (
            self.context.preparation.resolution.current_reference_bundle(project_id)
            if self.context.preparation.resolution is not None
            else None
        )
        process = psutil.Process()
        source_snapshots = (
            self.context.preparation.sources.get_current_source_snapshots(
                project_id
            )
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

        original_prepared_writer = (
            bounded_preparation_module.write_polars_prepared_snapshot
        )
        original_prepared_batches = (
            bounded_preparation_module.iter_polars_prepared_batches
        )
        original_project_row = (
            evaluator_module.CompiledBrowserRowTransformer.project
        )
        original_finish_row = (
            evaluator_module.CompiledBrowserRowTransformer.finish
        )
        original_prepare_row = (
            source_module.CompiledPreparedRowTransformer.transform
        )
        original_canonical_adapter = (
            bounded_preparation_module._canonical_session_row
        )

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
                    bounded_preparation_module,
                    "write_polars_prepared_snapshot",
                    timed_call(
                        "prepared_snapshot_transform_and_write",
                        original_prepared_writer,
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    bounded_preparation_module,
                    "iter_polars_prepared_batches",
                    timed_prepared_batches,
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
                project,
                revision.definition,
                revision.version,
                physical,
                effective,
                self.context.preparation.sources.get_source_catalogs(project_id),
                self.artifacts,
                reference_bundle,
                self.context.preparation.sessions,
                actor=self.context.actor,
                source_snapshots=source_snapshots,
            )
        elapsed = perf_counter() - started
        peak_mib = memory_sampler.peak_bytes / (1024 * 1024)
        ending_mib = process.memory_info().rss / (1024 * 1024)
        database_path = self.root / project_id / "project.duckdb"
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

        if PREPARATION_SCALE_ROWS > COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
            self.skipTest(
                "The production background probe honors the current "
                f"{COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:,}-row "
                "columnar-direct safety limit"
            )

        project_id, _source_sha256, _source_size_bytes = (
            self._prepare_project_and_evidence(
                row_count=PREPARATION_SCALE_ROWS,
                column_count=PREPARATION_SCALE_COLUMNS,
                mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
                dirty=PREPARATION_SCALE_DIRTY,
            )
        )
        project = self.context.queries.get(project_id)
        selection = self.context.queries.get_source_selection(project_id)
        assert selection is not None
        manager = self.context.preparation_jobs
        assert manager is not None

        def run_attempt() -> tuple[float, int]:
            job = manager.enqueue(
                project_id,
                project.name,
                sum(item.row_count for item in selection.datasets),
                actor=self.context.actor,
            )
            started = perf_counter()
            peak_worker_bytes = 0
            deadline = started + 600
            while perf_counter() < deadline:
                current = manager.get(project_id, job.job_id)
                worker_pid = manager.worker_pid(job.job_id)
                if worker_pid is not None:
                    try:
                        peak_worker_bytes = max(
                            peak_worker_bytes,
                            psutil.Process(worker_pid).memory_info().rss,
                        )
                    except psutil.NoSuchProcess:
                        pass
                if current.terminal:
                    break
                sleep(0.05)
            else:
                self.fail(
                    "Background preparation did not finish within ten minutes"
                )
            self.assertEqual(
                current.status,
                PreparationJobStatus.SUCCEEDED,
                msg=f"{current.failure_code}: {current.failure_message}",
            )
            worker_deadline = perf_counter() + 5
            while (
                manager.worker_alive(job.job_id)
                and perf_counter() < worker_deadline
            ):
                sleep(0.01)
            self.assertFalse(manager.worker_alive(job.job_id))
            return perf_counter() - started, peak_worker_bytes

        first_seconds, first_peak = run_attempt()
        first_staging = (
            self.context.preparation.staging.get_current_staging_summary(
                project_id
            )
        )
        first_normalization = (
            self.context.preparation.normalization.current_summary(project_id)
        )
        prepared = (
            self.context.preparation.sessions.current_prepared_snapshots(
                project_id
            )
        )
        self.assertEqual(len(prepared), 1)
        prepared_path = self.root / project_id / prepared[0].parquet_storage_key
        prepared_modified = prepared_path.stat().st_mtime_ns
        self.artifacts.delete_source(
            project_id,
            project.source_files[0].stored_name,
        )

        repeat_seconds, repeat_peak = run_attempt()
        repeated_staging = (
            self.context.preparation.staging.get_current_staging_summary(
                project_id
            )
        )
        repeated_normalization = (
            self.context.preparation.normalization.current_summary(project_id)
        )
        self.assertIsNotNone(first_staging)
        self.assertIsNotNone(first_normalization)
        self.assertIsNotNone(repeated_staging)
        self.assertIsNotNone(repeated_normalization)
        assert first_staging is not None
        assert first_normalization is not None
        assert repeated_staging is not None
        assert repeated_normalization is not None
        self.assertEqual(repeated_staging.content_hash, first_staging.content_hash)
        self.assertEqual(
            repeated_normalization.content_hash,
            first_normalization.content_hash,
        )
        self.assertEqual(
            self.context.preparation.sessions.current_prepared_snapshots(
                project_id
            ),
            prepared,
        )
        self.assertEqual(prepared_path.stat().st_mtime_ns, prepared_modified)
        print(
            "Background preparation probe: "
            f"rows={PREPARATION_SCALE_ROWS:,}, "
            f"first={first_seconds:.3f}s/{first_peak / (1024 * 1024):.1f} MiB, "
            f"repeat={repeat_seconds:.3f}s/{repeat_peak / (1024 * 1024):.1f} MiB, "
            "workers_exited=yes, source_reopened=no"
        )
        if PREPARATION_SCALE_ROWS == COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
            self.assertLess(first_seconds, 120)
            self.assertLess(repeat_seconds, 120)
            self.assertLess(first_peak / (1024 * 1024), 900)
            self.assertLess(repeat_peak / (1024 * 1024), 900)

    def _prepare_project_and_evidence(
        self,
        *,
        row_count: int,
        column_count: int,
        mapped_field_count: int,
        dirty: bool = False,
    ) -> tuple[str, str, int]:
        project = self.context.projects.create_project(
            actor=self.context.actor,
            name="100k complete preparation benchmark",
            source_system="Deterministic CSV fixture",
        )
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

        file_id = str(uuid4())
        with source_path.open("rb") as stream:
            stored = self.artifacts.store_source(
                project.project_id,
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
            received_at=datetime.now(timezone.utc),
        )
        project = self.context.projects.add_source_file(
            project.project_id,
            actor=self.context.actor,
            expected_revision=project.revision,
            source_file=source,
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            project,
            data_manager="Performance tester",
            functional_owner="Performance tester",
            business_unit="Engineering",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_scale",
            intended_models=(
                "product.template"
                if PREPARATION_SCALE_WORKLOAD == "products"
                else "mrp.bom.line",
            ),
            status=ProjectStatus.REGISTERED,
            revision=project.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        self.context.projects.repository.save(
            registered,
            expected_revision=project.revision,
            event_type="TEST_PROJECT_REGISTERED",
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
            registered.project_id,
            (catalog,),
            actor=self.context.actor,
        )
        self.context.sources.confirm_source(
            registered.project_id,
            source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=self.context.actor,
        )
        selection = self.context.sources.freeze_selection(
            registered.project_id,
            dataset_names={
                (source.file_id, "csv"): (
                    "products"
                    if PREPARATION_SCALE_WORKLOAD == "products"
                    else "bom_lines"
                )
            },
            actor=self.context.actor,
        )
        dataset = selection.datasets[0]
        columns = dataset.columns
        fields = tuple(
            ScalarFieldMapping(
                target_field=(
                    (
                        "default_code"
                        if PREPARATION_SCALE_WORKLOAD == "products"
                        else "x_bom_reference"
                    )
                    if index == 0
                    else (
                        "name"
                        if PREPARATION_SCALE_WORKLOAD == "products"
                        else "x_line_reference"
                    )
                    if index == 1
                    else (
                        "list_price"
                        if PREPARATION_SCALE_WORKLOAD == "products"
                        else "product_qty"
                    )
                    if index == 2
                    else f"x_scale_{index:02d}"
                ),
                source_column_key=columns[index].stable_key,
                transform=(
                    ScalarTransformPolicy(trim=True)
                    if index == 1
                    else ScalarTransformPolicy()
                ),
                value_type="decimal" if index == 2 else "string",
                required=index in {0, 1},
            )
            for index in range(mapped_field_count)
        )
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=selection.content_hash,
            schema_hash="sha256:" + "5" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model=(
                        "product.template"
                        if PREPARATION_SCALE_WORKLOAD == "products"
                        else "mrp.bom.line"
                    ),
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(
                        (columns[0].stable_key,)
                        if PREPARATION_SCALE_WORKLOAD == "products"
                        else (
                            columns[0].stable_key,
                            columns[1].stable_key,
                        )
                    ),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(
                                (columns[0].stable_key,)
                                if PREPARATION_SCALE_WORKLOAD == "products"
                                else (
                                    columns[0].stable_key,
                                    columns[1].stable_key,
                                )
                            ),
                            target_fields=(
                                ("default_code",)
                                if PREPARATION_SCALE_WORKLOAD == "products"
                                else ("x_bom_reference", "x_line_reference")
                            ),
                        ),
                    ),
                    fields=fields,
                    control_totals=(
                        BusinessControlTotal(
                            name=(
                                "Sales price total"
                                if PREPARATION_SCALE_WORKLOAD == "products"
                                else "Component quantity total"
                            ),
                            target_field=(
                                "list_price"
                                if PREPARATION_SCALE_WORKLOAD == "products"
                                else "product_qty"
                            ),
                            expected_total=str(row_count),
                            unit="EUR",
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
            registered.project_id,
            revision,
            validation=validation,
            expected_parent_version=None,
            expected_working_draft_version=None,
            checked_draft=MappingWorkingDraft(
                mapping_id=definition.mapping_id,
                version=1,
                project_id=registered.project_id,
                base_mapping_version=revision.version,
                definition=definition,
                updated_at=now,
                updated_by=self.context.actor.identity.display_name,
            ),
            actor=self.context.actor,
        )
        mapping_repository.save_mapping_submission(
            registered.project_id,
            MappingSubmission(
                submission_id=str(uuid4()),
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
        return registered.project_id, stored.sha256, stored.size_bytes

    def _enable_advanced_coverage(self, project_id: str) -> None:
        """Install deterministic Slice 6 inputs for the supported-scale probe."""

        selection = self.context.preparation.sources.get_source_selection(project_id)
        revision = self.context.preparation.mappings.get_mapping_revision(project_id)
        assert selection is not None
        assert revision is not None
        dataset_mapping = revision.definition.datasets[0]
        dataset_name = selection.datasets[0].name
        now = datetime.now(timezone.utc)
        scope = CoverageScopeRevision(
            scope_id=str(uuid4()),
            project_id=project_id,
            version=1,
            parent_version=None,
            source_selection_hash=selection.content_hash,
            declarations=tuple(
                CoverageDeclaration(
                    family=family,
                    applicability=(
                        CoverageApplicability.APPLICABLE
                        if family in {
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
                        if family in {
                            CoverageFamily.TC_09,
                            CoverageFamily.TC_14,
                            CoverageFamily.TC_15,
                            CoverageFamily.TC_20,
                            CoverageFamily.TC_23,
                        }
                        else "Not required by this deterministic scale fixture."
                    ),
                    datasets=(dataset_name,)
                    if family in {
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
            reference_id=str(uuid4()),
            version=1,
            name="Scale approved values",
            key_fields=("value",),
            value_kinds={"approved_value": ReferenceValueKind.BUSINESS_KEY},
            entries=reference_entries,
            owner="Performance tester",
            classification="internal",
            effective_label="Deterministic scale fixture",
        )
        bundle = ReferenceBundle(project_id=project_id, datasets=(reference,))
        target_fields = tuple(
            sorted(item.target_field for item in dataset_mapping.fields)
        )
        policy = ResolutionPolicy(
            policy_id=str(uuid4()),
            project_id=project_id,
            version=1,
            parent_version=None,
            coverage_scope_hash=scope.content_hash,
            mapping_hash=revision.definition.content_hash,
            schema_hash=revision.definition.schema_hash,
            reference_bundle_hash=bundle.content_hash,
            rules=(
                ResolutionRule(
                    rule_id=str(uuid4()),
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
            project_id,
            scope,
            expected_parent_version=None,
            actor=self.context.actor,
        )
        advanced.save_reference_bundle(
            project_id,
            bundle,
            actor=self.context.actor,
        )
        advanced.save_resolution_policy(
            project_id,
            policy,
            expected_parent_version=None,
            actor=self.context.actor,
        )
        base = default_quality_ruleset(
            project_id=project_id,
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
            project_id=project_id,
            version=1,
            parent_version=None,
            mapping_hash=base.mapping_hash,
            schema_hash=base.schema_hash,
            rules=tuple(
                sorted((*base.rules, approved_code, count_boundary), key=lambda item: item.rule_id)
            ),
            coverage_scope_hash=scope.content_hash,
            reference_bundle_hash=bundle.content_hash,
        )
        self.context.quality.publish_ruleset(
            project_id,
            ruleset,
            actor=self.context.actor,
        )


class BoundedPreparationParityTests(unittest.TestCase):
    """Prove the direct durable path preserves the materialized Stage-E bytes."""

    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.artifacts = LocalArtifactStore(self.root)
        self.app = create_local_app(
            self.root,
            artifact_store=self.artifacts,
        )
        self.context = self.app.state.context

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_session_matches_materialized_canonical_evidence(self) -> None:
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=37,
                column_count=8,
                mapped_field_count=5,
                dirty=True,
            )
        )
        project = self.context.preparation.projects.get(project_id)
        revision = self.context.preparation.mappings.get_mapping_revision(project_id)
        physical = self.context.preparation.sources.get_source_selection(project_id)
        effective = self.context.preparation.sources.get_mapping_source_selection(
            project_id
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
            legacy_impacts = []
            legacy = preparation_module.stage_browser_mapping(
                project,
                revision.definition,
                physical,
                effective,
                None,
                self.context.preparation.sources.get_source_catalogs(project_id),
                self.artifacts,
                collect_transformation_impact=True,
                transformation_detail_limit=0,
                transformation_impact_sink=legacy_impacts.append,
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
        original_quality_batches = (
            quality_repository.iter_encoded_json_batches
        )

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
                    raise AssertionError(
                        "Unexpected preparation transport shape"
                    )
                preparation_transport_batches[family].append(
                    batch.row_count
                )
                yield batch

        with (
            patch.object(
                self.context.preparation.sessions,
                "append_provisional_rows",
                side_effect=AssertionError(
                    "direct preparation must not copy canonical rows into provisional storage"
                ),
            ),
            patch.object(
                self.context.preparation.sessions,
                "finalize_session",
                side_effect=AssertionError(
                    "direct preparation must not copy canonical rows into final-session storage"
                ),
            ),
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
                "impodo.adapters.duckdb.quality_repository."
                "DUCKDB_JSON_BATCH_MAX_BYTES",
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
                project_id,
                actor=self.context.actor,
            )
        for family_batches in quality_transport_batches.values():
            self.assertGreater(len(family_batches), 1)
            self.assertEqual(sum(family_batches), 37)
        canonical_batches = preparation_transport_batches["canonical_rows"]
        self.assertGreater(len(canonical_batches), 1)
        self.assertEqual(sum(canonical_batches), 37)
        for family in ("identities", "lineage", "physical_rows"):
            family_batches = preparation_transport_batches[family]
            self.assertGreater(len(family_batches), 1)
            self.assertEqual(sum(family_batches), 37)
        impact_batches = preparation_transport_batches["impacts"]
        self.assertGreater(len(impact_batches), 1)
        self.assertEqual(
            sum(impact_batches),
            len(legacy_impacts),
        )

        summary = self.context.preparation.staging.get_current_staging_summary(
            project_id
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        stored = self.context.preparation.staging.get_canonical_staging_run(
            project_id,
            summary.run_id,
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(summary.content_hash, legacy.canonical_run.content_hash)
        self.assertEqual(stored.to_json(), legacy.canonical_run.to_json())
        ruleset = self.context.preparation.quality.current_ruleset(project_id)
        quality_summary = self.context.preparation.quality.current_summary(project_id)
        self.assertIsNotNone(ruleset)
        self.assertIsNotNone(quality_summary)
        assert ruleset is not None
        assert quality_summary is not None
        expected_quality = evaluate_quality(
            project=project,
            staging=legacy.canonical_run,
            physical_rows=dict(legacy.physical_rows),
            ruleset=ruleset,
            published_staging_content_hash=summary.content_hash,
        )
        self.assertEqual(quality_summary.content_hash, expected_quality.content_hash)
        stored_quality = self.context.preparation.quality.current_run(project_id)
        self.assertIsNotNone(stored_quality)
        assert stored_quality is not None
        self.assertEqual(stored_quality.to_json(), expected_quality.to_json())
        datasets_by_id = {item.dataset_id: item for item in effective.datasets}
        mappings = {
            datasets_by_id[item.dataset_id].name: item
            for item in revision.definition.datasets
        }
        expected_normalization = evaluate_normalization(
            project=project,
            staging=legacy.canonical_run,
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
                for item in legacy_impacts
            ),
            published_staging_content_hash=summary.content_hash,
            published_quality_content_hash=quality_summary.content_hash,
        )
        normalization_summary = (
            self.context.preparation.normalization.current_summary(project_id)
        )
        self.assertIsNotNone(normalization_summary)
        assert normalization_summary is not None
        self.assertEqual(
            normalization_summary.content_hash,
            expected_normalization.content_hash,
        )
        stored_normalization = (
            self.context.preparation.normalization.repository
            .get_normalization_evaluation(
                project_id,
                normalization_summary.run_id,
            )
        )
        self.assertIsNotNone(stored_normalization)
        assert stored_normalization is not None
        self.assertEqual(
            stored_normalization.to_json(),
            expected_normalization.to_json(),
        )
        database_path = self.root / project_id / "project.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            sessions = connection.execute(
                """
                SELECT status, provisional_row_count, canonical_row_count,
                       impact_row_count
                  FROM preparation_session
                """
            ).fetchall()
            temporary_rows = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM preparation_provisional_row),
                    (SELECT COUNT(*) FROM preparation_lineage),
                    (SELECT COUNT(*) FROM preparation_impact_row),
                    (SELECT COUNT(*) FROM preparation_final_row),
                    (SELECT COUNT(*) FROM preparation_direct_identity)
                """
            ).fetchone()
        self.assertEqual(sessions, [("PUBLISHED", 37, 37, 37)])
        self.assertEqual(temporary_rows, (0, 0, 0, 0, 0))

        repeated = self.context.preparation.prepare(
            project_id,
            actor=self.context.actor,
        )
        repeated_staging = (
            self.context.preparation.staging.get_current_staging_summary(
                project_id
            )
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
        project_id, _source_hash, _source_size = (
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
                project_id,
                actor=self.context.actor,
            )

        database_path = self.root / project_id / "project.duckdb"
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

    def test_canonical_transport_failure_cleans_pending_preparation(self) -> None:
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import preparation_session_repository

        original_batches = (
            preparation_session_repository.iter_encoded_json_batches
        )

        def fail_after_first_canonical_batch(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                yield batch
                if '"row_json"' in batch.payload:
                    raise RuntimeError("injected canonical transport failure")

        with (
            patch.object(
                preparation_session_repository,
                "iter_encoded_json_batches",
                fail_after_first_canonical_batch,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected canonical transport failure",
            ),
        ):
            self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )

        database_path = self.root / project_id / "project.duckdb"
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
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import preparation_session_repository

        original_batches = (
            preparation_session_repository.iter_encoded_json_batches
        )

        def reject_canonical_json_batches(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                if '"row_json"' in batch.payload:
                    raise AssertionError(
                        "large canonical rows must bypass JSON transport"
                    )
                yield batch

        with (
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
                project_id,
                actor=self.context.actor,
            )

        staging = self.context.preparation.staging.get_current_staging_summary(
            project_id
        )
        self.assertIsNotNone(staging)
        assert staging is not None
        self.assertEqual(staging.total_rows, 5)
        self.assertEqual(summary.project_id, project_id)

    def test_compact_fact_transport_failure_cleans_direct_batch(self) -> None:
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import preparation_session_repository

        original_batches = (
            preparation_session_repository.iter_encoded_json_batches
        )

        def fail_after_first_physical_batch(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                yield batch
                keys = set(json.loads(batch.payload)[0])
                if keys == {"physical_dataset_id", "source_row"}:
                    raise RuntimeError("injected compact fact failure")

        with (
            patch.object(
                preparation_session_repository,
                "iter_encoded_json_batches",
                fail_after_first_physical_batch,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected compact fact failure",
            ),
        ):
            self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )

        database_path = self.root / project_id / "project.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            session = connection.execute(
                "SELECT status, failure_code FROM preparation_session"
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*) FROM preparation_direct_identity),
                    (SELECT COUNT(*) FROM preparation_lineage),
                    (SELECT COUNT(*) FROM preparation_physical_row),
                    (SELECT COUNT(*) FROM canonical_staging_run)
                """
            ).fetchone()
        self.assertEqual(session, ("FAILED", "BOUNDED_PREPARATION_FAILED"))
        self.assertEqual(counts, (0, 0, 0, 0, 0))

    def test_impact_transport_failure_cleans_pending_preparation(self) -> None:
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import preparation_session_repository

        original_batches = (
            preparation_session_repository.iter_encoded_json_batches
        )

        def fail_after_first_impact_batch(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                yield batch
                if '"impact_json"' in batch.payload:
                    raise RuntimeError("injected impact transport failure")

        with (
            patch.object(
                preparation_session_repository,
                "iter_encoded_json_batches",
                fail_after_first_impact_batch,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected impact transport failure",
            ),
        ):
            self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )

        database_path = self.root / project_id / "project.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            session = connection.execute(
                """
                SELECT status, failure_code
                  FROM preparation_session
                """
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM preparation_impact_row),
                    (SELECT COUNT(*) FROM canonical_staging_row),
                    (SELECT COUNT(*) FROM canonical_staging_run),
                    (SELECT COUNT(*) FROM canonical_staging_current)
                """
            ).fetchone()
        self.assertEqual(session, ("FAILED", "BOUNDED_PREPARATION_FAILED"))
        self.assertEqual(counts, (0, 0, 0, 0))

    def test_source_accounting_transport_failure_rolls_back_quality(self) -> None:
        project_id, _source_hash, _source_size = (
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

        def fail_after_first_source_entry(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                yield batch
                if '"physical_dataset_id"' in batch.payload:
                    raise RuntimeError(
                        "injected source accounting transport failure"
                    )

        with (
            patch.object(
                quality_repository,
                "iter_encoded_json_batches",
                fail_after_first_source_entry,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected source accounting transport failure",
            ),
        ):
            self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )

        database_path = self.root / project_id / "project.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM quality_row_result),
                    (SELECT COUNT(*) FROM source_accounting_entry),
                    (SELECT COUNT(*) FROM source_accounting_link),
                    (SELECT COUNT(*) FROM quality_run),
                    (SELECT COUNT(*) FROM quality_current)
                """
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0, 0, 0))

    def test_normalization_transport_failure_rolls_back_pending_evidence(self) -> None:
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=5,
                column_count=5,
                mapped_field_count=5,
                dirty=True,
            )
        )
        from impodo.adapters.duckdb import normalization_repository

        original_batches = normalization_repository.iter_encoded_json_batches

        def fail_after_first_batch(*args, **kwargs):
            for batch in original_batches(*args, **kwargs):
                yield batch
                raise RuntimeError("injected transport failure")

        with (
            patch.object(
                normalization_repository,
                "iter_encoded_json_batches",
                fail_after_first_batch,
            ),
            self.assertRaisesRegex(RuntimeError, "injected transport failure"),
        ):
            self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )

        database_path = self.root / project_id / "project.duckdb"
        with self.context.preparation.staging._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM normalization_effect),
                    (SELECT COUNT(*) FROM normalization_run),
                    (SELECT COUNT(*) FROM normalization_current)
                """
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0))

    def test_direct_publication_failure_preserves_current_run(self) -> None:
        project_id, _source_hash, _source_size = (
            PreparationWorkflowScaleTests._prepare_project_and_evidence(
                self,
                row_count=17,
                column_count=8,
                mapped_field_count=5,
            )
        )
        self.context.preparation.prepare(
            project_id,
            actor=self.context.actor,
        )
        current = self.context.preparation.staging.get_current_staging_summary(
            project_id
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
                    project_id,
                    actor=self.context.actor,
                )

        unchanged = self.context.preparation.staging.get_current_staging_summary(
            project_id
        )
        self.assertIsNotNone(unchanged)
        assert unchanged is not None
        self.assertEqual(unchanged.run_id, current.run_id)
        database_path = self.root / project_id / "project.duckdb"
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
        project_id, _source_hash, _source_size = (
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
                    project_id,
                    actor=self.context.actor,
                )

        self.assertIsNone(
            self.context.preparation.staging.get_current_staging_summary(
                project_id
            )
        )
        database_path = self.root / project_id / "project.duckdb"
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
        "product_reference" if workload == "products" else "bom_reference",
        "name" if workload == "products" else "line_reference",
        "list_price" if workload == "products" else "quantity",
        *(f"field_{index:02d}" for index in range(3, column_count)),
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
        ),
        (
            f" Product {index:06d} "
            if workload == "products"
            else f" L{identity_index:06d} "
        ),
        "1",
        *(
            f"value-{column:02d}-{index % 100:02d}"
            for column in range(3, column_count)
        ),
    )


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
            distinct_count=(
                row_count if index in {0, 1} else 1 if index == 2 else 100
            ),
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
        contract_version=1,
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
