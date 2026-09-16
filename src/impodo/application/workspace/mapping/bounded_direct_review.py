"""Bounded Stage-3 reviews for an unchanged direct source selection.

These projections deliberately stop before canonical preparation. They reuse the
same source batches and row transformer as bounded direct preparation, but only
produce the evidence requested by the review.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.application.shared.artifacts import ArtifactStoreError, GovernedArtifactStores
from impodo.application.workspace.preparation.bounded_preparation import (
    open_bounded_source,
    supports_bounded_direct_preparation,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    RowInclusionMode,
)
from impodo.domain.mapping.row_inclusion_review import (
    RowInclusionDatasetReview,
    RowInclusionReviewIdentity,
    RowInclusionReviewOutcome,
    RowInclusionReviewReport,
    RowInclusionReviewRow,
    describe_row_inclusion_policy,
)
from impodo.domain.mapping.source_conditions import SourceConditionValueError
from impodo.domain.preparation.source import SourceLoadError, SourceRow
from impodo.domain.source_binding import SourceOriginKind, require_file_source
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.staging.evaluator import (
    CompiledBrowserRowTransformer,
    compile_browser_row_transformer,
    row_inclusion_review_values,
)
from impodo.domain.staging.scale import (
    BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT,
    browser_evaluation_scale,
)
from impodo.domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
    _TransformationImpactCollector,
    reviewable_rule_impact_definitions,
)
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.workspace.workbench import WorkspaceState


def uses_bounded_direct_review(
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> bool:
    """Keep the qualified materialized route for smaller and derived reviews."""

    return (
        sum(item.row_count for item in physical_selection.datasets)
        > MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
        and supports_bounded_direct_preparation(
            physical_selection, effective_selection, plan
        )
    )


def _direct_batches(
    workspace_state: WorkspaceState,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: GovernedArtifactStores,
    source_snapshots: Iterable[SourceSnapshot],
    *,
    dataset_ids: frozenset[str] | None = None,
) -> Iterator[
    tuple[
        SourceDataset,
        DatasetMapping,
        CompiledBrowserRowTransformer,
        tuple[SourceRow, ...],
    ]
]:
    """Verify the frozen direct inputs and yield one source batch at a time."""

    scale = browser_evaluation_scale(
        physical_selection,
        supported_limit=BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT,
    )
    if not scale.supported:
        raise ReadinessError(
            f"This direct-mapping review contains {scale.physical_rows:,} "
            "source rows. This review can check up to "
            f"{scale.supported_limit:,} rows; no review data was changed."
        )
    if not supports_bounded_direct_preparation(
        physical_selection, effective_selection, None
    ) or physical_selection.data_version_id != effective_selection.data_version_id:
        raise ReadinessError("Bounded review requires unchanged direct datasets")
    if definition.source_selection_hash != effective_selection.content_hash:
        raise ReadinessError("The checked mapping no longer matches its source data")

    physical_by_id = {item.dataset_id: item for item in physical_selection.datasets}
    mapping_by_id = {item.dataset_id: item for item in definition.datasets}
    if (
        len(physical_by_id) != len(physical_selection.datasets)
        or len(mapping_by_id) != len(definition.datasets)
        or set(mapping_by_id) != set(physical_by_id)
    ):
        raise ReadinessError("Bounded review datasets are incomplete or duplicated")
    snapshots = tuple(source_snapshots)
    snapshot_by_id = {item.dataset_id: item for item in snapshots}
    if len(snapshot_by_id) != len(snapshots) or set(snapshot_by_id) != set(physical_by_id):
        raise ReadinessError("Frozen source snapshots are incomplete")
    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in workspace_state.source_files}

    for effective in effective_selection.datasets:
        if dataset_ids is not None and effective.dataset_id not in dataset_ids:
            continue
        physical = physical_by_id[effective.dataset_id]
        mapping = mapping_by_id[effective.dataset_id]
        source_file = None
        named_range = None
        if physical.origin is SourceOriginKind.FILE:
            binding = require_file_source(physical.source)
            source_file = source_file_by_id.get(binding.file_id)
            catalog = catalog_by_file.get(binding.file_id)
            table_catalog = next(
                (
                    item
                    for item in (catalog.tables if catalog else ())
                    if item.table_key == binding.table_key
                ),
                None,
            )
            if source_file is None or table_catalog is None:
                raise ReadinessError("Frozen source evidence is incomplete")
            if table_catalog.kind == "NAMED_TABLE" and table_catalog.named_tables:
                named_range = table_catalog.named_tables[0].cell_range
        elif physical.origin is not SourceOriginKind.ODOO:
            raise ReadinessError("Frozen source evidence is incomplete")
        transformer = compile_browser_row_transformer(
            effective, physical, mapping, None, "source"
        )
        count = 0
        try:
            with open_bounded_source(
                workspace_state,
                physical_selection,
                physical,
                source_file,
                artifacts,
                snapshot_by_id[physical.dataset_id],
                named_range=named_range,
            ) as source:
                if source.content_hash != physical.source_evidence_hash:
                    raise ReadinessError("Stored source content changed after selection")
                for batch in source.iter_batches():
                    count += len(batch)
                    if count > physical.row_count:
                        raise ReadinessError("Frozen source row count changed")
                    yield effective, mapping, transformer, batch
        except (ArtifactStoreError, SourceLoadError) as error:
            raise ReadinessError("The frozen source snapshot could not be verified") from error
        if count != physical.row_count:
            raise ReadinessError("Frozen source row count changed")


def direct_transformation_impact(
    workspace_state: WorkspaceState,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: GovernedArtifactStores,
    source_snapshots: Iterable[SourceSnapshot],
    write_impact: Callable[[TransformationImpactRow], None],
) -> TransformationImpactReport:
    """Stream changed-value facts without preparing canonical rows."""

    collector = _TransformationImpactCollector(
        mapping_content_hash=definition.content_hash,
        detail_limit=0,
        sink=write_impact,
    )
    for mapping in definition.datasets:
        for field in mapping.fields:
            for rule in reviewable_rule_impact_definitions(mapping.dataset_id, field):
                collector.register_rule(rule)
    for _effective, _mapping, transformer, batch in _direct_batches(
        workspace_state,
        definition,
        physical_selection,
        effective_selection,
        catalogs,
        artifacts,
        source_snapshots,
    ):
        for source_row in batch:
            projected = transformer.project(source_row)
            try:
                included = transformer.includes(projected)
            except SourceConditionValueError:
                continue
            if included:
                transformer.finish(projected, impact_collector=collector)
    return collector.report()


def direct_row_inclusion_review(
    workspace_state: WorkspaceState,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: GovernedArtifactStores,
    source_snapshots: Iterable[SourceSnapshot],
) -> RowInclusionReviewReport | None:
    """Evaluate source-only admission rules without transforming target rows."""

    matching = {
        item.dataset_id: item
        for item in definition.datasets
        if item.row_inclusion.mode is RowInclusionMode.MATCHING_ROWS
    }
    if not matching:
        return None
    rows: list[RowInclusionReviewRow] = []
    counts = {
        dataset_id: {outcome: 0 for outcome in RowInclusionReviewOutcome}
        for dataset_id in matching
    }
    sentences = {
        effective.dataset_id: describe_row_inclusion_policy(
            matching[effective.dataset_id].row_inclusion,
            {item.stable_key: item.source_name for item in effective.columns},
        )
        for effective in effective_selection.datasets
        if effective.dataset_id in matching
    }
    if set(sentences) != set(matching):
        raise ReadinessError("Row-inclusion review is missing current datasets")
    for effective, mapping, transformer, batch in _direct_batches(
        workspace_state,
        definition,
        physical_selection,
        effective_selection,
        catalogs,
        artifacts,
        source_snapshots,
        dataset_ids=frozenset(matching),
    ):
        sentence = sentences[effective.dataset_id]
        for source_row in batch:
            projected = transformer.project(source_row)
            values = row_inclusion_review_values(
                effective, mapping, projected.source_values
            )
            try:
                included = transformer.includes(projected)
            except SourceConditionValueError as error:
                outcome = RowInclusionReviewOutcome.CANNOT_EVALUATE
                message = str(error)
            else:
                outcome = (
                    RowInclusionReviewOutcome.INCLUDED
                    if included
                    else RowInclusionReviewOutcome.EXCLUDED
                )
                message = ""
            counts[effective.dataset_id][outcome] += 1
            rows.append(
                RowInclusionReviewRow(
                    dataset_id=effective.dataset_id,
                    dataset_name=effective.name,
                    source_row=source_row.number,
                    values=values,
                    outcome=outcome,
                    rule_sentence=sentence,
                    message=message,
                )
            )
    reviews = []
    for effective in effective_selection.datasets:
        if effective.dataset_id not in matching:
            continue
        tally = counts[effective.dataset_id]
        reviews.append(
            RowInclusionDatasetReview(
                dataset_id=effective.dataset_id,
                dataset_name=effective.name,
                source_row_count=sum(tally.values()),
                included_count=tally[RowInclusionReviewOutcome.INCLUDED],
                excluded_count=tally[RowInclusionReviewOutcome.EXCLUDED],
                cannot_evaluate_count=tally[RowInclusionReviewOutcome.CANNOT_EVALUATE],
                rule_sentence=sentences[effective.dataset_id],
            )
        )
    return RowInclusionReviewReport(
        identity=RowInclusionReviewIdentity(
            physical_selection_hash=physical_selection.content_hash,
            source_selection_hash=effective_selection.content_hash,
            mapping_content_hash=definition.content_hash,
            schema_hash=definition.schema_hash,
            derived_plan_hash=None,
        ),
        datasets=tuple(reviews),
        rows=tuple(rows),
    )
