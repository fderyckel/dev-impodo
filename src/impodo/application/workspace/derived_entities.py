"""Workspace use cases and consumer-owned ports for derived datasets."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from impodo.application.data_version.source_snapshots import (
    load_source_snapshot_table,
    validate_snapshot_for_dataset,
)
from impodo.application.shared.artifacts import (
    ArtifactStoreError,
    DataVersionSourceArtifactStore,
)
from impodo.domain.preparation.source import SourceLoadError, SourceTable
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.staging.scale import (
    LOOKUP_DERIVED_BROWSER_EVALUATION_ROW_LIMIT,
    LOOKUP_DERIVED_OUTPUT_ROW_LIMIT,
    MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT,
)
from impodo.domain.structural import (
    MAX_STRUCTURAL_BRANCHES,
    MAX_STRUCTURAL_COLUMNS,
    MAX_STRUCTURAL_KEYS,
    ExactJoinLineageReference,
    ExactJoinReview,
    ExactJoinRule,
    GroupAggregateRule,
    JoinKey,
    JoinKind,
    StructuralError,
    StructuralOutputColumn,
    StructuralProjection,
    UnionAllRule,
    execute_structural_rules,
    review_exact_join,
    structural_dataset_id,
    structural_mapping_selection,
)
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityImpactReview,
    DerivedEntityPlan,
    DerivedEntityPreview,
    DerivedEntityRule,
    ExactJoinInputSnapshotEvidence,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    LookupRule,
    RelatedDatasetPreview,
    RelatedDatasetRule,
    SavedExactJoinReview,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    SourceFileCatalogView,
    _hierarchy_source_dataset,
    _related_source_dataset,
    _rule_dataset_names,
    _source_dataset,
    preparation_input_dataset_ids,
    preview_derived_entities,
    preview_related_datasets,
    reconcile_source_dataset_uses,
    review_derived_entities,
    source_dataset_uses_for_authoring,
    validate_source_dataset_uses,
)
from impodo.domain.workspace.errors import WorkspaceError


class DerivedSourceRepository(Protocol):
    """Provide the exact frozen source evidence used to author and preview rules."""

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        """Return the current frozen physical selection."""
        ...

    def get_source_catalogs(
        self, workspace_id: str
    ) -> tuple[SourceFileCatalogView, ...]:
        """Return bounded previews and field labels for the frozen sources."""
        ...

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        """Return the immutable full-row snapshots for the current Data version."""
        ...


class DerivedEntityRepository(Protocol):
    """Persist immutable source-preparation plan revisions and a current pointer."""

    def get_derived_entity_plan(self, workspace_id: str) -> DerivedEntityPlan | None:
        """Return the current source-preparation plan, if one exists."""
        ...

    def save_derived_entity_plan(
        self,
        workspace_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        """Append a plan at the expected parent version and invalidate mapping."""
        ...


@dataclass(frozen=True, slots=True)
class DerivedRulePreview:
    """Keep each saved rule's preview or explanation when another rule is invalid."""

    rule: LookupRule | RelatedDatasetRule
    preview: DerivedEntityPreview | RelatedDatasetPreview | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ExactJoinRequest:
    """Application input for one bounded, model-neutral exact enrichment."""

    output_dataset_name: str
    left_dataset_id: str
    right_dataset_id: str
    keys: tuple[JoinKey, ...]
    output_columns: tuple[StructuralOutputColumn, ...]
    projections: tuple[StructuralProjection, ...]
    kind: JoinKind = JoinKind.LEFT
    require_all_right_rows: bool = True
    rule_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        try:
            rule_id = str(UUID(self.rule_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Exact-join request identifier is invalid") from error
        object.__setattr__(self, "keys", tuple(self.keys))
        object.__setattr__(self, "output_columns", tuple(self.output_columns))
        object.__setattr__(self, "projections", tuple(self.projections))
        object.__setattr__(self, "kind", JoinKind(self.kind))
        object.__setattr__(self, "rule_id", rule_id)


@dataclass(frozen=True, slots=True)
class ExactJoinGroupAddition:
    """One physical table that enriches a grouped primary-table decision."""

    dataset_id: str
    key_column_keys: tuple[str, ...]
    kind: JoinKind = JoinKind.LEFT
    require_all_rows: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_column_keys", tuple(self.key_column_keys))
        object.__setattr__(self, "kind", JoinKind(self.kind))


@dataclass(frozen=True, slots=True)
class ExactJoinGroupProjection:
    """One selected and named field in a grouped exact-join output."""

    source_dataset_id: str
    source_column_key: str
    output_name: str


@dataclass(frozen=True, slots=True)
class ExactJoinGroupRequest:
    """Application input for one primary table and several enrichments."""

    output_dataset_name: str
    primary_dataset_id: str
    primary_key_column_keys: tuple[str, ...]
    key_value_types: tuple[str, ...]
    additions: tuple[ExactJoinGroupAddition, ...]
    projections: tuple[ExactJoinGroupProjection, ...]
    group_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        try:
            group_id = str(UUID(self.group_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Combination review identifier is invalid") from error
        object.__setattr__(
            self,
            "primary_key_column_keys",
            tuple(self.primary_key_column_keys),
        )
        object.__setattr__(self, "key_value_types", tuple(self.key_value_types))
        object.__setattr__(self, "additions", tuple(self.additions))
        object.__setattr__(self, "projections", tuple(self.projections))
        object.__setattr__(self, "group_id", group_id)


@dataclass(frozen=True, slots=True)
class ExactJoinGroupPreview:
    """Complete per-enrichment evidence for one grouped join decision."""

    rules: tuple[ExactJoinRule, ...]
    reviews: tuple[ExactJoinReview, ...]
    physical_snapshot_scans: int = 0
    statement_count: int = 0
    bounded_execution_plan_verified: bool = False

    def __post_init__(self) -> None:
        if not self.rules or len(self.rules) != len(self.reviews):
            raise ValueError("Grouped exact-join evidence is incomplete")

    @property
    def can_save(self) -> bool:
        return all(review.can_save for review in self.reviews)

    @property
    def final_rule(self) -> ExactJoinRule:
        return self.rules[-1]


class ExactJoinGroupEvaluator(Protocol):
    """Evaluate one grouped exact join over verified columnar snapshots."""

    def review(
        self,
        *,
        request: ExactJoinGroupRequest,
        rules: tuple[ExactJoinRule, ...],
        selection: SourceSelection,
        snapshots: Mapping[str, SourceSnapshot],
        paths: Mapping[str, Path],
    ) -> ExactJoinGroupPreview:
        """Return complete reconciliation and lineage evidence."""
        ...


def _join_group_intermediate_name(
    output_name: str,
    group_id: UUID,
    index: int,
) -> str:
    """Return a bounded private name that cannot collide across group reviews."""

    suffix = f"_join_{index}_{str(group_id)[:8]}"
    return f"{output_name[: 63 - len(suffix)]}{suffix}"


class DerivedEntityWorkspaceService:
    """Author and review deterministic derived-entity rules.

    Saved rules are bound to the current frozen physical selection and use an
    optimistic plan version. A one-field lookup review reads every row in the
    chosen frozen source table. An exact-join review reads the complete bounded
    physical selection and retains reproducible reconciliation evidence.
    Preparation later repeats each accepted rule without changing the source.
    """

    def __init__(
        self,
        sources: DerivedSourceRepository,
        derived_entities: DerivedEntityRepository,
        authorization: WorkspaceAuthorizationPolicy,
        artifacts: DataVersionSourceArtifactStore | None = None,
        group_evaluator: ExactJoinGroupEvaluator | None = None,
    ) -> None:
        self.sources = sources
        self.derived_entities = derived_entities
        self.authorization = authorization
        self.artifacts = artifacts
        self.group_evaluator = group_evaluator

    def get_derived_entity_plan(
        self,
        workspace_id: str,
    ) -> DerivedEntityPlan | None:
        """Return the current plan through the application-owned repository port."""

        return self.derived_entities.get_derived_entity_plan(workspace_id)

    def save_derived_entity_plan(
        self,
        workspace_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        """Publish Recipe preparation with fresh source-specific join evidence."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        reviewed_plan = self._review_recipe_join_group(workspace_id, plan)
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            reviewed_plan,
            expected_parent_version=expected_parent_version,
            actor=actor,
        )

    def _review_recipe_join_group(
        self,
        workspace_id: str,
        plan: DerivedEntityPlan,
    ) -> DerivedEntityPlan:
        if (
            plan.join_reviews
            or not plan.rules
            or any(not isinstance(rule, ExactJoinRule) for rule in plan.rules)
        ):
            return plan
        rules = tuple(
            sorted(
                (rule for rule in plan.rules if isinstance(rule, ExactJoinRule)),
                key=lambda rule: rule.group_step or 0,
            )
        )
        group_ids = {rule.group_id for rule in rules}
        if (
            len(group_ids) != 1
            or None in group_ids
            or tuple(rule.group_step for rule in rules)
            != tuple(range(1, len(rules) + 1))
        ):
            return plan
        selection, current = self._join_context(
            workspace_id,
            require_materialized_scale=self.group_evaluator is None,
        )
        if current is not None:
            raise WorkspaceError(
                "The source-preparation plan was modified by another request; reload it"
            )
        if plan.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The source-preparation plan is stale; rebuild it from the frozen datasets"
            )
        first = rules[0]
        request = ExactJoinGroupRequest(
            output_dataset_name=rules[-1].output_dataset_name,
            primary_dataset_id=first.left_dataset_id,
            primary_key_column_keys=tuple(key.left_column_key for key in first.keys),
            key_value_types=tuple(key.value_type for key in first.keys),
            additions=tuple(
                ExactJoinGroupAddition(
                    dataset_id=rule.right_dataset_id,
                    key_column_keys=tuple(key.right_column_key for key in rule.keys),
                    kind=rule.kind,
                    require_all_rows=rule.require_all_right_rows,
                )
                for rule in rules
            ),
            projections=(),
            group_id=str(first.group_id),
        )
        if self.group_evaluator is None:
            tables, snapshots = self._load_join_tables(workspace_id, selection)
            preview = self._review_join_group(
                request,
                rules,
                selection,
                None,
                tables,
            )
        else:
            with self._materialize_join_group_inputs(
                workspace_id,
                selection,
                request,
            ) as (snapshots_by_id, paths):
                preview = self.group_evaluator.review(
                    request=request,
                    rules=rules,
                    selection=selection,
                    snapshots=snapshots_by_id,
                    paths=paths,
                )
            snapshots = tuple(
                snapshots_by_id[item.dataset_id] for item in selection.datasets
            )
        if not preview.can_save:
            findings = tuple(
                finding for review in preview.reviews for finding in review.findings
            )
            raise WorkspaceError(
                "The Recipe's complete combination cannot be applied: "
                + "; ".join(findings)
            )
        snapshot_evidence = tuple(
            ExactJoinInputSnapshotEvidence(
                dataset_id=item.dataset_id,
                logical_hash=item.logical_hash,
                parquet_sha256=item.parquet_sha256,
            )
            for item in snapshots
        )
        return replace(
            plan,
            join_reviews=tuple(
                SavedExactJoinReview(
                    rule_id=rule.rule_id,
                    rule_hash=content_hash(rule.to_dict()),
                    source_selection_hash=selection.content_hash,
                    input_snapshots=snapshot_evidence,
                    review=review,
                )
                for rule, review in zip(rules, preview.reviews, strict=True)
            ),
        )

    def preview_join(
        self,
        workspace_id: str,
        request: ExactJoinRequest,
    ) -> tuple[ExactJoinRule, ExactJoinReview]:
        """Review one exact join over every row in the frozen source snapshots."""

        selection, current = self._join_context(workspace_id)
        rule = self._join_rule(request)
        tables, _snapshots = self._load_join_tables(workspace_id, selection)
        review = self._review_join(rule, selection, current, tables)
        return rule, review

    def preview_join_group(
        self,
        workspace_id: str,
        request: ExactJoinGroupRequest,
    ) -> ExactJoinGroupPreview:
        """Review all enrichments over one loaded set of frozen snapshots."""

        selection, current = self._join_context(
            workspace_id,
            require_materialized_scale=self.group_evaluator is None,
        )
        rules = self._join_group_rules(request, selection, current)
        if self.group_evaluator is not None:
            with self._materialize_join_group_inputs(
                workspace_id,
                selection,
                request,
            ) as (snapshots, paths):
                return self.group_evaluator.review(
                    request=request,
                    rules=rules,
                    selection=selection,
                    snapshots=snapshots,
                    paths=paths,
                )
        tables, _snapshots = self._load_join_tables(workspace_id, selection)
        return self._review_join_group(request, rules, selection, current, tables)

    def save_join(
        self,
        workspace_id: str,
        request: ExactJoinRequest,
        *,
        dataset_uses: Mapping[str, SourceDatasetUse | str] | None = None,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> tuple[DerivedEntityPlan, ExactJoinRule, ExactJoinReview]:
        """Recheck and atomically append one accepted exact-join review."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection, current = self._join_context(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The source-preparation plan was modified by another request; reload it"
            )
        rule = self._join_rule(request)
        tables, snapshots = self._load_join_tables(workspace_id, selection)
        review = self._review_join(rule, selection, current, tables)
        if not review.can_save:
            raise WorkspaceError(
                "The exact join cannot be saved: " + "; ".join(review.findings)
            )

        rules = (*(current.rules if current else ()), rule)
        if dataset_uses is None:
            policies = reconcile_source_dataset_uses(
                current.dataset_uses if current else (),
                rules,
            )
        else:
            if set(dataset_uses) != {item.dataset_id for item in selection.datasets}:
                raise WorkspaceError(
                    "Source dataset-use policies must cover every frozen physical dataset"
                )
            policies = tuple(
                SourceDatasetUsePolicy(dataset_id, SourceDatasetUse(use))
                for dataset_id, use in dataset_uses.items()
            )
        saved_review = SavedExactJoinReview(
            rule_id=rule.rule_id,
            rule_hash=content_hash(rule.to_dict()),
            source_selection_hash=selection.content_hash,
            input_snapshots=tuple(
                ExactJoinInputSnapshotEvidence(
                    dataset_id=item.dataset_id,
                    logical_hash=item.logical_hash,
                    parquet_sha256=item.parquet_sha256,
                )
                for item in snapshots
            ),
            review=review,
        )
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=rules,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
            dataset_uses=policies,
            join_reviews=(*(current.join_reviews if current else ()), saved_review),
        )
        validate_source_dataset_uses(selection, plan)
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule, review

    def save_join_group(
        self,
        workspace_id: str,
        request: ExactJoinGroupRequest,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> tuple[DerivedEntityPlan, ExactJoinGroupPreview]:
        """Recheck and atomically append one complete grouped join decision."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection, current = self._join_context(
            workspace_id,
            require_materialized_scale=self.group_evaluator is None,
        )
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The source-preparation plan was modified by another request; reload it"
            )
        rules = self._join_group_rules(request, selection, current)
        if self.group_evaluator is None:
            tables, snapshots = self._load_join_tables(workspace_id, selection)
            preview = self._review_join_group(
                request,
                rules,
                selection,
                current,
                tables,
            )
        else:
            with self._materialize_join_group_inputs(
                workspace_id,
                selection,
                request,
            ) as (snapshots_by_id, paths):
                preview = self.group_evaluator.review(
                    request=request,
                    rules=rules,
                    selection=selection,
                    snapshots=snapshots_by_id,
                    paths=paths,
                )
            snapshots = tuple(
                snapshots_by_id[item.dataset_id] for item in selection.datasets
            )
        if not preview.can_save:
            findings = tuple(
                finding for review in preview.reviews for finding in review.findings
            )
            raise WorkspaceError(
                "The complete combination cannot be saved: " + "; ".join(findings)
            )

        all_rules = (*(current.rules if current else ()), *rules)
        uses = source_dataset_uses_for_authoring(selection, current)
        participating = {
            request.primary_dataset_id,
            *(addition.dataset_id for addition in request.additions),
        }
        for dataset_id in participating:
            if uses[dataset_id] is SourceDatasetUse.MAP_DIRECTLY:
                uses[dataset_id] = SourceDatasetUse.PREPARATION_ONLY
        snapshot_evidence = tuple(
            ExactJoinInputSnapshotEvidence(
                dataset_id=item.dataset_id,
                logical_hash=item.logical_hash,
                parquet_sha256=item.parquet_sha256,
            )
            for item in snapshots
        )
        saved_reviews = tuple(
            SavedExactJoinReview(
                rule_id=rule.rule_id,
                rule_hash=content_hash(rule.to_dict()),
                source_selection_hash=selection.content_hash,
                input_snapshots=snapshot_evidence,
                review=review,
            )
            for rule, review in zip(rules, preview.reviews, strict=True)
        )
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=all_rules,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
            dataset_uses=tuple(
                SourceDatasetUsePolicy(dataset_id, use)
                for dataset_id, use in uses.items()
            ),
            join_reviews=(
                *(current.join_reviews if current else ()),
                *saved_reviews,
            ),
        )
        validate_source_dataset_uses(selection, plan)
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, preview

    def _join_context(
        self,
        workspace_id: str,
        *,
        require_materialized_scale: bool = True,
    ) -> tuple[SourceSelection, DerivedEntityPlan | None]:
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before combining tables")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The source-preparation plan is stale; rebuild it from the frozen datasets"
            )
        if (
            require_materialized_scale
            and sum(item.row_count for item in selection.datasets)
            > MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT
        ):
            raise WorkspaceError(
                "This workspace selection contains more source rows than the "
                f"{MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT:,}-row exact-join "
                "review limit"
            )
        return selection, current

    @staticmethod
    def _join_rule(request: ExactJoinRequest) -> ExactJoinRule:
        return ExactJoinRule(
            rule_id=request.rule_id,
            output_dataset_name=request.output_dataset_name,
            left_dataset_id=request.left_dataset_id,
            right_dataset_id=request.right_dataset_id,
            keys=request.keys,
            output_columns=request.output_columns,
            projections=request.projections,
            kind=request.kind,
            require_all_right_rows=request.require_all_right_rows,
        )

    @staticmethod
    def _join_group_rules(
        request: ExactJoinGroupRequest,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
    ) -> tuple[ExactJoinRule, ...]:
        datasets = {item.dataset_id: item for item in selection.datasets}
        primary = datasets.get(request.primary_dataset_id)
        if primary is None:
            raise WorkspaceError("Choose a current physical primary table")
        if not 1 <= len(request.primary_key_column_keys) <= MAX_STRUCTURAL_KEYS:
            raise WorkspaceError("Choose one to five primary key fields")
        if len(request.key_value_types) != len(request.primary_key_column_keys):
            raise WorkspaceError("Choose a value type for every primary key field")
        if not request.additions or len(request.additions) >= MAX_STRUCTURAL_BRANCHES:
            raise WorkspaceError("Choose between one and nine added tables")

        participant_ids = [request.primary_dataset_id]
        for addition in request.additions:
            if addition.dataset_id not in datasets:
                raise WorkspaceError("Choose only current physical added tables")
            if addition.dataset_id in participant_ids:
                raise WorkspaceError("Choose every participating table only once")
            if len(addition.key_column_keys) != len(request.primary_key_column_keys):
                raise WorkspaceError(
                    "Confirm every shared key field for each added table"
                )
            participant_ids.append(addition.dataset_id)

        primary_columns = {item.stable_key: item for item in primary.columns}
        if any(key not in primary_columns for key in request.primary_key_column_keys):
            raise WorkspaceError("Choose primary key fields from the primary table")

        selected: list[tuple[ExactJoinGroupProjection, str, str]] = []
        selected_sources: set[tuple[str, str]] = set()
        output_names: set[str] = set()
        namespace = UUID(request.group_id)
        for projection in request.projections:
            source = datasets.get(projection.source_dataset_id)
            if source is None or source.dataset_id not in participant_ids:
                raise WorkspaceError(
                    "Choose output fields only from participating physical tables"
                )
            source_column = next(
                (
                    item
                    for item in source.columns
                    if item.stable_key == projection.source_column_key
                ),
                None,
            )
            if source_column is None:
                raise WorkspaceError("Choose current output fields")
            source_identity = (source.dataset_id, source_column.stable_key)
            if source_identity in selected_sources:
                raise WorkspaceError("Choose every output field only once")
            output_name = projection.output_name.strip()
            if not output_name:
                raise WorkspaceError("Give every selected output field a name")
            if output_name in output_names:
                raise WorkspaceError("Combined output field names must be unique")
            selected_sources.add(source_identity)
            output_names.add(output_name)
            output_key = "column:join-group:" + str(
                uuid5(
                    namespace,
                    f"field:{source.dataset_id}:{source_column.stable_key}",
                )
            )
            selected.append((projection, output_key, source_column.candidate_type))
        if not selected:
            raise WorkspaceError("Choose at least one output field")

        selected_keys = {
            (projection.source_dataset_id, projection.source_column_key): output_key
            for projection, output_key, _candidate_type in selected
        }
        helper_keys: list[tuple[str, str, str]] = []
        for index, source_key in enumerate(
            request.primary_key_column_keys,
            start=1,
        ):
            output_key = selected_keys.get(
                (request.primary_dataset_id, source_key),
                "column:join-group:" + str(uuid5(namespace, f"primary-key:{index}")),
            )
            helper_name = next(
                (
                    projection.output_name.strip()
                    for projection in request.projections
                    if projection.source_dataset_id == request.primary_dataset_id
                    and projection.source_column_key == source_key
                ),
                f"__impodo_group_key_{str(namespace)[:8]}_{index}",
            )
            helper_keys.append((source_key, output_key, helper_name))
        rules: list[ExactJoinRule] = []
        left_dataset_id = primary.dataset_id
        joined_ids = {primary.dataset_id}
        for index, addition in enumerate(request.additions, start=1):
            right = datasets[addition.dataset_id]
            right_columns = {item.stable_key for item in right.columns}
            if any(key not in right_columns for key in addition.key_column_keys):
                raise WorkspaceError(
                    "Choose added-table key fields from the matching added table"
                )
            final = index == len(request.additions)
            joined_ids.add(right.dataset_id)
            output_columns: list[StructuralOutputColumn] = []
            projections: list[StructuralProjection] = []
            output_keys: set[str] = set()
            for projection, output_key, candidate_type in selected:
                if projection.source_dataset_id not in joined_ids:
                    continue
                output_columns.append(
                    StructuralOutputColumn(
                        output_key,
                        projection.output_name.strip(),
                        candidate_type,
                    )
                )
                output_keys.add(output_key)
                if projection.source_dataset_id == right.dataset_id:
                    source_dataset_id = right.dataset_id
                    source_column_key = projection.source_column_key
                elif index == 1:
                    source_dataset_id = primary.dataset_id
                    source_column_key = projection.source_column_key
                else:
                    source_dataset_id = left_dataset_id
                    source_column_key = output_key
                projections.append(
                    StructuralProjection(
                        output_key,
                        source_dataset_id,
                        source_column_key,
                    )
                )
            if not final:
                for source_key, output_key, helper_name in helper_keys:
                    if output_key in output_keys:
                        continue
                    output_columns.append(
                        StructuralOutputColumn(
                            output_key,
                            helper_name,
                            primary_columns[source_key].candidate_type,
                        )
                    )
                    projections.append(
                        StructuralProjection(
                            output_key,
                            primary.dataset_id if index == 1 else left_dataset_id,
                            source_key if index == 1 else output_key,
                        )
                    )
            output_name = (
                request.output_dataset_name
                if final
                else _join_group_intermediate_name(
                    request.output_dataset_name,
                    namespace,
                    index,
                )
            )
            left_keys = (
                request.primary_key_column_keys
                if index == 1
                else tuple(item[1] for item in helper_keys)
            )
            if len(output_columns) > MAX_STRUCTURAL_COLUMNS:
                raise WorkspaceError(
                    "Choose fewer output fields so the internal shared key remains "
                    f"within the {MAX_STRUCTURAL_COLUMNS}-field limit"
                )
            rule = ExactJoinRule(
                rule_id=str(uuid5(namespace, f"step:{index}")),
                output_dataset_name=output_name,
                left_dataset_id=left_dataset_id,
                right_dataset_id=right.dataset_id,
                keys=tuple(
                    JoinKey(left_key, right_key, value_type)
                    for left_key, right_key, value_type in zip(
                        left_keys,
                        addition.key_column_keys,
                        request.key_value_types,
                        strict=True,
                    )
                ),
                output_columns=tuple(output_columns),
                projections=tuple(projections),
                kind=addition.kind,
                require_all_right_rows=addition.require_all_rows,
                require_unique_left=True,
                map_output=final,
                group_id=request.group_id,
                group_step=index,
            )
            rules.append(rule)
            left_dataset_id = structural_dataset_id(rule)

        current_structural = tuple(
            rule
            for rule in (current.rules if current else ())
            if isinstance(rule, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
        )
        try:
            structural_mapping_selection(selection, (*current_structural, *rules))
        except (StructuralError, ValueError) as error:
            raise WorkspaceError(str(error)) from error
        return tuple(rules)

    @staticmethod
    def _review_join_group(
        request: ExactJoinGroupRequest,
        rules: tuple[ExactJoinRule, ...],
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
        loaded_tables: Mapping[str, SourceTable],
    ) -> ExactJoinGroupPreview:
        specs = {item.dataset_id: item for item in selection.datasets}
        primary = specs[request.primary_dataset_id]
        reviews: list[ExactJoinReview] = []
        for index, (addition, rule) in enumerate(
            zip(request.additions, rules, strict=True),
            start=1,
        ):
            review_column = primary.columns[0]
            review_rule = ExactJoinRule(
                rule_id=str(uuid5(UUID(request.group_id), f"review:{index}")),
                output_dataset_name=_join_group_intermediate_name(
                    request.output_dataset_name,
                    UUID(request.group_id),
                    index + len(rules),
                ),
                left_dataset_id=request.primary_dataset_id,
                right_dataset_id=addition.dataset_id,
                keys=tuple(
                    JoinKey(left_key, right_key, value_type)
                    for left_key, right_key, value_type in zip(
                        request.primary_key_column_keys,
                        addition.key_column_keys,
                        request.key_value_types,
                        strict=True,
                    )
                ),
                output_columns=(
                    StructuralOutputColumn(
                        "column:review",
                        "Review field",
                        review_column.candidate_type,
                    ),
                ),
                projections=(
                    StructuralProjection(
                        "column:review",
                        primary.dataset_id,
                        review_column.stable_key,
                    ),
                ),
                kind=addition.kind,
                require_all_right_rows=addition.require_all_rows,
                require_unique_left=True,
                map_output=False,
            )
            reviews.append(
                review_exact_join(
                    review_rule,
                    specs=specs,
                    tables=loaded_tables,
                )
            )
        if not all(review.can_save for review in reviews):
            return ExactJoinGroupPreview(rules, tuple(reviews))

        current_rules = tuple(
            item
            for item in (current.rules if current else ())
            if isinstance(item, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
        )
        try:
            execution = execute_structural_rules(
                selection=selection,
                loaded_tables=loaded_tables,
                rules=(*current_rules, *rules),
            )
        except (StructuralError, ValueError) as error:
            raise WorkspaceError(str(error)) from error
        outputs = execution.by_dataset_id()
        complete: list[ExactJoinReview] = []
        for rule, review in zip(rules, reviews, strict=True):
            output = outputs[structural_dataset_id(rule)]
            lineage_by_primary_row = {
                primary_row.number: output.lineage[output_row.number]
                for primary_row, output_row in zip(
                    loaded_tables[request.primary_dataset_id].rows,
                    output.table.rows,
                    strict=True,
                )
            }
            complete.append(
                replace(
                    review,
                    actual_output_rows=output.reconciliation.output_rows,
                    lineage_links=output.reconciliation.lineage_links,
                    output_table_hash=output.table.content_hash,
                    lineage_hash=content_hash(
                        {
                            str(row_number): {
                                dataset_id: list(source_rows)
                                for dataset_id, source_rows in sources.items()
                            }
                            for row_number, sources in output.lineage.items()
                        }
                    ),
                    examples=tuple(
                        replace(
                            example,
                            physical_lineage=tuple(
                                ExactJoinLineageReference(dataset_id, source_rows)
                                for dataset_id, source_rows in lineage_by_primary_row[
                                    example.left_row_number
                                ].items()
                            ),
                        )
                        for example in review.examples
                    ),
                )
            )
        return ExactJoinGroupPreview(rules, tuple(complete))

    @contextmanager
    def _materialize_join_group_inputs(
        self,
        workspace_id: str,
        selection: SourceSelection,
        request: ExactJoinGroupRequest,
    ) -> Iterator[tuple[dict[str, SourceSnapshot], dict[str, Path]]]:
        """Keep each participating verified snapshot available for one review."""

        if self.artifacts is None:
            raise WorkspaceError(
                "Impodo cannot open the accepted source data for this review"
            )
        snapshots = self.sources.get_current_source_snapshots(workspace_id)
        by_dataset = {item.dataset_id: item for item in snapshots}
        physical = {item.dataset_id: item for item in selection.datasets}
        if len(by_dataset) != len(snapshots) or set(by_dataset) != set(physical):
            raise WorkspaceError(
                "Accept this Data version again before reviewing the exact join"
            )
        participants = {
            request.primary_dataset_id,
            *(item.dataset_id for item in request.additions),
        }
        stack = ExitStack()
        paths: dict[str, Path] = {}
        try:
            for dataset in selection.datasets:
                snapshot = by_dataset[dataset.dataset_id]
                validate_snapshot_for_dataset(selection, dataset, snapshot)
                if dataset.dataset_id not in participants:
                    continue
                paths[dataset.dataset_id] = stack.enter_context(
                    self.artifacts.materialize_source_snapshot(
                        selection.data_version_id,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )
                )
        except (ArtifactStoreError, OSError, SourceLoadError, ValueError) as error:
            stack.close()
            raise WorkspaceError(
                "Impodo could not verify the accepted source data for this review"
            ) from error
        try:
            yield by_dataset, paths
        finally:
            stack.close()

    def _load_join_tables(
        self,
        workspace_id: str,
        selection: SourceSelection,
    ) -> tuple[dict[str, SourceTable], tuple[SourceSnapshot, ...]]:
        if self.artifacts is None:
            raise WorkspaceError(
                "Impodo cannot open the accepted source data for this review"
            )
        snapshots = self.sources.get_current_source_snapshots(workspace_id)
        by_dataset = {item.dataset_id: item for item in snapshots}
        physical_ids = {item.dataset_id for item in selection.datasets}
        if len(by_dataset) != len(snapshots) or set(by_dataset) != physical_ids:
            raise WorkspaceError(
                "Accept this Data version again before reviewing the exact join"
            )
        loaded: dict[str, SourceTable] = {}
        try:
            for dataset in selection.datasets:
                snapshot = by_dataset[dataset.dataset_id]
                validate_snapshot_for_dataset(selection, dataset, snapshot)
                with self.artifacts.materialize_source_snapshot(
                    selection.data_version_id,
                    snapshot.parquet_storage_key,
                    expected_sha256=snapshot.parquet_sha256,
                ) as path:
                    loaded[dataset.dataset_id] = load_source_snapshot_table(
                        path,
                        snapshot,
                    )
        except (ArtifactStoreError, OSError, SourceLoadError, ValueError) as error:
            raise WorkspaceError(
                "Impodo could not verify the accepted source data for this review"
            ) from error
        return loaded, tuple(by_dataset[item.dataset_id] for item in selection.datasets)

    @staticmethod
    def _review_join(
        rule: ExactJoinRule,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
        loaded_tables: Mapping[str, SourceTable],
    ) -> ExactJoinReview:
        current_rules = tuple(
            item
            for item in (current.rules if current else ())
            if isinstance(item, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
        )
        try:
            structural_mapping_selection(selection, (*current_rules, rule))
            existing = execute_structural_rules(
                selection=selection,
                loaded_tables=loaded_tables,
                rules=current_rules,
            )
            available_selection = structural_mapping_selection(
                selection,
                current_rules,
            )
            specs = {item.dataset_id: item for item in available_selection.datasets}
            tables = dict(loaded_tables)
            tables.update(
                {item.dataset.dataset_id: item.table for item in existing.outputs}
            )
            review = review_exact_join(rule, specs=specs, tables=tables)
            if not review.can_save:
                return review
            execution = execute_structural_rules(
                selection=selection,
                loaded_tables=loaded_tables,
                rules=(*current_rules, rule),
            )
            output = execution.by_dataset_id()[structural_dataset_id(rule)]
        except (StructuralError, ValueError) as error:
            raise WorkspaceError(str(error)) from error
        lineage_by_left_row = {
            left_row.number: output.lineage[output_row.number]
            for left_row, output_row in zip(
                tables[rule.left_dataset_id].rows,
                output.table.rows,
                strict=True,
            )
        }
        return replace(
            review,
            actual_output_rows=output.reconciliation.output_rows,
            lineage_links=output.reconciliation.lineage_links,
            output_table_hash=output.table.content_hash,
            lineage_hash=content_hash(
                {
                    str(row_number): {
                        dataset_id: list(source_rows)
                        for dataset_id, source_rows in sources.items()
                    }
                    for row_number, sources in output.lineage.items()
                }
            ),
            examples=tuple(
                replace(
                    example,
                    physical_lineage=tuple(
                        ExactJoinLineageReference(dataset_id, source_rows)
                        for dataset_id, source_rows in lineage_by_left_row[
                            example.left_row_number
                        ].items()
                    ),
                )
                for example in review.examples
            ),
        )

    def save_dataset_uses(
        self,
        workspace_id: str,
        *,
        dataset_uses: Mapping[str, SourceDatasetUse | str],
        expected_parent_version: int,
        actor: Actor,
    ) -> DerivedEntityPlan:
        """Save how every physical dataset participates in mapping and preparation."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before choosing their use")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        if current is None:
            raise WorkspaceError(
                "Add a source-preparation rule before choosing dataset use"
            )
        if expected_parent_version != current.version:
            raise WorkspaceError(
                "The source-preparation plan was modified by another request; reload it"
            )
        if current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The source-preparation plan is stale; rebuild it from the frozen datasets"
            )
        if set(dataset_uses) != {item.dataset_id for item in selection.datasets}:
            raise WorkspaceError(
                "Source dataset-use policies must cover every frozen physical dataset"
            )
        plan = DerivedEntityPlan(
            plan_id=current.plan_id,
            version=current.version + 1,
            workspace_id=current.workspace_id,
            source_selection_hash=current.source_selection_hash,
            rules=current.rules,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
            dataset_uses=tuple(
                SourceDatasetUsePolicy(
                    dataset_id=dataset_id,
                    use=SourceDatasetUse(use),
                )
                for dataset_id, use in dataset_uses.items()
            ),
            join_reviews=current.join_reviews,
        )
        validate_source_dataset_uses(selection, plan)
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=current.version,
            actor=actor,
        )
        return plan

    def save_rule(
        self,
        workspace_id: str,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_column_key: str,
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        parent_separator: str | None,
        blank_policy: str,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> tuple[DerivedEntityPlan, DerivedEntityRule]:
        """Append one reviewed lookup-extraction rule to the current plan."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The derived-entity plan was modified by another request; reload it"
            )
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The derived-entity plan is stale; rebuild it from the frozen datasets"
            )

        rule = self._lookup_rule(
            selection,
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_column_key=source_column_key,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            parent_separator=parent_separator,
            blank_policy=blank_policy,
        )
        self._validate_lookup_rule_availability(rule, selection, current)

        now = datetime.now(UTC)
        rules = (*(current.rules if current else ()), rule)
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=rules,
            updated_at=now,
            updated_by=actor.identity.display_name,
            dataset_uses=reconcile_source_dataset_uses(
                current.dataset_uses if current else (),
                rules,
            ),
            join_reviews=current.join_reviews if current else (),
        )
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule

    def preview_lookup(
        self,
        workspace_id: str,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_column_key: str,
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        parent_separator: str | None,
        blank_policy: str,
    ) -> tuple[DerivedEntityRule, DerivedEntityImpactReview]:
        """Review the complete related-table result without saving the rule."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        rule = self._lookup_rule(
            selection,
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_column_key=source_column_key,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            parent_separator=parent_separator,
            blank_policy=blank_policy,
        )
        self._validate_lookup_rule_availability(rule, selection, current)
        dataset = _source_dataset(
            selection,
            rule.source_dataset_id,
            rule.source_column_key,
        )
        if dataset.row_count > LOOKUP_DERIVED_BROWSER_EVALUATION_ROW_LIMIT:
            raise WorkspaceError(
                f"{dataset.name} contains {dataset.row_count:,} rows. "
                "Impodo can currently review a related table from up to "
                f"{LOOKUP_DERIVED_BROWSER_EVALUATION_ROW_LIMIT:,} source rows."
            )
        if self.artifacts is None:
            raise WorkspaceError(
                "Impodo cannot open the accepted source data for this review"
            )
        snapshot = next(
            (
                item
                for item in self.sources.get_current_source_snapshots(workspace_id)
                if item.dataset_id == dataset.dataset_id
            ),
            None,
        )
        if snapshot is None:
            raise WorkspaceError(
                "Accept this Data version again before reviewing the related table"
            )
        column = next(
            item
            for item in dataset.columns
            if item.stable_key == rule.source_column_key
        )
        try:
            validate_snapshot_for_dataset(selection, dataset, snapshot)
            with self.artifacts.materialize_source_snapshot(
                selection.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                table = load_source_snapshot_table(path, snapshot)
                review = review_derived_entities(
                    rule,
                    selection,
                    (
                        (row.number, row.values.get(column.source_name))
                        for row in table.rows
                    ),
                )
        except (ArtifactStoreError, OSError, SourceLoadError, ValueError) as error:
            raise WorkspaceError(
                "Impodo could not verify the accepted source data for this review"
            ) from error
        return (
            rule,
            replace(
                review,
                supported_record_limit=LOOKUP_DERIVED_OUTPUT_ROW_LIMIT,
            ),
        )

    @staticmethod
    def _lookup_rule(
        selection: SourceSelection,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_column_key: str,
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        parent_separator: str | None,
        blank_policy: str,
    ) -> DerivedEntityRule:
        source_dataset = _source_dataset(
            selection,
            source_dataset_id,
            source_column_key,
        )
        return DerivedEntityRule(
            rule_id=str(uuid4()),
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset.dataset_id,
            source_column_key=source_column_key,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            parent_separator=parent_separator,
            blank_policy=blank_policy,
        )

    @staticmethod
    def _validate_lookup_rule_availability(
        rule: LookupRule,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
    ) -> None:
        existing_names = {item.name for item in selection.datasets}
        existing_names.update(_rule_dataset_names(current.rules if current else ()))
        if rule.output_dataset_name in existing_names:
            raise WorkspaceError(
                "Derived dataset names must be unique in the workspace"
            )

    def preview_hierarchy(
        self,
        workspace_id: str,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_level_column_keys: tuple[str, ...],
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        missing_parent_mode: str,
        missing_parent_value: str | None,
        missing_leaf: str,
        all_blank_mode: str,
        all_blank_value: str | None,
    ) -> tuple[HierarchicalLookupRule, DerivedEntityPreview]:
        """Validate and preview an ordered multi-column hierarchy."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        rule = self._hierarchy_rule(
            selection,
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_level_column_keys=source_level_column_keys,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            missing_parent_mode=missing_parent_mode,
            missing_parent_value=missing_parent_value,
            missing_leaf=missing_leaf,
            all_blank_mode=all_blank_mode,
            all_blank_value=all_blank_value,
        )
        self._validate_lookup_rule_availability(rule, selection, current)
        return (
            rule,
            preview_derived_entities(
                rule,
                selection,
                self.sources.get_source_catalogs(workspace_id),
            ),
        )

    def save_hierarchy(
        self,
        workspace_id: str,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_level_column_keys: tuple[str, ...],
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        missing_parent_mode: str,
        missing_parent_value: str | None,
        missing_leaf: str,
        all_blank_mode: str,
        all_blank_value: str | None,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> tuple[DerivedEntityPlan, HierarchicalLookupRule]:
        """Append one reviewed multi-column hierarchy rule."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The derived-entity plan was modified by another request; reload it"
            )
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The derived-entity plan is stale; rebuild it from the frozen datasets"
            )
        rule = self._hierarchy_rule(
            selection,
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_level_column_keys=source_level_column_keys,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            missing_parent_mode=missing_parent_mode,
            missing_parent_value=missing_parent_value,
            missing_leaf=missing_leaf,
            all_blank_mode=all_blank_mode,
            all_blank_value=all_blank_value,
        )
        self._validate_lookup_rule_availability(rule, selection, current)
        rules = (*(current.rules if current else ()), rule)
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=rules,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
            dataset_uses=reconcile_source_dataset_uses(
                current.dataset_uses if current else (),
                rules,
            ),
            join_reviews=current.join_reviews if current else (),
        )
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule

    @staticmethod
    def _hierarchy_rule(
        selection: SourceSelection,
        *,
        output_dataset_name: str,
        source_dataset_id: str,
        source_level_column_keys: tuple[str, ...],
        target_model: str,
        target_name_field: str,
        external_id_namespace: str,
        missing_parent_mode: str,
        missing_parent_value: str | None,
        missing_leaf: str,
        all_blank_mode: str,
        all_blank_value: str | None,
    ) -> HierarchicalLookupRule:
        rule = HierarchicalLookupRule(
            rule_id=str(uuid4()),
            output_dataset_name=output_dataset_name,
            source_dataset_id=source_dataset_id,
            source_level_column_keys=source_level_column_keys,
            target_model=target_model,
            target_name_field=target_name_field,
            external_id_namespace=external_id_namespace,
            missing_parent=HierarchyValuePolicy(
                mode=missing_parent_mode,
                value=missing_parent_value,
            ),
            missing_leaf=missing_leaf,
            all_blank=HierarchyValuePolicy(
                mode=all_blank_mode,
                value=all_blank_value,
            ),
        )
        _hierarchy_source_dataset(selection, rule)
        return rule

    def preview_related_split(
        self,
        workspace_id: str,
        *,
        source_dataset_id: str,
        parent_dataset_name: str,
        child_dataset_name: str,
        parent_key_column_key: str,
        child_key_column_key: str,
        scope_column_key: str | None,
        blank_policy: str,
    ) -> tuple[RelatedDatasetRule, RelatedDatasetPreview]:
        """Validate and preview a split without changing the workspace plan."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before preparing related data")
        rule = self._related_rule(
            selection,
            source_dataset_id=source_dataset_id,
            parent_dataset_name=parent_dataset_name,
            child_dataset_name=child_dataset_name,
            parent_key_column_key=parent_key_column_key,
            child_key_column_key=child_key_column_key,
            scope_column_key=scope_column_key,
            blank_policy=blank_policy,
        )
        self._validate_related_rule_availability(
            rule,
            selection,
            self.derived_entities.get_derived_entity_plan(workspace_id),
        )
        return (
            rule,
            preview_related_datasets(
                rule,
                selection,
                self.sources.get_source_catalogs(workspace_id),
            ),
        )

    def save_related_split(
        self,
        workspace_id: str,
        *,
        source_dataset_id: str,
        parent_dataset_name: str,
        child_dataset_name: str,
        parent_key_column_key: str,
        child_key_column_key: str,
        scope_column_key: str | None,
        blank_policy: str,
        expected_parent_version: int | None,
        actor: Actor,
        source_dataset_use: SourceDatasetUse | str = (
            SourceDatasetUse.PREPARATION_ONLY
        ),
    ) -> tuple[DerivedEntityPlan, RelatedDatasetRule]:
        """Persist one reviewed parent/child split as an immutable plan revision."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before preparing related data")
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The source-preparation plan was modified by another request; reload it"
            )
        if current and current.source_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The source-preparation plan is stale; rebuild it from the frozen datasets"
            )

        rule = self._related_rule(
            selection,
            source_dataset_id=source_dataset_id,
            parent_dataset_name=parent_dataset_name,
            child_dataset_name=child_dataset_name,
            parent_key_column_key=parent_key_column_key,
            child_key_column_key=child_key_column_key,
            scope_column_key=scope_column_key,
            blank_policy=blank_policy,
        )
        self._validate_related_rule_availability(rule, selection, current)

        try:
            selected_use = SourceDatasetUse(source_dataset_use)
        except ValueError as error:
            raise WorkspaceError(
                "Choose whether the original table should remain in Match data"
            ) from error
        if not selected_use.is_preparation_input:
            raise WorkspaceError(
                "A separated-table input must be Preparation only or Map and prepare"
            )

        rules = (*(current.rules if current else ()), rule)
        uses = source_dataset_uses_for_authoring(selection, current)
        uses[source_dataset_id] = selected_use
        policies = tuple(
            SourceDatasetUsePolicy(dataset.dataset_id, uses[dataset.dataset_id])
            for dataset in selection.datasets
        )
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=rules,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
            dataset_uses=policies,
            join_reviews=current.join_reviews if current else (),
        )
        validate_source_dataset_uses(selection, plan)
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=actual_parent,
            actor=actor,
        )
        return plan, rule

    @staticmethod
    def _related_rule(
        selection: SourceSelection,
        *,
        source_dataset_id: str,
        parent_dataset_name: str,
        child_dataset_name: str,
        parent_key_column_key: str,
        child_key_column_key: str,
        scope_column_key: str | None,
        blank_policy: str,
    ) -> RelatedDatasetRule:
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=source_dataset_id,
            parent_dataset_name=parent_dataset_name,
            child_dataset_name=child_dataset_name,
            parent_key_column_key=parent_key_column_key,
            child_key_column_key=child_key_column_key,
            scope_column_key=scope_column_key,
            blank_policy=blank_policy,
        )
        _related_source_dataset(selection, rule)
        return rule

    @staticmethod
    def _validate_related_rule_availability(
        rule: RelatedDatasetRule,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
    ) -> None:
        existing_names = {item.name for item in selection.datasets}
        existing_names.update(_rule_dataset_names(current.rules if current else ()))
        if {rule.parent_dataset_name, rule.child_dataset_name}.intersection(
            existing_names
        ):
            raise WorkspaceError(
                "Related dataset names must be unique in the workspace"
            )
        if any(
            isinstance(item, RelatedDatasetRule)
            and item.source_dataset_id == rule.source_dataset_id
            for item in (current.rules if current else ())
        ):
            raise WorkspaceError(
                "This source dataset already has a parent/child split; remove it before creating another"
            )

    def delete_rule(
        self,
        workspace_id: str,
        rule_id: str,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> DerivedEntityPlan:
        """Append a new plan revision without the requested existing rule."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            workspace_id=workspace_id,
        )
        current = self.derived_entities.get_derived_entity_plan(workspace_id)
        if current is None:
            raise WorkspaceError("No derived-entity plan exists")
        if expected_parent_version != current.version:
            raise WorkspaceError(
                "The derived-entity plan was modified by another request; reload it"
            )
        requested = next(
            (item for item in current.rules if item.rule_id == rule_id),
            None,
        )
        if requested is None:
            raise WorkspaceError("Derived-entity rule not found")
        remove_ids = {rule_id}
        if (
            isinstance(requested, ExactJoinRule)
            and requested.map_output
            and requested.group_id is not None
        ):
            remove_ids.update(
                item.rule_id
                for item in current.rules
                if isinstance(item, ExactJoinRule)
                and item.group_id == requested.group_id
            )
        elif isinstance(requested, ExactJoinRule) and requested.map_output:
            structural_by_output = {
                structural_dataset_id(item): item
                for item in current.rules
                if isinstance(item, ExactJoinRule) and not item.map_output
            }
            pending_inputs = [requested.left_dataset_id, requested.right_dataset_id]
            while pending_inputs:
                input_id = pending_inputs.pop()
                hidden = structural_by_output.get(input_id)
                if hidden is None or hidden.rule_id in remove_ids:
                    continue
                consumers = {
                    item.rule_id
                    for item in current.rules
                    if input_id in preparation_input_dataset_ids((item,))
                }
                if consumers.issubset(remove_ids):
                    remove_ids.add(hidden.rule_id)
                    pending_inputs.extend(
                        (hidden.left_dataset_id, hidden.right_dataset_id)
                    )
        remaining = tuple(
            item for item in current.rules if item.rule_id not in remove_ids
        )
        for removed in (item for item in current.rules if item.rule_id in remove_ids):
            if not isinstance(
                removed,
                (ExactJoinRule, UnionAllRule, GroupAggregateRule),
            ):
                continue
            removed_output_id = structural_dataset_id(removed)
            if any(
                removed_output_id in preparation_input_dataset_ids((rule,))
                for rule in remaining
            ):
                raise WorkspaceError(
                    "Remove the later source-preparation step that uses this "
                    "combined table first"
                )
        plan = DerivedEntityPlan(
            plan_id=current.plan_id,
            version=current.version + 1,
            workspace_id=current.workspace_id,
            source_selection_hash=current.source_selection_hash,
            rules=remaining,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
            dataset_uses=reconcile_source_dataset_uses(
                current.dataset_uses,
                remaining,
            ),
            join_reviews=tuple(
                review
                for review in current.join_reviews
                if review.rule_id not in remove_ids
            ),
        )
        self.derived_entities.save_derived_entity_plan(
            workspace_id,
            plan,
            expected_parent_version=current.version,
            actor=actor,
        )
        return plan

    def preview(
        self,
        workspace_id: str,
        rule: LookupRule,
    ) -> DerivedEntityPreview:
        """Build a bounded lookup-extraction preview from current source catalogs."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before deriving entities")
        return preview_derived_entities(
            rule,
            selection,
            self.sources.get_source_catalogs(workspace_id),
        )

    def preview_rules(
        self,
        rules: tuple[LookupRule | RelatedDatasetRule, ...],
        *,
        selection: SourceSelection | None,
        catalogs: tuple[SourceFileCatalogView, ...],
    ) -> tuple[DerivedRulePreview, ...]:
        """Evaluate saved rule cards from the page's already verified source inputs."""

        results = []
        for rule in rules:
            try:
                lookup = isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule))
                if selection is None:
                    raise WorkspaceError(
                        "Freeze source datasets before deriving entities"
                        if lookup
                        else "Freeze source datasets before separating related data"
                    )
                preview = (
                    preview_derived_entities(rule, selection, catalogs)
                    if lookup
                    else preview_related_datasets(rule, selection, catalogs)
                )
                results.append(DerivedRulePreview(rule, preview))
            except WorkspaceError as error:
                results.append(DerivedRulePreview(rule, None, str(error)))
        return tuple(results)

    def preview_related(
        self,
        workspace_id: str,
        rule: RelatedDatasetRule,
    ) -> RelatedDatasetPreview:
        """Build a bounded parent/child preview from current source catalogs."""

        selection = self.sources.get_source_selection(workspace_id)
        if selection is None:
            raise WorkspaceError("Freeze source datasets before preparing related data")
        return preview_related_datasets(
            rule,
            selection,
            self.sources.get_source_catalogs(workspace_id),
        )
