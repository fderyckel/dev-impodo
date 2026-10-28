"""Prepare one reviewed grouped exact join through bounded source batches.

The qualified route is deliberately narrower than the general structural
evaluator.  It accepts one left-deep group of reviewed exact joins, streams the
final mapped dataset from a request-scoped columnar adapter, and publishes the
same durable canonical session evidence used by direct preparation.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
)
from impodo.application.shared.artifacts import (
    ArtifactStoreError,
    GovernedArtifactStores,
)
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import MappingDefinition, RowInclusionMode
from impodo.domain.mapping.source_conditions import SourceConditionValueError
from impodo.domain.preparation.source import (
    CompiledPreparedRowTransformer,
    SourceLoadError,
    SourceRow,
)
from impodo.domain.preparation.staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    StagingDatasetRole,
    StagingDisposition,
    canonical_row_from_inclusion_decision,
    canonical_row_from_prepared,
)
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import (
    Issue,
    PreparedRecord,
    Severity,
    canonical_json_bytes,
)
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.staging.canonical_projection import (
    canonical_quality_identity_key,
    canonical_quality_record_label,
)
from impodo.domain.staging.control_totals import CompiledControlTotalAccumulator
from impodo.domain.staging.evaluator import (
    canonical_field_sources,
    compile_browser_row_transformer,
    compile_reference_indexes,
)
from impodo.domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    StoredCanonicalStagingRun,
)
from impodo.domain.staging.transformation_impact import (
    TransformationImpactRow,
    _TransformationImpactCollector,
    reviewable_rule_impact_definitions,
)
from impodo.domain.structural import ExactJoinRule, JoinKind, structural_dataset_id
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.workspace.workbench import WorkspaceState

from .readiness_ports import PreparationSessionRepository

GROUPED_EXACT_JOIN_INPUT_ROW_LIMIT = 100_000
GROUPED_EXACT_JOIN_OUTPUT_ROW_LIMIT = 50_000
GROUPED_EXACT_JOIN_BATCH_ROWS = 1_000
_SESSION_BATCH_ROWS = 5_000


@dataclass(frozen=True, slots=True)
class BoundedExactJoinGroup:
    """Validated left-deep exact-join group admitted to bounded preparation."""

    rules: tuple[ExactJoinRule, ...]
    primary_dataset_id: str
    participant_dataset_ids: tuple[str, ...]
    output_dataset_id: str


@dataclass(frozen=True, slots=True)
class GroupedExactJoinBatch:
    """One bounded final-output page and its complete physical lineage."""

    rows: tuple[SourceRow, ...]
    physical_sources: tuple[Mapping[str, tuple[int, ...]], ...]

    def __post_init__(self) -> None:
        if len(self.rows) != len(self.physical_sources):
            raise ValueError("Grouped exact-join batch lineage is incomplete")


class GroupedExactJoinSourcePort(Protocol):
    """Stream a reviewed exact-join group from verified immutable snapshots."""

    def iter_batches(
        self,
        *,
        group: BoundedExactJoinGroup,
        physical_selection: SourceSelection,
        effective_dataset: SourceDataset,
        snapshots: Mapping[str, SourceSnapshot],
        paths: Mapping[str, Path],
        batch_size: int,
    ) -> Iterator[GroupedExactJoinBatch]:
        """Yield final joined rows without materializing the complete table."""
        ...


@dataclass(frozen=True, slots=True)
class BoundedGroupedExactJoinPreparation:
    """READY grouped-join session plus its stored canonical projection."""

    session_id: str
    run: StoredCanonicalStagingRun


class _ImpactBatchSink:
    def __init__(
        self,
        repository: PreparationSessionRepository,
        workspace_id: str,
        session_id: str,
    ) -> None:
        self.repository = repository
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.rows: list[TransformationImpactRow] = []

    def add(self, row: TransformationImpactRow) -> None:
        self.rows.append(row)
        if len(self.rows) >= _SESSION_BATCH_ROWS:
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


def bounded_exact_join_group(
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> BoundedExactJoinGroup | None:
    """Return the safely qualified grouped exact-join route, if present."""

    if plan is None or not plan.rules or len(effective_selection.datasets) != 1:
        return None
    if plan.source_selection_hash != physical_selection.content_hash:
        return None
    if any(not isinstance(rule, ExactJoinRule) for rule in plan.rules):
        return None
    rules = tuple(
        sorted(
            (rule for rule in plan.rules if isinstance(rule, ExactJoinRule)),
            key=lambda rule: rule.group_step or 0,
        )
    )
    group_ids = {rule.group_id for rule in rules}
    if None in group_ids or len(group_ids) != 1:
        return None
    if tuple(rule.group_step for rule in rules) != tuple(range(1, len(rules) + 1)):
        return None
    if any(rule.kind is not JoinKind.LEFT for rule in rules):
        return None
    if any(rule.map_output != (index == len(rules)) for index, rule in enumerate(rules, 1)):
        return None

    physical_ids = {dataset.dataset_id for dataset in physical_selection.datasets}
    primary_id = rules[0].left_dataset_id
    participant_ids = (primary_id, *(rule.right_dataset_id for rule in rules))
    if (
        primary_id not in physical_ids
        or len(set(participant_ids)) != len(participant_ids)
        or set(participant_ids) != physical_ids
    ):
        return None
    for previous, rule in itertools.pairwise(rules):
        if rule.left_dataset_id != structural_dataset_id(previous):
            return None
    final_id = structural_dataset_id(rules[-1])
    effective = effective_selection.datasets[0]
    if effective.dataset_id != final_id:
        return None
    if effective.row_count > GROUPED_EXACT_JOIN_OUTPUT_ROW_LIMIT:
        return None
    if (
        sum(dataset.row_count for dataset in physical_selection.datasets)
        > GROUPED_EXACT_JOIN_INPUT_ROW_LIMIT
    ):
        return None
    reviews = {review.rule_id: review for review in plan.join_reviews}
    if set(reviews) != {rule.rule_id for rule in rules}:
        return None
    if any(
        not reviews[rule.rule_id].review.can_save
        or reviews[rule.rule_id].review.actual_output_rows != effective.row_count
        for rule in rules
    ):
        return None
    return BoundedExactJoinGroup(
        rules=rules,
        primary_dataset_id=primary_id,
        participant_dataset_ids=participant_ids,
        output_dataset_id=final_id,
    )


def prepare_bounded_grouped_exact_join_session(
    workspace_state: WorkspaceState,
    definition: MappingDefinition,
    mapping_version: int,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan,
    artifacts: GovernedArtifactStores,
    reference_bundle: ReferenceBundle | None,
    sessions: PreparationSessionRepository,
    source: GroupedExactJoinSourcePort,
    *,
    actor: Actor,
    source_snapshots: Iterable[SourceSnapshot],
    batch_progress: Callable[[int, int], None] | None = None,
) -> BoundedGroupedExactJoinPreparation:
    """Stream one qualified grouped exact join into durable canonical staging."""

    group = bounded_exact_join_group(physical_selection, effective_selection, plan)
    if group is None:
        raise ReadinessError("Bounded grouped exact-join preparation is unavailable")
    if physical_selection.data_version_id != effective_selection.data_version_id:
        raise ReadinessError("Canonical evaluation evidence belongs to another DataVersion")
    if reference_bundle is not None and reference_bundle.workspace_id != workspace_state.workspace_id:
        raise ReadinessError("Reference data belongs to another workspace")
    if definition.source_selection_hash != effective_selection.content_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    if len(definition.datasets) != 1:
        raise ReadinessError("Grouped exact-join mapping must contain one final dataset")

    snapshots = tuple(source_snapshots)
    snapshot_by_id = {snapshot.dataset_id: snapshot for snapshot in snapshots}
    physical_by_id = {dataset.dataset_id: dataset for dataset in physical_selection.datasets}
    if len(snapshot_by_id) != len(snapshots) or set(snapshot_by_id) != set(physical_by_id):
        raise ReadinessError("Frozen source snapshots are incomplete")
    current_snapshots = {
        (snapshot.dataset_id, snapshot.logical_hash, snapshot.parquet_sha256)
        for snapshot in snapshots
    }
    if any(
        {
            (item.dataset_id, item.logical_hash, item.parquet_sha256)
            for item in review.input_snapshots
        }
        != current_snapshots
        for review in plan.join_reviews
    ):
        raise ReadinessError("The accepted grouped exact-join review is stale")
    for dataset_id, dataset in physical_by_id.items():
        snapshot = snapshot_by_id[dataset_id]
        validate_snapshot_for_dataset(physical_selection, dataset, snapshot)

    effective = effective_selection.datasets[0]
    mapping = definition.datasets[0]
    if mapping.dataset_id != effective.dataset_id:
        raise ReadinessError("Grouped exact-join mapping dataset is inconsistent")
    compiled_plan = compile_browser_mapping(
        definition,
        effective_selection,
        derived_plan_hash=plan.content_hash,
    )
    compiled_dataset = compiled_plan.datasets[0]
    reference_indexes = compile_reference_indexes(reference_bundle)
    transformer = compile_browser_row_transformer(
        effective,
        effective,
        mapping,
        None,
        "source",
        reference_indexes=reference_indexes,
    )
    preparer = CompiledPreparedRowTransformer.compile(
        compiled_dataset,
        transformer.headers,
    )
    source_hashes = {effective.name: effective.source_evidence_hash}
    field_sources = canonical_field_sources(definition, effective_selection)
    session = sessions.begin_direct_session(
        workspace_state.workspace_id,
        PreparationSessionBindings(
            mapping_id=definition.mapping_id,
            mapping_version=mapping_version,
            physical_selection_hash=physical_selection.content_hash,
            source_selection_hash=effective_selection.content_hash,
            mapping_hash=definition.content_hash,
            schema_hash=definition.schema_hash,
            derived_plan_hash=plan.content_hash,
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
        mapping_content_hash=definition.content_hash,
        detail_limit=0,
        sink=impact_sink.add,
    )
    for field in mapping.fields:
        for rule in reviewable_rule_impact_definitions(mapping.dataset_id, field):
            impact_collector.register_rule(rule)
    totals = CompiledControlTotalAccumulator.compile(definition, effective_selection)
    run_issues: list[Issue] = []
    if preparer.dataset_issue is not None:
        run_issues.append(preparer.dataset_issue)
    row_count = 0
    included_row_count = 0
    total_physical_rows = sum(item.row_count for item in physical_selection.datasets)

    try:
        with ExitStack() as stack:
            paths = {
                dataset_id: stack.enter_context(
                    artifacts.materialize_source_snapshot(
                        physical_selection.data_version_id,
                        snapshot_by_id[dataset_id].parquet_storage_key,
                        expected_sha256=snapshot_by_id[dataset_id].parquet_sha256,
                    )
                )
                for dataset_id in group.participant_dataset_ids
            }
            for batch in source.iter_batches(
                group=group,
                physical_selection=physical_selection,
                effective_dataset=effective,
                snapshots=snapshot_by_id,
                paths=paths,
                batch_size=GROUPED_EXACT_JOIN_BATCH_ROWS,
            ):
                prepared_rows: list[CanonicalPreparedSessionRow] = []
                for joined_row, physical_sources in zip(
                    batch.rows,
                    batch.physical_sources,
                    strict=True,
                ):
                    projected = transformer.project(joined_row)
                    try:
                        included = transformer.includes(projected)
                    except SourceConditionValueError as error:
                        prepared_rows.append(
                            _inclusion_session_row(
                                dataset=effective.name,
                                source_row=joined_row.number,
                                target_model=mapping.target_model,
                                disposition=StagingDisposition.BLOCKED,
                                issues=(
                                    Issue(
                                        code="ROW_INCLUSION_SOURCE_VALUE_INVALID",
                                        message=str(error),
                                        severity=Severity.ERROR,
                                        dataset=effective.name,
                                        row=joined_row.number,
                                        field=error.source_column_key,
                                    ),
                                ),
                                ordinal=row_count + len(prepared_rows),
                                source_hash=source_hashes[effective.name],
                                source_selection_hash=effective_selection.content_hash,
                                mapping_hash=definition.content_hash,
                                schema_hash=definition.schema_hash,
                                derived_plan_hash=plan.content_hash,
                                source_column_keys=tuple(
                                    condition.source_column_key
                                    for condition in mapping.row_inclusion.conditions
                                ),
                                primary_dataset_id=group.primary_dataset_id,
                                physical_sources=physical_sources,
                            )
                        )
                        continue
                    if not included:
                        prepared_rows.append(
                            _inclusion_session_row(
                                dataset=effective.name,
                                source_row=joined_row.number,
                                target_model=mapping.target_model,
                                disposition=StagingDisposition.EXCLUDED,
                                issues=(),
                                ordinal=row_count + len(prepared_rows),
                                source_hash=source_hashes[effective.name],
                                source_selection_hash=effective_selection.content_hash,
                                mapping_hash=definition.content_hash,
                                schema_hash=definition.schema_hash,
                                derived_plan_hash=plan.content_hash,
                                source_column_keys=tuple(
                                    condition.source_column_key
                                    for condition in mapping.row_inclusion.conditions
                                ),
                                primary_dataset_id=group.primary_dataset_id,
                                physical_sources=physical_sources,
                            )
                        )
                        continue
                    staged_row, preparation_issues = transformer.finish(
                        projected,
                        impact_collector=impact_collector,
                    )
                    record = preparer.transform(staged_row)
                    included_row_count += 1
                    if preparation_issues:
                        record = replace(record, issues=(*record.issues, *preparation_issues))
                    totals.add(record)
                    prepared_rows.append(
                        _canonical_session_row(
                            record,
                            source_row=joined_row.number,
                            ordinal=row_count + len(prepared_rows),
                            mode=compiled_dataset.target.mode,
                            source_hash=source_hashes[effective.name],
                            source_selection_hash=effective_selection.content_hash,
                            mapping_hash=definition.content_hash,
                            schema_hash=definition.schema_hash,
                            derived_plan_hash=plan.content_hash,
                            field_sources=field_sources.get(effective.name, {}),
                            primary_dataset_id=group.primary_dataset_id,
                            physical_sources=physical_sources,
                        )
                    )
                sessions.append_direct_rows(
                    workspace_state.workspace_id,
                    session.session_id,
                    prepared_rows,
                )
                row_count += len(batch.rows)
                if batch_progress is not None:
                    batch_progress(row_count, total_physical_rows)

        if row_count != effective.row_count:
            raise ReadinessError("Grouped exact-join output row count changed")
        if mapping.row_inclusion.mode is RowInclusionMode.MATCHING_ROWS and included_row_count == 0:
            run_issues.append(
                Issue(
                    code="ROW_INCLUSION_ZERO_INCLUDED",
                    message=(
                        "The row-inclusion rule did not include any rows. "
                        "Review the rule before continuing."
                    ),
                    severity=Severity.ERROR,
                    dataset=effective.name,
                )
            )
        impact_sink.flush()
        run = sessions.finalize_direct_session(
            workspace_state.workspace_id,
            session.session_id,
            dataset_evidence={
                effective.name: (
                    group.primary_dataset_id,
                    StagingDatasetRole.JOIN,
                    total_physical_rows,
                    mapping.target_model,
                )
            },
            run_issues=run_issues,
            control_totals=totals.report(),
            impact_report=impact_collector.report(),
        )
        if batch_progress is not None:
            batch_progress(total_physical_rows, total_physical_rows)
        return BoundedGroupedExactJoinPreparation(session.session_id, run)
    except (ArtifactStoreError, OSError, SourceLoadError, ValueError) as error:
        sessions.fail_session(
            workspace_state.workspace_id,
            session.session_id,
            "BOUNDED_GROUPED_JOIN_FAILED",
        )
        if isinstance(error, ReadinessError):
            raise
        raise ReadinessError(
            "The reviewed grouped exact-join source data could not be verified"
        ) from error
    except Exception:
        sessions.fail_session(
            workspace_state.workspace_id,
            session.session_id,
            "BOUNDED_GROUPED_JOIN_FAILED",
        )
        raise


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
    derived_plan_hash: str,
    field_sources: Mapping[str, tuple[str, ...]],
    primary_dataset_id: str,
    physical_sources: Mapping[str, tuple[int, ...]],
) -> CanonicalPreparedSessionRow:
    primary_rows = physical_sources.get(primary_dataset_id, ())
    canonical = canonical_row_from_prepared(
        record,
        mode=mode,
        source_hash=source_hash,
        source_selection_hash=source_selection_hash,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        derived_plan_hash=derived_plan_hash,
        field_sources=field_sources,
        physical_dataset_id=primary_dataset_id,
        physical_source_rows=primary_rows,
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


def _inclusion_session_row(
    *,
    dataset: str,
    source_row: int,
    target_model: str,
    disposition: StagingDisposition,
    issues: Sequence[Issue],
    ordinal: int,
    source_hash: str,
    source_selection_hash: str,
    mapping_hash: str,
    schema_hash: str,
    derived_plan_hash: str,
    source_column_keys: tuple[str, ...],
    primary_dataset_id: str,
    physical_sources: Mapping[str, tuple[int, ...]],
) -> CanonicalPreparedSessionRow:
    canonical = canonical_row_from_inclusion_decision(
        dataset=dataset,
        source_row=source_row,
        target_model=target_model,
        disposition=disposition,
        issues=issues,
        source_hash=source_hash,
        source_selection_hash=source_selection_hash,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        derived_plan_hash=derived_plan_hash,
        source_column_keys=source_column_keys,
        physical_dataset_id=primary_dataset_id,
        physical_source_rows=physical_sources.get(primary_dataset_id, ()),
        physical_sources=physical_sources,
    )
    return CanonicalPreparedSessionRow(
        row_id=canonical.row_id,
        ordinal=ordinal,
        dataset=canonical.dataset,
        source_row=canonical.source_row,
        target_model=canonical.target_model,
        disposition=canonical.disposition,
        source_identity=(),
        row_json=canonical_json_bytes(canonical.to_portable_dict()).decode("utf-8"),
        references={},
        physical_sources=physical_sources,
        record_label=f"Source row {source_row}",
        quality_identity_key=None,
        issues=canonical.issues,
    )
