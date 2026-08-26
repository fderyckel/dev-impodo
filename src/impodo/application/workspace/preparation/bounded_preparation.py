"""Bounded direct-dataset orchestration for 100,000-row Stage-E preparation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from impodo.domain.shared.access import Actor
from impodo.application.shared.artifacts import GovernedArtifactStores, ArtifactStoreError
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.columnar_transformation import (
    ColumnarCompilationError,
    ColumnarSupport,
    ColumnarTransformationProgram,
    compile_columnar_transformation_programs,
)
from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.prepared_snapshot import (
    PREPARED_WRITER_CONTRACT_VERSION,
    PreparedSnapshot,
    prepared_snapshot_logical_hash,
)
from impodo.domain.staging.control_totals import CompiledControlTotalAccumulator
from impodo.domain.staging.canonical_projection import (
    canonical_prepared_session_row,
    canonical_quality_identity_key,
    canonical_quality_record_label,
)
from impodo.domain.staging.evaluator import (
    canonical_field_sources,
    compile_browser_row_transformer,
    compile_reference_indexes,
)
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.source_binding import SourceOriginKind, require_file_source
from impodo.domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparedCanonicalProjection,
    PreparationSessionBindings,
    StoredCanonicalStagingRun,
)
from impodo.domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
)
from impodo.domain.staging.transformation_impact import (
    TransformationImpactRow,
    _TransformationImpactCollector,
    reviewable_rule_impact_definitions,
)
from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.domain.shared.models import Issue, PreparedRecord, canonical_json_bytes
from impodo.domain.workspace.workbench import WorkspaceState, SourceFile
from impodo.domain.preparation.source import CompiledPreparedRowTransformer, SourceLoadError
from impodo.application.data_version.source_files import open_selected_source_batches
from impodo.application.data_version.source_snapshots import (
    open_source_snapshot_batches,
    validate_snapshot_for_dataset,
)
from impodo.domain.preparation.staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    StagingDatasetRole,
    canonical_row_from_prepared,
)
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection
from impodo.domain.errors import ReadinessError
from .columnar_transformation_port import (
    DEFAULT_COLUMNAR_TRANSFORMATION_BATCH_ROWS,
    ColumnarTransformationPort,
)
from .readiness_ports import PreparationSessionRepository


BOUNDED_SOURCE_BATCH_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class BoundedDirectPreparation:
    """READY session plus its bounded canonical publication projection."""

    session_id: str
    run: StoredCanonicalStagingRun


class _ImpactBatchSink:
    """Bound the collector callback while preserving exact emission order."""

    def __init__(self, repository, workspace_id: str, session_id: str) -> None:
        self.repository = repository
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.rows: list[TransformationImpactRow] = []

    def add(self, row: TransformationImpactRow) -> None:
        self.rows.append(row)
        if len(self.rows) >= BOUNDED_SOURCE_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        self.repository.append_impacts(
            self.workspace_id,
            self.session_id,
            tuple(self.rows),
        )
        self.rows.clear()


def supports_bounded_direct_preparation(
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> bool:
    """Return whether every effective dataset is a direct physical dataset."""

    if plan is not None:
        return False
    physical = {(item.dataset_id, item.name) for item in physical_selection.datasets}
    effective = {(item.dataset_id, item.name) for item in effective_selection.datasets}
    return physical == effective and len(physical) == len(physical_selection.datasets)


def direct_preparation_row_limit(
    definition: MappingDefinition,
    effective_selection: SourceSelection,
    source_snapshots: Iterable[SourceSnapshot],
) -> int:
    """Return 100k only for the mandatory verified columnar production path."""

    try:
        decisions = compile_columnar_transformation_programs(
            definition,
            effective_selection,
        )
    except ColumnarCompilationError:
        return BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
    if any(item.support is not ColumnarSupport.SUPPORTED for item in decisions):
        return BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
    snapshots = tuple(source_snapshots)
    snapshots_by_id = {item.dataset_id: item for item in snapshots}
    datasets_by_id = {item.dataset_id: item for item in effective_selection.datasets}
    if len(snapshots_by_id) != len(snapshots) or set(snapshots_by_id) != set(
        datasets_by_id
    ):
        return BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
    try:
        for dataset_id, dataset in datasets_by_id.items():
            validate_snapshot_for_dataset(
                effective_selection,
                dataset,
                snapshots_by_id[dataset_id],
            )
    except SourceLoadError:
        return BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
    return COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT


def prepare_bounded_direct_session(
    workspace_state: WorkspaceState,
    definition: MappingDefinition,
    mapping_version: int,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: GovernedArtifactStores,
    reference_bundle: ReferenceBundle | None,
    sessions: PreparationSessionRepository,
    columnar_transformations: ColumnarTransformationPort,
    *,
    actor: Actor,
    source_snapshots: Iterable[SourceSnapshot] | None = None,
    batch_progress: Callable[[int, int], None] | None = None,
    columnar_batch_size: int = DEFAULT_COLUMNAR_TRANSFORMATION_BATCH_ROWS,
) -> BoundedDirectPreparation:
    """Transform direct selected sources into one READY durable session."""

    if not supports_bounded_direct_preparation(
        physical_selection,
        effective_selection,
        None,
    ):
        raise ReadinessError(
            "Bounded direct preparation requires unchanged physical datasets"
        )
    if (
        physical_selection.data_version_id != effective_selection.data_version_id
    ):
        raise ReadinessError(
            "Canonical evaluation evidence belongs to another DataVersion"
        )
    physical_selection_hash = physical_selection.content_hash
    source_selection_hash = effective_selection.content_hash
    mapping_hash = definition.content_hash
    schema_hash = definition.schema_hash
    if definition.source_selection_hash != source_selection_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    if (
        reference_bundle is not None
        and reference_bundle.workspace_id != workspace_state.workspace_id
    ):
        raise ReadinessError("Reference data belongs to another workspace")
    if columnar_batch_size < 1:
        raise ValueError("Columnar transformation batch size must be positive")

    physical_by_id = {item.dataset_id: item for item in physical_selection.datasets}
    effective_by_id = {item.dataset_id: item for item in effective_selection.datasets}
    mapping_by_id = {item.dataset_id: item for item in definition.datasets}
    if (
        len(physical_by_id) != len(physical_selection.datasets)
        or len(effective_by_id) != len(effective_selection.datasets)
        or len(mapping_by_id) != len(definition.datasets)
        or set(mapping_by_id) != set(effective_by_id)
    ):
        raise ReadinessError("Prepared direct datasets are incomplete or duplicated")

    compiled_plan = compile_browser_mapping(definition, effective_selection)
    compiled_by_name = {item.name: item for item in compiled_plan.datasets}
    columnar_by_id = {
        item.dataset_id: item
        for item in compile_columnar_transformation_programs(
            definition,
            effective_selection,
        )
    }
    source_hashes = {
        item.name: item.source_evidence_hash for item in effective_selection.datasets
    }
    modes = {dataset.name: dataset.target.mode for dataset in compiled_plan.datasets}
    field_sources = canonical_field_sources(
        definition,
        effective_selection,
    )
    snapshot_by_dataset = {item.dataset_id: item for item in (source_snapshots or ())}
    if source_snapshots is not None and (
        len(snapshot_by_dataset) != len(physical_selection.datasets)
        or set(snapshot_by_dataset) != set(physical_by_id)
    ):
        raise ReadinessError("Frozen source snapshots are incomplete")
    session = sessions.begin_direct_session(
        workspace_state.workspace_id,
        PreparationSessionBindings(
            mapping_id=definition.mapping_id,
            mapping_version=mapping_version,
            physical_selection_hash=physical_selection_hash,
            source_selection_hash=source_selection_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            derived_plan_hash=None,
            compiled_plan_hash=compiled_plan.semantic_hash,
            contract_version=STAGING_CONTRACT_VERSION,
            evaluator_version=BROWSER_EVALUATOR_VERSION,
            source_hashes=source_hashes,
        ),
        actor=actor,
    )
    impact_sink = _ImpactBatchSink(
        sessions,
        workspace_state.workspace_id,
        session.session_id,
    )
    impact_collector = _TransformationImpactCollector(
        mapping_content_hash=mapping_hash,
        detail_limit=0,
        sink=impact_sink.add,
    )
    for dataset_mapping in definition.datasets:
        for field in dataset_mapping.fields:
            for rule in reviewable_rule_impact_definitions(
                dataset_mapping.dataset_id, field
            ):
                impact_collector.register_rule(rule)
    reference_indexes = compile_reference_indexes(reference_bundle)
    totals = CompiledControlTotalAccumulator.compile(
        definition,
        effective_selection,
    )
    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in workspace_state.source_files}
    run_issues: list[Issue] = []
    dataset_evidence: dict[
        str,
        tuple[str, StagingDatasetRole, int, str],
    ] = {}
    dataset_offsets: dict[str, int] = {}
    next_offset = 0
    for dataset in sorted(effective_selection.datasets, key=lambda item: item.name):
        dataset_offsets[dataset.dataset_id] = next_offset
        next_offset += dataset.row_count
    total_rows = sum(item.row_count for item in effective_selection.datasets)
    completed_rows = 0

    try:
        for effective in effective_selection.datasets:
            physical = physical_by_id[effective.dataset_id]
            binding = physical.source
            mapping = mapping_by_id[effective.dataset_id]
            snapshot = snapshot_by_dataset.get(physical.dataset_id)
            source_file: SourceFile | None = None
            named_range: str | None = None
            if physical.origin is SourceOriginKind.FILE:
                file_binding = require_file_source(binding)
                source_file = source_file_by_id.get(file_binding.file_id)
                catalog = catalog_by_file.get(file_binding.file_id)
                table_catalog = next(
                    (
                        item
                        for item in (catalog.tables if catalog else ())
                        if item.table_key == file_binding.table_key
                    ),
                    None,
                )
                if source_file is None or catalog is None or table_catalog is None:
                    raise ReadinessError("Frozen source evidence is incomplete")
                named_range = (
                    table_catalog.named_tables[0].cell_range
                    if (
                        table_catalog.kind == "NAMED_TABLE"
                        and table_catalog.named_tables
                    )
                    else None
                )
            elif physical.origin is SourceOriginKind.ODOO:
                if snapshot is None:
                    raise ReadinessError(
                        "Pinned Odoo preparation requires its frozen snapshot"
                    )
            else:
                raise ReadinessError("Frozen source evidence is incomplete")
            columnar = columnar_by_id[effective.dataset_id]
            if columnar.support is ColumnarSupport.SUPPORTED:
                if snapshot is None:
                    raise ReadinessError(
                        "Supported direct preparation requires its frozen "
                        "source snapshot"
                    )
                assert columnar.program is not None
                row_count = 0
                pending_rows: list[CanonicalPreparedSessionRow] = []
                try:
                    validate_snapshot_for_dataset(
                        physical_selection,
                        physical,
                        snapshot,
                    )
                    prepared_snapshot = _prepared_snapshot_for_program(
                        workspace_state,
                        snapshot,
                        columnar.program,
                        artifacts,
                        sessions,
                        session.session_id,
                        columnar_transformations,
                    )
                    projection = PreparedCanonicalProjection(
                        dataset_id=effective.dataset_id,
                        dataset=effective.name,
                        ordinal_start=dataset_offsets[effective.dataset_id],
                        row_count=prepared_snapshot.row_count,
                        mode=modes[effective.name],
                        source_hash=source_hashes[effective.name],
                        physical_dataset_id=physical.dataset_id,
                        field_sources=field_sources.get(
                            effective.name,
                            {},
                        ),
                        program=columnar.program,
                    )
                    with artifacts.materialize_prepared_snapshot(
                        workspace_state.workspace_id,
                        prepared_snapshot.parquet_storage_key,
                        expected_sha256=prepared_snapshot.parquet_sha256,
                    ) as path:
                        native_projection = sessions.append_native_prepared_projection(
                            workspace_state.workspace_id,
                            session.session_id,
                            prepared_snapshot,
                            replace(
                                projection,
                                set_based_projection=True,
                            ),
                            path,
                            totals.target_fields(effective.name),
                        )
                        if native_projection is not None:
                            for control in native_projection.control_totals:
                                totals.add_precomputed(
                                    effective.name,
                                    target_field=control.target_field,
                                    actual_total=control.actual_total,
                                    included_rows=control.included_rows,
                                    empty_rows=control.empty_rows,
                                )
                            impact_collector.record_persisted_precomputed(
                                native_projection.impact_counts,
                                columnar_transformations.summarize_rule_impacts(
                                    path,
                                    prepared_snapshot,
                                    columnar.program,
                                ),
                            )
                            row_count = native_projection.row_count
                            completed_rows += row_count
                            if batch_progress is not None:
                                batch_progress(completed_rows, total_rows)
                            dataset_evidence[effective.name] = (
                                physical.dataset_id,
                                StagingDatasetRole.DIRECT,
                                row_count,
                                mapping.target_model,
                            )
                            continue

                        _require_python_row_adaptation_capacity(total_rows)
                        sessions.bind_prepared_canonical_projection(
                            workspace_state.workspace_id,
                            session.session_id,
                            prepared_snapshot,
                            projection,
                        )
                        for native_batch in columnar_transformations.iter_prepared_batches(
                            path,
                            prepared_snapshot,
                            snapshot,
                            columnar.program,
                            batch_size=columnar_batch_size,
                            materialize_records=False,
                        ):
                            impact_collector.record_precomputed(
                                native_batch.impact_counts,
                                native_batch.impacts,
                                native_batch.rule_impacts,
                            )
                            for batch_index, (
                                source_row,
                                source_identity,
                                target_identity,
                                target_scope,
                                scalar_values,
                                references,
                                issues,
                            ) in enumerate(
                                zip(
                                    native_batch.source_rows,
                                    native_batch.source_identities,
                                    native_batch.target_identities,
                                    native_batch.target_scopes,
                                    native_batch.scalar_values,
                                    native_batch.references,
                                    native_batch.issues,
                                    strict=True,
                                )
                            ):
                                totals.add_values(
                                    effective.name,
                                    scalar_values,
                                )
                                pending_rows.append(
                                    canonical_prepared_session_row(
                                        dataset=effective.name,
                                        source_row=source_row,
                                        target_model=columnar.program.target_model,
                                        source_identity=source_identity,
                                        target_identity=target_identity,
                                        target_scope=target_scope,
                                        scalar_values=scalar_values,
                                        references=references,
                                        issues=issues,
                                        ordinal=(
                                            dataset_offsets[effective.dataset_id]
                                            + row_count
                                            + batch_index
                                        ),
                                        mode=modes[effective.name],
                                        source_hash=source_hashes[effective.name],
                                        source_selection_hash=(source_selection_hash),
                                        mapping_hash=mapping_hash,
                                        schema_hash=schema_hash,
                                        field_sources=field_sources.get(
                                            effective.name,
                                            {},
                                        ),
                                        physical_dataset_id=(physical.dataset_id),
                                        encode_payload=False,
                                    )
                                )
                                if len(pending_rows) == BOUNDED_SOURCE_BATCH_SIZE:
                                    sessions.append_direct_rows(
                                        workspace_state.workspace_id,
                                        session.session_id,
                                        pending_rows,
                                    )
                                    pending_rows.clear()
                            batch_rows = len(native_batch.source_rows)
                            row_count += batch_rows
                            completed_rows += batch_rows
                            if batch_progress is not None:
                                batch_progress(completed_rows, total_rows)
                        if pending_rows:
                            sessions.append_direct_rows(
                                workspace_state.workspace_id,
                                session.session_id,
                                pending_rows,
                            )
                            pending_rows.clear()
                except (ArtifactStoreError, SourceLoadError) as error:
                    _cleanup_prepared_snapshot_orphans(
                        workspace_state.workspace_id,
                        artifacts,
                        sessions,
                    )
                    raise ReadinessError(
                        "The prepared columnar snapshot could not be verified"
                    ) from error
                dataset_evidence[effective.name] = (
                    physical.dataset_id,
                    StagingDatasetRole.DIRECT,
                    row_count,
                    mapping.target_model,
                )
                continue

            transformer = compile_browser_row_transformer(
                effective,
                physical,
                mapping,
                None,
                "source",
                reference_indexes=reference_indexes,
            )
            preparer = CompiledPreparedRowTransformer.compile(
                compiled_by_name[effective.name],
                transformer.headers,
            )
            if preparer.dataset_issue is not None:
                run_issues.append(preparer.dataset_issue)
            row_count = 0
            with _open_preparation_source(
                workspace_state,
                physical_selection,
                physical,
                source_file,
                artifacts,
                snapshot,
                named_range=named_range,
            ) as source:
                expected_hash = binding.source_evidence_hash
                if source.content_hash != expected_hash:
                    raise ReadinessError(
                        "Stored source content changed after selection"
                    )
                for source_batch in source.iter_batches():
                    prepared_batch: list[CanonicalPreparedSessionRow] = []
                    for source_row in source_batch:
                        projected = transformer.project(source_row)
                        staged_row, preparation_issues = transformer.finish(
                            projected,
                            impact_collector=impact_collector,
                        )
                        record = preparer.transform(staged_row)
                        if preparation_issues:
                            record = replace(
                                record,
                                issues=(*record.issues, *preparation_issues),
                            )
                        totals.add(record)
                        prepared_batch.append(
                            _canonical_session_row(
                                record,
                                source_row=source_row.number,
                                ordinal=(
                                    dataset_offsets[effective.dataset_id]
                                    + row_count
                                    + len(prepared_batch)
                                ),
                                mode=modes[effective.name],
                                source_hash=source_hashes[effective.name],
                                source_selection_hash=source_selection_hash,
                                mapping_hash=mapping_hash,
                                schema_hash=schema_hash,
                                field_sources=field_sources.get(
                                    effective.name,
                                    {},
                                ),
                                physical_dataset_id=physical.dataset_id,
                            )
                        )
                    sessions.append_direct_rows(
                        workspace_state.workspace_id,
                        session.session_id,
                        prepared_batch,
                    )
                    row_count += len(source_batch)
                    completed_rows += len(source_batch)
                    if batch_progress is not None:
                        batch_progress(completed_rows, total_rows)
            dataset_evidence[effective.name] = (
                physical.dataset_id,
                StagingDatasetRole.DIRECT,
                row_count,
                mapping.target_model,
            )

        impact_sink.flush()
        run = sessions.finalize_direct_session(
            workspace_state.workspace_id,
            session.session_id,
            dataset_evidence=dataset_evidence,
            run_issues=run_issues,
            control_totals=totals.report(),
            impact_report=impact_collector.report(),
        )
        return BoundedDirectPreparation(session_id=session.session_id, run=run)
    except Exception:
        sessions.fail_session(
            workspace_state.workspace_id,
            session.session_id,
            "BOUNDED_PREPARATION_FAILED",
        )
        raise


def _require_python_row_adaptation_capacity(total_rows: int) -> None:
    """Keep a data-dependent native miss inside the proven Python boundary."""

    if total_rows <= BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
        return
    raise ReadinessError(
        "This prepared data requires the bounded compatibility checker, but "
        f"this workspace selection contains {total_rows:,} rows. That route safely "
        f"supports up to {BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:,} "
        "rows. Split the source into smaller projects; no data was changed."
    )


def _canonical_session_row(
    record: PreparedRecord,
    *,
    source_row: int,
    ordinal: int,
    mode: str,
    source_hash: str,
    source_selection_hash: str,
    mapping_hash: str,
    schema_hash: str,
    field_sources: Mapping[str, tuple[str, ...]],
    physical_dataset_id: str,
) -> CanonicalPreparedSessionRow:
    """Adapt either row backend through the one canonical publication path."""

    physical_sources = {physical_dataset_id: (source_row,)}
    canonical = canonical_row_from_prepared(
        record,
        mode=mode,
        source_hash=source_hash,
        source_selection_hash=source_selection_hash,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        derived_plan_hash=None,
        field_sources=field_sources,
        physical_dataset_id=physical_dataset_id,
        physical_source_rows=(source_row,),
        physical_sources=physical_sources,
    )
    return CanonicalPreparedSessionRow(
        row_id=canonical.row_id,
        ordinal=ordinal,
        dataset=canonical.dataset,
        source_row=canonical.source_row,
        target_model=canonical.target_model,
        disposition=canonical.disposition,
        source_identity=canonical.source_identity,
        row_json=canonical_json_bytes(canonical.to_portable_dict()).decode("utf-8"),
        references=record.references,
        physical_sources=physical_sources,
        record_label=canonical_quality_record_label(
            canonical.source_identity,
            canonical.target_identity,
            canonical.source_row,
        ),
        quality_identity_key=canonical_quality_identity_key(
            dataset=canonical.dataset,
            target_model=canonical.target_model,
            target_identity=canonical.target_identity,
            target_scope=canonical.target_scope,
        ),
        issues=canonical.issues,
    )


def _prepared_snapshot_for_program(
    workspace_state: WorkspaceState,
    source_snapshot: SourceSnapshot,
    program: ColumnarTransformationProgram,
    artifacts: GovernedArtifactStores,
    sessions: PreparationSessionRepository,
    session_id: str,
    columnar_transformations: ColumnarTransformationPort,
) -> PreparedSnapshot:
    """Reuse or publish one exact typed projection before canonical adaptation."""

    logical_hash = prepared_snapshot_logical_hash(
        workspace_id=workspace_state.workspace_id,
        dataset_id=program.dataset_id,
        dataset_name=program.dataset_name,
        source_snapshot_hash=source_snapshot.content_hash,
        mapping_hash=program.mapping_content_hash,
        schema_hash=program.schema_hash,
        transformation_program_hash=program.content_hash,
        writer_contract_version=PREPARED_WRITER_CONTRACT_VERSION,
        row_count=source_snapshot.row_count,
    )
    existing = sessions.find_prepared_snapshot(
        workspace_state.workspace_id,
        program.dataset_id,
        logical_hash,
    )
    if existing is not None:
        try:
            with artifacts.materialize_prepared_snapshot(
                workspace_state.workspace_id,
                existing.parquet_storage_key,
                expected_sha256=existing.parquet_sha256,
            ):
                pass
        except ArtifactStoreError:
            existing = None
        else:
            sessions.bind_prepared_snapshot(
                workspace_state.workspace_id,
                session_id,
                existing,
            )
            return existing

    try:
        with artifacts.materialize_source_snapshot(
            source_snapshot.data_version_id,
            source_snapshot.parquet_storage_key,
            expected_sha256=source_snapshot.parquet_sha256,
        ) as source_path:
            with artifacts.prepare_prepared_snapshot(workspace_state.workspace_id) as workspace:
                candidate_path = workspace / "prepared.parquet"
                candidate = columnar_transformations.write_prepared_snapshot(
                    source_path,
                    source_snapshot,
                    program,
                    candidate_path,
                )
                prepared = PreparedSnapshot.create(
                    workspace_id=workspace_state.workspace_id,
                    dataset_id=program.dataset_id,
                    dataset_name=program.dataset_name,
                    source_snapshot_hash=source_snapshot.content_hash,
                    mapping_hash=program.mapping_content_hash,
                    schema_hash=program.schema_hash,
                    transformation_program_hash=program.content_hash,
                    row_count=candidate.row_count,
                    physical_schema_hash=candidate.physical_schema_hash,
                    parquet_sha256=candidate.parquet_sha256,
                    created_at=datetime.now(timezone.utc),
                )
                artifacts.publish_prepared_snapshot(
                    workspace_state.workspace_id,
                    candidate_path,
                    prepared.parquet_storage_key,
                    expected_sha256=prepared.parquet_sha256,
                )
        sessions.bind_prepared_snapshot(
            workspace_state.workspace_id,
            session_id,
            prepared,
        )
        return prepared
    except Exception:
        _cleanup_prepared_snapshot_orphans(
            workspace_state.workspace_id,
            artifacts,
            sessions,
        )
        raise


def _cleanup_prepared_snapshot_orphans(
    workspace_id: str,
    artifacts: GovernedArtifactStores,
    sessions: PreparationSessionRepository,
) -> None:
    """Best-effort cleanup without masking the preparation failure."""

    try:
        referenced = sessions.prepared_snapshot_storage_keys(workspace_id)
        artifacts.cleanup_prepared_snapshots(workspace_id, referenced)
    except Exception:
        pass


@contextmanager
def _open_preparation_source(
    workspace_state: WorkspaceState,
    selection: SourceSelection,
    dataset: SourceDataset,
    source_file: SourceFile | None,
    artifacts: GovernedArtifactStores,
    snapshot: SourceSnapshot | None,
    *,
    named_range: str | None,
):
    """Resolve production reads to Parquet with an explicit oracle fallback."""

    if snapshot is not None:
        try:
            validate_snapshot_for_dataset(selection, dataset, snapshot)
            with artifacts.materialize_source_snapshot(
                selection.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                with open_source_snapshot_batches(
                    path,
                    snapshot,
                    batch_size=BOUNDED_SOURCE_BATCH_SIZE,
                ) as source:
                    yield source
            return
        except (ArtifactStoreError, SourceLoadError) as error:
            raise ReadinessError(
                "The frozen source snapshot could not be verified"
            ) from error

    if source_file is None:
        raise ReadinessError("The frozen source snapshot is required")

    with artifacts.materialize_source(
        selection.data_version_id,
        source_file.stored_name,
    ) as path:
        binding = require_file_source(dataset.source)
        with open_selected_source_batches(
            path,
            dataset=dataset.name,
            table_key=binding.table_key,
            encoding=binding.encoding,
            delimiter=binding.delimiter,
            header_row=binding.header_row,
            named_table_range=named_range,
            source_display_name=source_file.display_name,
            batch_size=BOUNDED_SOURCE_BATCH_SIZE,
        ) as source:
            yield source
