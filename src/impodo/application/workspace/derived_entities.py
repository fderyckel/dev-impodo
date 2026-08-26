"""Workspace use cases and consumer-owned ports for derived datasets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityPreview,
    DerivedEntityRule,
    RelatedDatasetPreview,
    RelatedDatasetRule,
    SourceFileCatalogView,
    _related_source_dataset,
    _rule_dataset_names,
    _source_dataset,
    preview_derived_entities,
    preview_related_datasets,
)


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


class DerivedEntityWorkspaceService:
    """Author and preview deterministic derived-entity rules.

    Saved rules are bound to the current frozen physical selection and use an
    optimistic plan version. Previews are bounded authoring evidence only; the
    staging evaluator later repeats accepted rules across every frozen row.
    """

    def __init__(
        self,
        sources: DerivedSourceRepository,
        derived_entities: DerivedEntityRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.sources = sources
        self.derived_entities = derived_entities
        self.authorization = authorization

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

        now = datetime.now(timezone.utc)
        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=(*(current.rules if current else ()), rule),
            updated_at=now,
            updated_by=actor.identity.display_name,
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
    ) -> tuple[DerivedEntityRule, DerivedEntityPreview]:
        """Validate and preview related-record extraction without saving it."""

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
        return (
            rule,
            preview_derived_entities(
                rule,
                selection,
                self.sources.get_source_catalogs(workspace_id),
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
        rule: DerivedEntityRule,
        selection: SourceSelection,
        current: DerivedEntityPlan | None,
    ) -> None:
        existing_names = {item.name for item in selection.datasets}
        existing_names.update(_rule_dataset_names(current.rules if current else ()))
        if rule.output_dataset_name in existing_names:
            raise WorkspaceError(
                "Derived dataset names must be unique in the workspace"
            )

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

        plan = DerivedEntityPlan(
            plan_id=current.plan_id if current else str(uuid4()),
            version=(current.version + 1 if current else 1),
            workspace_id=workspace_id,
            source_selection_hash=selection.content_hash,
            rules=(*(current.rules if current else ()), rule),
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
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
        plan = DerivedEntityPlan(
            plan_id=current.plan_id,
            version=current.version + 1,
            workspace_id=current.workspace_id,
            source_selection_hash=current.source_selection_hash,
            rules=remaining,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
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
        rule: DerivedEntityRule,
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



