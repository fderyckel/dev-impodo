"""Bounded direct-dataset orchestration for 100,000-row Stage-E preparation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from ..artifacts import ArtifactStore
from ..derived_entities import DerivedEntityPlan
from ..domain.compiler.browser_mapping_compiler import compile_browser_mapping
from ..domain.mapping.contracts import MappingDefinition
from ..domain.staging.control_totals import CompiledControlTotalAccumulator
from ..domain.staging.evaluator import (
    canonical_field_sources,
    compile_browser_row_transformer,
    compile_reference_indexes,
)
from ..domain.coverage import ReferenceBundle
from ..domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    StoredCanonicalStagingRun,
)
from ..domain.staging.transformation_impact import (
    TransformationImpactRow,
    _TransformationImpactCollector,
)
from ..inspection import SourceFileCatalog
from ..models import Issue, canonical_json_bytes
from ..projects import MigrationProject
from ..source import CompiledPreparedRowTransformer, open_selected_source_batches
from ..staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    StagingDatasetRole,
    canonical_row_from_prepared,
)
from ..workspace_contracts import SourceSelection
from ..domain.errors import ReadinessError
from .readiness_ports import PreparationSessionRepository


BOUNDED_SOURCE_BATCH_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class BoundedDirectPreparation:
    """READY session plus its bounded canonical publication projection."""

    session_id: str
    run: StoredCanonicalStagingRun


class _ImpactBatchSink:
    """Bound the collector callback while preserving exact emission order."""

    def __init__(self, repository, project_id: str, session_id: str) -> None:
        self.repository = repository
        self.project_id = project_id
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
            self.project_id,
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
    physical = {
        (item.dataset_id, item.name) for item in physical_selection.datasets
    }
    effective = {
        (item.dataset_id, item.name) for item in effective_selection.datasets
    }
    return physical == effective and len(physical) == len(
        physical_selection.datasets
    )


def prepare_bounded_direct_session(
    project: MigrationProject,
    definition: MappingDefinition,
    mapping_version: int,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: ArtifactStore,
    reference_bundle: ReferenceBundle | None,
    sessions: PreparationSessionRepository,
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
        physical_selection.project_id != project.project_id
        or effective_selection.project_id != project.project_id
    ):
        raise ReadinessError("Canonical evaluation evidence belongs to another project")
    physical_selection_hash = physical_selection.content_hash
    source_selection_hash = effective_selection.content_hash
    mapping_hash = definition.content_hash
    schema_hash = definition.schema_hash
    if definition.source_selection_hash != source_selection_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    if reference_bundle is not None and reference_bundle.project_id != project.project_id:
        raise ReadinessError("Reference data belongs to another project")

    physical_by_id = {
        item.dataset_id: item for item in physical_selection.datasets
    }
    effective_by_id = {
        item.dataset_id: item for item in effective_selection.datasets
    }
    mapping_by_id = {item.dataset_id: item for item in definition.datasets}
    if (
        len(physical_by_id) != len(physical_selection.datasets)
        or len(effective_by_id) != len(effective_selection.datasets)
        or len(mapping_by_id) != len(definition.datasets)
        or set(mapping_by_id) != set(effective_by_id)
    ):
        raise ReadinessError("Prepared direct datasets are incomplete or duplicated")

    compiled_plan = compile_browser_mapping(definition, effective_selection)
    compiled_by_name = {
        item.name: item for item in compiled_plan.datasets
    }
    source_hashes = {
        item.name: f"sha256:{item.source_sha256.removeprefix('sha256:')}"
        for item in effective_selection.datasets
    }
    modes = {
        dataset.name: dataset.target.mode
        for dataset in compiled_plan.datasets
    }
    field_sources = canonical_field_sources(
        definition,
        effective_selection,
    )
    session = sessions.begin_session(
        project.project_id,
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
    )
    impact_sink = _ImpactBatchSink(
        sessions,
        project.project_id,
        session.session_id,
    )
    impact_collector = _TransformationImpactCollector(
        mapping_content_hash=mapping_hash,
        detail_limit=0,
        sink=impact_sink.add,
    )
    reference_indexes = compile_reference_indexes(reference_bundle)
    totals = CompiledControlTotalAccumulator.compile(
        definition,
        effective_selection,
    )
    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in project.source_files}
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

    try:
        for effective in effective_selection.datasets:
            physical = physical_by_id[effective.dataset_id]
            mapping = mapping_by_id[effective.dataset_id]
            source_file = source_file_by_id.get(physical.file_id)
            catalog = catalog_by_file.get(physical.file_id)
            table_catalog = next(
                (
                    item
                    for item in (catalog.tables if catalog else ())
                    if item.table_key == physical.table_key
                ),
                None,
            )
            if source_file is None or catalog is None or table_catalog is None:
                raise ReadinessError("Frozen source evidence is incomplete")
            named_range = (
                table_catalog.named_tables[0].cell_range
                if table_catalog.kind == "NAMED_TABLE"
                and table_catalog.named_tables
                else None
            )
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
            with artifacts.materialize_source(
                project.project_id,
                source_file.stored_name,
            ) as path:
                with open_selected_source_batches(
                    path,
                    dataset=physical.name,
                    table_key=physical.table_key,
                    encoding=physical.encoding,
                    delimiter=physical.delimiter,
                    header_row=physical.header_row,
                    named_table_range=named_range,
                    batch_size=BOUNDED_SOURCE_BATCH_SIZE,
                ) as source:
                    expected_hash = (
                        f"sha256:{physical.source_sha256.removeprefix('sha256:')}"
                    )
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
                            physical_sources = {
                                physical.dataset_id: (source_row.number,)
                            }
                            canonical = canonical_row_from_prepared(
                                record,
                                mode=modes[effective.name],
                                source_hash=source_hashes[effective.name],
                                source_selection_hash=source_selection_hash,
                                mapping_hash=mapping_hash,
                                schema_hash=schema_hash,
                                derived_plan_hash=None,
                                field_sources=field_sources.get(
                                    effective.name,
                                    {},
                                ),
                                physical_dataset_id=physical.dataset_id,
                                physical_source_rows=(source_row.number,),
                                physical_sources=physical_sources,
                            )
                            prepared_batch.append(
                                CanonicalPreparedSessionRow(
                                    row_id=canonical.row_id,
                                    ordinal=(
                                        dataset_offsets[effective.dataset_id]
                                        + row_count
                                        + len(prepared_batch)
                                    ),
                                    dataset=canonical.dataset,
                                    source_row=canonical.source_row,
                                    target_model=canonical.target_model,
                                    disposition=canonical.disposition,
                                    source_identity=canonical.source_identity,
                                    row_json=canonical_json_bytes(
                                        canonical.to_portable_dict()
                                    ).decode("utf-8"),
                                    physical_sources=physical_sources,
                                )
                            )
                        sessions.append_provisional_rows(
                            project.project_id,
                            session.session_id,
                            prepared_batch,
                        )
                        row_count += len(source_batch)
            dataset_evidence[effective.name] = (
                physical.dataset_id,
                StagingDatasetRole.DIRECT,
                row_count,
                mapping.target_model,
            )

        impact_sink.flush()
        run = sessions.finalize_session(
            project.project_id,
            session.session_id,
            modes=modes,
            field_sources=field_sources,
            dataset_evidence=dataset_evidence,
            run_issues=run_issues,
            control_totals=totals.report(),
            impact_report=impact_collector.report(),
        )
        return BoundedDirectPreparation(session_id=session.session_id, run=run)
    except Exception:
        sessions.fail_session(
            project.project_id,
            session.session_id,
            "BOUNDED_PREPARATION_FAILED",
        )
        raise
