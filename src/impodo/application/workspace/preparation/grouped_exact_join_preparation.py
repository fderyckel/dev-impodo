"""Compose reviewed exact joins and frozen tables in one bounded session.

Each output is qualified independently. Producers stream bounded pages while
one workspace session owns canonical evidence, control totals, and impacts.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
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
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    RowInclusionMode,
)
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
from impodo.domain.serialization import content_hash
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
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    derived_dataset_links,
)
from impodo.domain.workspace.workbench import WorkspaceState

from .bounded_preparation import open_bounded_source
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
    issues: tuple[tuple[Issue, ...], ...] = ()

    def __post_init__(self) -> None:
        if len(self.rows) != len(self.physical_sources):
            raise ValueError("Grouped exact-join batch lineage is incomplete")
        if self.issues and len(self.issues) != len(self.rows):
            raise ValueError("Prepared source batch findings are incomplete")


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

    def iter_lookup_batches(
        self, *, rule: DerivedEntityRule, link: DerivedDatasetLink,
        effective_dataset: SourceDataset, snapshot: SourceSnapshot, path: Path,
        batch_size: int,
    ) -> Iterator[GroupedExactJoinBatch]:
        """Group one frozen value column with complete physical lineage."""
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
    *,
    dataset_id: str | None = None,
) -> BoundedExactJoinGroup | None:
    """Qualify a reviewed group, optionally scoped to one value-read output.

    The default qualifies an exclusive group. Preparation and value reads can
    qualify each selected output beside other independently mapped datasets.
    """

    if plan is None or not plan.rules:
        return None
    if plan.source_selection_hash != physical_selection.content_hash:
        return None
    if dataset_id is None:
        if len(effective_selection.datasets) != 1 or any(
            not isinstance(rule, ExactJoinRule) for rule in plan.rules
        ):
            return None
        selected_rules = plan.rules
    else:
        final = next(
            (
                rule for rule in plan.rules
                if isinstance(rule, ExactJoinRule)
                and rule.map_output
                and structural_dataset_id(rule) == dataset_id
            ),
            None,
        )
        if final is None or final.group_id is None:
            return None
        selected_rules = tuple(
            rule for rule in plan.rules
            if isinstance(rule, ExactJoinRule) and rule.group_id == final.group_id
        )
    rules = tuple(
        sorted(
            (rule for rule in selected_rules if isinstance(rule, ExactJoinRule)),
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
        or not set(participant_ids).issubset(physical_ids)
        or (dataset_id is None and set(participant_ids) != physical_ids)
    ):
        return None
    for previous, rule in itertools.pairwise(rules):
        if rule.left_dataset_id != structural_dataset_id(previous):
            return None
    final_id = structural_dataset_id(rules[-1])
    effective = next(
        (item for item in effective_selection.datasets if item.dataset_id == final_id),
        None,
    )
    if effective is None or (dataset_id is not None and dataset_id != final_id):
        return None
    rule_ids = {rule.rule_id for rule in rules}
    selected_reviews = tuple(
        review for review in plan.join_reviews
        if dataset_id is None or review.rule_id in rule_ids
    )
    reviews = {review.rule_id: review for review in selected_reviews}
    if len(reviews) != len(selected_reviews) or set(reviews) != rule_ids:
        return None
    if any(
        not reviews[rule.rule_id].review.can_save
        or reviews[rule.rule_id].rule_hash != content_hash(rule.to_dict())
        or reviews[rule.rule_id].source_selection_hash != physical_selection.content_hash
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



@dataclass(frozen=True, slots=True)
class BoundedPreparationProducer:
    """One logical output and its independently verified physical dependencies."""

    dataset: SourceDataset
    group: BoundedExactJoinGroup | None
    primary_dataset_id: str
    participant_dataset_ids: tuple[str, ...]
    lookup: tuple[DerivedEntityRule, DerivedDatasetLink] | None = None


def bounded_preparation_producers(
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> tuple[BoundedPreparationProducer, ...] | None:
    """Qualify every output without reading rows or relaxing join review binds."""

    if plan is None or plan.source_selection_hash != physical_selection.content_hash:
        return None
    if physical_selection.data_version_id != effective_selection.data_version_id:
        return None
    physical = {item.dataset_id: item for item in physical_selection.datasets}
    if len(physical) != len(physical_selection.datasets):
        return None
    outputs = sorted(effective_selection.datasets, key=lambda item: item.name)
    if not outputs or len({item.dataset_id for item in outputs}) != len(outputs):
        return None
    producers = []
    lookup_rules = tuple(rule for rule in plan.rules
                         if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule)))
    lookups = {link.derived_dataset_id: (rule, link)
               for rule, link in zip(lookup_rules, derived_dataset_links(plan), strict=True)}
    for dataset in outputs:
        lookup = lookups.get(dataset.dataset_id)
        group = bounded_exact_join_group(
            physical_selection, effective_selection, plan, dataset_id=dataset.dataset_id,
        )
        if group is not None:
            producers.append(BoundedPreparationProducer(
                dataset, group, group.primary_dataset_id, group.participant_dataset_ids,
            ))
        elif physical.get(dataset.dataset_id) == dataset:
            producers.append(BoundedPreparationProducer(
                dataset, None, dataset.dataset_id, (dataset.dataset_id,),
            ))
        elif (lookup is not None and isinstance(lookup[0], DerivedEntityRule)
              and lookup[0].parent_separator is None
              and lookup[0].source_dataset_id in physical
              and lookup[0].source_column_key in {
                  column.stable_key for column in physical[lookup[0].source_dataset_id].columns
              }):
            rule, link = lookup[0], lookup[1]
            producers.append(BoundedPreparationProducer(
                dataset, None, rule.source_dataset_id, (rule.source_dataset_id,),
                (rule, link),
            ))
        else:
            return None
    return tuple(producers)


def _producer_batches(
    producer: BoundedPreparationProducer,
    workspace_state: WorkspaceState,
    physical_selection: SourceSelection,
    artifacts: GovernedArtifactStores,
    snapshots: Mapping[str, SourceSnapshot],
    source: GroupedExactJoinSourcePort,
    column_keys: tuple[str, ...],
) -> Iterator[GroupedExactJoinBatch]:
    """Read each producer's frozen inputs once and retain bounded lineage pages."""

    if producer.lookup is not None:
        rule, link = producer.lookup
        snapshot = snapshots[rule.source_dataset_id]
        with artifacts.materialize_source_snapshot(
            physical_selection.data_version_id, snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            yield from source.iter_lookup_batches(
                rule=rule, link=link, effective_dataset=producer.dataset,
                snapshot=snapshot, path=path, batch_size=GROUPED_EXACT_JOIN_BATCH_ROWS,
            )
        return
    if producer.group is None:
        with open_bounded_source(
            workspace_state, physical_selection, producer.dataset, None, artifacts,
            snapshots[producer.dataset.dataset_id], named_range=None,
        ) as reader:
            if reader.content_hash != producer.dataset.source_evidence_hash:
                raise ReadinessError("Stored source content changed after selection")
            for rows in reader.iter_batches():
                yield GroupedExactJoinBatch(
                    tuple(rows),
                    tuple({producer.primary_dataset_id: (row.number,)} for row in rows),
                )
        return
    with ExitStack() as stack:
        paths = {
            dataset_id: stack.enter_context(artifacts.materialize_source_snapshot(
                physical_selection.data_version_id,
                snapshots[dataset_id].parquet_storage_key,
                expected_sha256=snapshots[dataset_id].parquet_sha256,
            ))
            for dataset_id in producer.participant_dataset_ids
        }
        yield from source.iter_batches(
            group=producer.group,
            physical_selection=physical_selection,
            effective_dataset=replace(producer.dataset, columns=tuple(
                column for column in producer.dataset.columns if column.stable_key in column_keys
            )),
            snapshots=snapshots,
            paths=paths,
            batch_size=GROUPED_EXACT_JOIN_BATCH_ROWS,
        )


def _mapping_column_keys(mapping: DatasetMapping, dataset: SourceDataset) -> tuple[str, ...]:
    """Project authored source dependencies; retain every column for formulas."""

    keys: set[str] = set()
    has_formula = False

    def visit(value: object) -> None:
        nonlocal has_formula
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "formula" and item:
                    has_formula = True
                if key in {"source_column_key", "source_column_keys", "source_identity_column_keys", "key_source_column_keys"}:
                    if isinstance(item, str):
                        keys.add(item)
                    elif isinstance(item, (tuple, list)):
                        keys.update(part for part in item if isinstance(part, str))
                visit(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    visit(asdict(mapping))
    return tuple(column.stable_key for column in dataset.columns
                 if has_formula or column.stable_key in keys)


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
    """Stream all qualified outputs into one durable canonical staging session."""

    physical_selection_hash = physical_selection.content_hash
    source_selection_hash = effective_selection.content_hash
    mapping_hash = definition.content_hash
    schema_hash = definition.schema_hash
    derived_plan_hash = plan.content_hash
    producers = bounded_preparation_producers(physical_selection, effective_selection, plan)
    if producers is None:
        raise ReadinessError("Bounded grouped exact-join preparation is unavailable")
    if physical_selection.data_version_id != effective_selection.data_version_id:
        raise ReadinessError("Canonical evaluation evidence belongs to another DataVersion")
    if reference_bundle is not None and reference_bundle.workspace_id != workspace_state.workspace_id:
        raise ReadinessError("Reference data belongs to another workspace")
    if definition.source_selection_hash != source_selection_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    mappings = {item.dataset_id: item for item in definition.datasets}
    if len(mappings) != len(definition.datasets) or set(mappings) != {
        producer.dataset.dataset_id for producer in producers
    }:
        raise ReadinessError("Prepared datasets are incomplete or duplicated")

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
        if review.rule_id in {rule.rule_id for producer in producers
                              if producer.group for rule in producer.group.rules}
    ):
        raise ReadinessError("The accepted grouped exact-join review is stale")
    for dataset_id, dataset in physical_by_id.items():
        snapshot = snapshot_by_id[dataset_id]
        validate_snapshot_for_dataset(physical_selection, dataset, snapshot)

    compiled_plan = compile_browser_mapping(
        definition, effective_selection, derived_plan_hash=derived_plan_hash,
    )
    compiled_by_name = {item.name: item for item in compiled_plan.datasets}
    reference_indexes = compile_reference_indexes(reference_bundle)
    lookup_rules = tuple(rule for rule in plan.rules
                         if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule)))
    lookup_bindings = tuple(zip(lookup_rules, derived_dataset_links(plan), strict=True))
    source_hashes = {
        item.name: item.source_evidence_hash for item in effective_selection.datasets
    }
    field_sources = canonical_field_sources(definition, effective_selection)
    session = sessions.begin_direct_session(
        workspace_state.workspace_id,
        PreparationSessionBindings(
            mapping_id=definition.mapping_id,
            mapping_version=mapping_version,
            physical_selection_hash=physical_selection_hash,
            source_selection_hash=source_selection_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            derived_plan_hash=derived_plan_hash,
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
    for mapping in definition.datasets:
        for field in mapping.fields:
            for rule in reviewable_rule_impact_definitions(mapping.dataset_id, field):
                impact_collector.register_rule(rule)
    totals = CompiledControlTotalAccumulator.compile(definition, effective_selection)
    run_issues: list[Issue] = []
    total_output_rows = sum(item.dataset.row_count for item in producers)
    ordinal_offset = 0
    dataset_evidence: dict[str, tuple[str, StagingDatasetRole, int, str]] = {}

    try:
        # Account for shared and unused physical inputs once, independently of
        # how many logical outputs consume them. This scan projects only ordinals.
        with sessions.operation_scope():
            for snapshot in snapshots:
                with artifacts.materialize_source_snapshot(
                    physical_selection.data_version_id, snapshot.parquet_storage_key,
                    expected_sha256=snapshot.parquet_sha256,
                ) as path:
                    sessions.register_physical_snapshot(
                        workspace_state.workspace_id, session.session_id, snapshot, path,
                    )
        with sessions.operation_scope():
            for producer in producers:
                effective = producer.dataset
                mapping = mappings[effective.dataset_id]
                compiled_dataset = compiled_by_name[effective.name]
                transformer = compile_browser_row_transformer(
                    effective, effective, mapping, None, "source",
                    lookup_bindings=lookup_bindings,
                    reference_indexes=reference_indexes,
                )
                preparer = CompiledPreparedRowTransformer.compile(
                    compiled_dataset, transformer.headers,
                )
                if preparer.dataset_issue is not None:
                    run_issues.append(preparer.dataset_issue)
                row_count = 0
                included_row_count = 0
                for batch in _producer_batches(
                    producer, workspace_state, physical_selection, artifacts,
                    snapshot_by_id, source, _mapping_column_keys(mapping, effective),
                ):
                    prepared_rows: list[CanonicalPreparedSessionRow] = []
                    for batch_index, (joined_row, physical_sources) in enumerate(zip(
                        batch.rows,
                        batch.physical_sources,
                        strict=True,
                    )):
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
                                    ordinal=ordinal_offset + row_count + len(prepared_rows),
                                    source_hash=source_hashes[effective.name],
                                    source_selection_hash=source_selection_hash,
                                    mapping_hash=mapping_hash,
                                    schema_hash=schema_hash,
                                    derived_plan_hash=derived_plan_hash,
                                    source_column_keys=tuple(
                                        condition.source_column_key
                                        for condition in mapping.row_inclusion.conditions
                                    ),
                                    primary_dataset_id=producer.primary_dataset_id,
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
                                    ordinal=ordinal_offset + row_count + len(prepared_rows),
                                    source_hash=source_hashes[effective.name],
                                    source_selection_hash=source_selection_hash,
                                    mapping_hash=mapping_hash,
                                    schema_hash=schema_hash,
                                    derived_plan_hash=derived_plan_hash,
                                    source_column_keys=tuple(
                                        condition.source_column_key
                                        for condition in mapping.row_inclusion.conditions
                                    ),
                                    primary_dataset_id=producer.primary_dataset_id,
                                    physical_sources=physical_sources,
                                )
                            )
                            continue
                        staged_row, preparation_issues = transformer.finish(
                            projected,
                            impact_collector=impact_collector,
                        )
                        if batch.issues:
                            preparation_issues = (*preparation_issues, *batch.issues[batch_index])
                        record = preparer.transform(staged_row)
                        included_row_count += 1
                        if preparation_issues:
                            record = replace(record, issues=(*record.issues, *preparation_issues))
                        totals.add(record)
                        prepared_rows.append(
                            _canonical_session_row(
                                record,
                                source_row=joined_row.number,
                                ordinal=ordinal_offset + row_count + len(prepared_rows),
                                mode=compiled_dataset.target.mode,
                                source_hash=source_hashes[effective.name],
                                source_selection_hash=source_selection_hash,
                                mapping_hash=mapping_hash,
                                schema_hash=schema_hash,
                                derived_plan_hash=derived_plan_hash,
                                field_sources=field_sources.get(effective.name, {}),
                                primary_dataset_id=producer.primary_dataset_id,
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
                        batch_progress(ordinal_offset + row_count, total_output_rows)

                if producer.lookup is None and row_count != effective.row_count:
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
                dataset_evidence[effective.name] = (
                    producer.primary_dataset_id,
                    (StagingDatasetRole.LOOKUP if producer.lookup else
                     StagingDatasetRole.JOIN if producer.group else StagingDatasetRole.DIRECT),
                    sum(physical_by_id[item].row_count for item in producer.participant_dataset_ids),
                    mapping.target_model,
                )
                ordinal_offset += row_count

            impact_sink.flush()
            run = sessions.finalize_direct_session(
                workspace_state.workspace_id,
                session.session_id,
                dataset_evidence=dataset_evidence,
                run_issues=run_issues,
                control_totals=totals.report(),
                impact_report=impact_collector.report(),
            )
            if batch_progress is not None:
                batch_progress(total_output_rows, total_output_rows)
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
