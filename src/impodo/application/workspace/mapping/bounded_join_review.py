"""Review logical join outputs using the same bounded producers as preparation.

Only source columns used by Rows to use are projected. Physical inputs remain
bounded by the join engine; review memory is bounded by logical output rows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping

from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    GroupedExactJoinSourcePort,
    bounded_preparation_producers,
    iter_bounded_preparation_source_batches,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    RowInclusionMode,
)
from impodo.domain.mapping.row_inclusion_review import RowInclusionReviewReport
from impodo.domain.mapping.source_conditions import SourceConditionValueError
from impodo.domain.preparation.source import SourceRow
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.staging.evaluator import (
    CompiledBrowserRowTransformer,
    compile_browser_row_transformer,
)
from impodo.domain.staging.scale import BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT
from impodo.domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
    _TransformationImpactCollector,
    reviewable_rule_impact_definitions,
)
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    derived_dataset_links,
)
from impodo.domain.workspace.workbench import WorkspaceState

from .bounded_direct_review import row_inclusion_review_from_batches


def supports_bounded_join_review(
    physical: SourceSelection,
    effective: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> bool:
    """Qualify reviewed joins and their independently bounded sibling outputs."""
    producers = bounded_preparation_producers(physical, effective, plan)
    return producers is not None and any(item.group is not None for item in producers)


def joined_row_inclusion_review(
    workspace: WorkspaceState,
    definition: MappingDefinition,
    physical: SourceSelection,
    effective: SourceSelection,
    plan: DerivedEntityPlan,
    artifacts: GovernedArtifactStores,
    source: GroupedExactJoinSourcePort,
    snapshots: Iterable[SourceSnapshot],
) -> RowInclusionReviewReport | None:
    """Check logical row admission without staging physical source tables."""
    matching = {
        item.dataset_id: item
        for item in definition.datasets
        if item.row_inclusion.mode is RowInclusionMode.MATCHING_ROWS
    }
    if not matching:
        return None
    output_rows = sum(
        item.row_count for item in effective.datasets if item.dataset_id in matching
    )
    if output_rows > BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
        raise ReadinessError(
            f"This Rows to use review contains {output_rows:,} logical output rows. "
            f"The review supports up to {BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:,} "
            "output rows per check; no review data was changed."
        )
    keys = {
        dataset_id: tuple(
            dict.fromkeys(
                condition.source_column_key
                for condition in item.row_inclusion.conditions
            )
        )
        for dataset_id, item in matching.items()
    }

    return row_inclusion_review_from_batches(
        definition,
        physical,
        effective,
        _joined_review_batches(
            workspace,
            definition,
            physical,
            effective,
            plan,
            artifacts,
            source,
            snapshots,
            keys,
        ),
        derived_plan_hash=plan.content_hash,
    )


def joined_transformation_impact(
    workspace: WorkspaceState,
    definition: MappingDefinition,
    physical: SourceSelection,
    effective: SourceSelection,
    plan: DerivedEntityPlan,
    artifacts: GovernedArtifactStores,
    source: GroupedExactJoinSourcePort,
    snapshots: Iterable[SourceSnapshot],
    write_impact: Callable[[TransformationImpactRow], None],
) -> TransformationImpactReport:
    """Stream rule effects from verified logical outputs without canonical staging."""
    rows = sum(item.row_count for item in effective.datasets)
    if rows > BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:
        raise ReadinessError(
            f"This rule-effects review contains {rows:,} logical output rows. "
            f"The review supports up to {BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT:,} "
            "output rows per check; no review data was changed."
        )
    collector = _TransformationImpactCollector(
        mapping_content_hash=definition.content_hash,
        detail_limit=0,
        sink=write_impact,
    )
    for mapping in definition.datasets:
        for field in mapping.fields:
            for rule in reviewable_rule_impact_definitions(mapping.dataset_id, field):
                collector.register_rule(rule)
    for _dataset, _mapping, transformer, batch in _joined_review_batches(
        workspace,
        definition,
        physical,
        effective,
        plan,
        artifacts,
        source,
        snapshots,
        None,
    ):
        for row in batch:
            projected = transformer.project(row)
            try:
                included = transformer.includes(projected)
            except SourceConditionValueError:
                continue
            if included:
                transformer.finish(projected, impact_collector=collector)
    return collector.report()


def _joined_review_batches(
    workspace: WorkspaceState,
    definition: MappingDefinition,
    physical: SourceSelection,
    effective: SourceSelection,
    plan: DerivedEntityPlan,
    artifacts: GovernedArtifactStores,
    source: GroupedExactJoinSourcePort,
    snapshots: Iterable[SourceSnapshot],
    keys: Mapping[str, tuple[str, ...]] | None,
) -> Iterator[
    tuple[
        SourceDataset,
        DatasetMapping,
        CompiledBrowserRowTransformer,
        tuple[SourceRow, ...],
    ]
]:
    """Use one source producer and transformer for each requested logical output."""
    mappings = {item.dataset_id: item for item in definition.datasets}
    lookups = tuple(
        rule
        for rule in plan.rules
        if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule))
    )
    lookup_bindings = tuple(zip(lookups, derived_dataset_links(plan), strict=True))
    transformers: dict[str, CompiledBrowserRowTransformer] = {}
    for dataset, batch in iter_bounded_preparation_source_batches(
        workspace,
        definition,
        physical,
        effective,
        plan,
        artifacts,
        source,
        snapshots,
        column_keys=keys,
    ):
        mapping = mappings[dataset.dataset_id]
        transformer = transformers.get(dataset.dataset_id)
        if transformer is None:
            transformer = compile_browser_row_transformer(
                dataset,
                dataset,
                mapping,
                None,
                "source",
                lookup_bindings=lookup_bindings,
            )
            transformers[dataset.dataset_id] = transformer
        yield dataset, mapping, transformer, batch.rows
