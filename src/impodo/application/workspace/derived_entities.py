"""Workspace use cases and consumer-owned ports for derived datasets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

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
    ) -> None:
        self.sources = sources
        self.derived_entities = derived_entities
        self.authorization = authorization
        self.artifacts = artifacts

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
            if set(dataset_uses) != {
                item.dataset_id for item in selection.datasets
            }:
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

    def _join_context(
        self,
        workspace_id: str,
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
            sum(item.row_count for item in selection.datasets)
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
        if set(dataset_uses) != {
            item.dataset_id for item in selection.datasets
        }:
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
        remaining = tuple(item for item in current.rules if item.rule_id != rule_id)
        if len(remaining) == len(current.rules):
            raise WorkspaceError("Derived-entity rule not found")
        removed = next(item for item in current.rules if item.rule_id == rule_id)
        if isinstance(removed, (ExactJoinRule, UnionAllRule, GroupAggregateRule)):
            removed_output_id = structural_dataset_id(removed)
            dependent = next(
                (
                    rule
                    for rule in remaining
                    if removed_output_id
                    in preparation_input_dataset_ids((rule,))
                ),
                None,
            )
            if dependent is not None:
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
                if review.rule_id != rule_id
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
        self, rules: tuple[LookupRule | RelatedDatasetRule, ...], *,
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
                        if lookup else "Freeze source datasets before separating related data"
                    )
                preview = (
                    preview_derived_entities(rule, selection, catalogs)
                    if lookup else preview_related_datasets(rule, selection, catalogs)
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



