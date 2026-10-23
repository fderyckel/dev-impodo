"""Present related Odoo data as business scope, not a model checklist."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    propose_relationship_action_defaults,
    recommended_relationship_capture_action,
    review_relationship_scope,
)
from impodo.domain.odoo_source_scope import (
    RelatedDataHandling,
    RelatedDataSuggestion,
    related_model_can_be_selected,
)


@dataclass(frozen=True, slots=True)
class RelatedDataItemView:
    """Readable description of one relationship and its technical evidence."""

    source_label: str
    field_label: str
    relation_label: str
    technical_name: str
    required: bool
    reviewed: bool


@dataclass(frozen=True, slots=True)
class RelatedDataGroupView:
    """One decision-oriented group in the source-scope proposal."""

    handling: RelatedDataHandling
    title: str
    description: str
    attention: bool
    models: tuple["RelatedDataModelView", ...]


@dataclass(frozen=True, slots=True)
class RelatedDataModelView:
    """One related model with its current source-scope decision."""

    name: str
    label: str
    selected: bool
    action: OdooRelationshipCaptureAction
    recommended_action: OdooRelationshipCaptureAction
    action_title: str
    action_description: str
    recommended: bool
    available: bool
    can_select: bool
    required: bool
    pending: bool
    unavailable_pending: bool
    default_conflict: bool
    preserve_allowed: bool
    saved_choice: bool
    items: tuple[RelatedDataItemView, ...]


@dataclass(frozen=True, slots=True)
class RelatedDataExceptionItemView:
    """One business link that explains why a deliberate choice is needed."""

    source_label: str
    field_label: str
    required: bool
    recommended_action_title: str


@dataclass(frozen=True, slots=True)
class RelatedDataExceptionModelView:
    """A compact model-wide exception with all of its conflicting links."""

    name: str
    label: str
    preserve_allowed: bool
    items: tuple[RelatedDataExceptionItemView, ...]


@dataclass(frozen=True, slots=True)
class RelatedDataScopeView:
    """Complete source-page projection for related-data decisions."""

    groups: tuple[RelatedDataGroupView, ...]
    saved_choice_models: tuple[RelatedDataModelView, ...]
    selectable_model_names: tuple[str, ...]
    all_choices_url: str
    complete: bool
    pending_count: int
    unavailable_count: int
    safe_default_count: int
    decision_required_count: int
    conflicting_model_names: tuple[str, ...]
    conflicting_model_labels: tuple[str, ...]
    exception_models: tuple[RelatedDataExceptionModelView, ...]
    capture_default_count: int
    preserve_default_count: int
    match_default_count: int
    exclude_default_count: int


_GROUP_COPY = {
    RelatedDataHandling.TRANSFER_RELATED: (
        "Related records included in the migration",
        "Impodo recommends transferring the reached business records with "
        "their selected values, then applying the same bounded rules to "
        "their own links.",
    ),
    RelatedDataHandling.INCLUDE_SUPPORTING: (
        "Needed to keep the records meaningful",
        "Impodo recommends keeping these populated links. It will reuse "
        "destination matches and prepare only identity and required values "
        "for missing records "
        "without expanding their whole relationship graph.",
    ),
    RelatedDataHandling.OPTIONAL_BUSINESS_DATA: (
        "Related business data to confirm",
        "Impodo recommends keeping each populated link without assuming that "
        "the whole related record type belongs in this migration. Choose to "
        "transfer it as migration data only when that wider data is in scope.",
    ),
    RelatedDataHandling.REUSE_DESTINATION: (
        "Reuse destination setup",
        "Keep these out of ordinary source creation. Before transfer, Impodo "
        "must match them to existing destination settings.",
    ),
    RelatedDataHandling.ODOO_MANAGED: (
        "Created by Odoo",
        "Impodo should preserve the inputs and verify the result instead of "
        "creating these generated records directly.",
    ),
    RelatedDataHandling.SEPARATE_PROCESS: (
        "Move as a separate business process",
        "These records expand the migration beyond the selected root data "
        "and need their own reviewed scope.",
    ),
    RelatedDataHandling.EXCLUDE_HISTORY: (
        "Not part of the business-data move",
        "Activity and message history stays out unless a separate, supported "
        "history migration is explicitly approved.",
    ),
    RelatedDataHandling.NEEDS_DECISION: (
        "Safe default for an unclassified link",
        "Impodo cannot infer the full business scope of these links. It "
        "recommends keeping each populated value without expanding the whole "
        "related record type. Choose another outcome under Advanced when the "
        "migration requires it.",
    ),
}

_GROUP_ORDER = tuple(_GROUP_COPY)

_ATTENTION_HANDLINGS = frozenset(
    {
        RelatedDataHandling.INCLUDE_SUPPORTING,
        RelatedDataHandling.TRANSFER_RELATED,
        RelatedDataHandling.OPTIONAL_BUSINESS_DATA,
        RelatedDataHandling.REUSE_DESTINATION,
        RelatedDataHandling.SEPARATE_PROCESS,
        RelatedDataHandling.NEEDS_DECISION,
    }
)


_ACTION_COPY = {
    OdooRelationshipCaptureAction.PRESERVE_LINKED: (
        "Keep linked value",
        "Reuse a matching destination record when one exists. Otherwise, "
        "prepare only its identity and required values, without following the "
        "rest of its relationships. If those values cannot be proven safe, "
        "Impodo asks for a decision before any load.",
    ),
    OdooRelationshipCaptureAction.CAPTURE_LINKED: (
        "Transfer related records as migration data",
        "Include the related records as migration data and review the "
        "relationships that they need in turn.",
    ),
    OdooRelationshipCaptureAction.MATCH_EXISTING: (
        "Use existing destination records only",
        "Match these links to records that already exist in the destination. "
        "A missing match stops the transfer instead of creating a record.",
    ),
    OdooRelationshipCaptureAction.DO_NOT_CAPTURE: (
        "Do not include",
        "Leave this relationship out of the migration scope.",
    ),
}


def _exception_recommendation_title(
    suggestion: RelatedDataSuggestion,
    *,
    root_models: frozenset[str],
) -> str:
    """Describe the edge default without pretending unsafe collections are safe."""

    if (
        suggestion.required
        and suggestion.relationship_type in {"many2many", "one2many"}
        and suggestion.recommendation_profile_id is None
    ):
        return "No safe default - choose deliberately"
    action = recommended_relationship_capture_action(
        suggestion,
        selected_root=suggestion.relation_model in root_models,
    )
    return _ACTION_COPY[action][0]


def build_related_data_scope_view(
    workspace_id: str,
    suggestions: tuple[RelatedDataSuggestion, ...],
    *,
    model_labels: Mapping[str, str] | None = None,
    selected_models: frozenset[str] = frozenset(),
    available_models: frozenset[str] | None = None,
    relationship_scope: OdooRelationshipScope | None = None,
) -> RelatedDataScopeView:
    """Group domain decisions and expose safe inline model choices."""

    labels = model_labels or {}
    available = available_models if available_models is not None else frozenset(labels)
    root_models = (
        frozenset(relationship_scope.root_models)
        if relationship_scope is not None
        else selected_models
    )
    review = review_relationship_scope(
        suggestions,
        relationship_scope,
        available_models=available,
    )
    pending_identities = {
        (item.source_model, item.field_name) for item in review.pending
    }
    unavailable_identities = {
        (item.source_model, item.field_name) for item in review.unavailable
    }
    selection_handling_by_model: dict[str, RelatedDataHandling] = {}
    for handling in _GROUP_ORDER:
        if not related_model_can_be_selected(handling):
            continue
        for item in suggestions:
            if item.handling is handling:
                selection_handling_by_model.setdefault(
                    item.relation_model,
                    handling,
                )
    effective_defaults = propose_relationship_action_defaults(
        suggestions,
        root_models=root_models,
        current_scope=relationship_scope,
    )
    recommended_defaults = propose_relationship_action_defaults(
        suggestions,
        root_models=root_models,
    )
    effective_action_by_model = effective_defaults.actions
    recommended_action_by_model = recommended_defaults.actions
    saved_choice_names = frozenset(
        relationship_scope.explicit_actions
        if relationship_scope is not None
        else ()
    )
    all_effective_conflicts = frozenset(
        effective_defaults.conflicting_models
    )
    conflicting_model_names = tuple(
        sorted(name for name in all_effective_conflicts if name in available)
    )
    conflicting_models = frozenset(conflicting_model_names)
    preserve_allowed_by_model = {
        model: all(
            item.handling is not RelatedDataHandling.REUSE_DESTINATION
            for item in suggestions
            if item.relation_model == model
        )
        for model in {item.relation_model for item in suggestions}
    }
    groups = tuple(
        RelatedDataGroupView(
            handling=handling,
            title=_GROUP_COPY[handling][0],
            description=_GROUP_COPY[handling][1],
            attention=handling in _ATTENTION_HANDLINGS,
            models=tuple(
                RelatedDataModelView(
                    name=relation_model,
                    label=labels.get(
                        relation_model,
                        _fallback_model_label(relation_model),
                    ),
                    selected=relation_model in selected_models,
                    action=(
                        action := effective_action_by_model.get(
                            relation_model,
                            recommended_relationship_capture_action(
                                related_items[0],
                                selected_root=relation_model in root_models,
                            ),
                        )
                    ),
                    recommended_action=recommended_action_by_model.get(
                        relation_model,
                        recommended_relationship_capture_action(
                            related_items[0],
                            selected_root=relation_model in root_models,
                        ),
                    ),
                    action_title=_ACTION_COPY[action][0],
                    action_description=_ACTION_COPY[action][1],
                    recommended=(
                        handling in {
                            RelatedDataHandling.INCLUDE_SUPPORTING,
                            RelatedDataHandling.TRANSFER_RELATED,
                            RelatedDataHandling.REUSE_DESTINATION,
                        }
                        and any(
                            item.recommendation_profile_id is not None
                            for item in related_items
                        )
                        and relation_model not in selected_models
                        and any(
                            (item.source_model, item.field_name)
                            in pending_identities
                            for item in related_items
                        )
                    ),
                    available=relation_model in available,
                    can_select=(
                        relation_model in available
                        and selection_handling_by_model.get(relation_model)
                        is handling
                    ),
                    required=any(item.required for item in related_items),
                    pending=any(
                        (item.source_model, item.field_name)
                        in pending_identities
                        for item in related_items
                    ),
                    unavailable_pending=any(
                        (item.source_model, item.field_name)
                        in unavailable_identities
                        for item in related_items
                    ),
                    default_conflict=relation_model in conflicting_models,
                    preserve_allowed=preserve_allowed_by_model[relation_model],
                    saved_choice=relation_model in saved_choice_names,
                    items=tuple(
                        RelatedDataItemView(
                            source_label=item.source_label,
                            field_label=item.field_label,
                            relation_label=labels.get(
                                item.relation_model,
                                _fallback_model_label(item.relation_model),
                            ),
                            technical_name=(
                                f"{item.source_model}.{item.field_name} -> "
                                f"{item.relation_model}"
                            ),
                            required=item.required,
                            reviewed=(
                                (item.source_model, item.field_name)
                                in review.reviewed_identities
                            ),
                        )
                        for item in related_items
                    ),
                )
                for relation_model in sorted(
                    {
                        item.relation_model
                        for item in suggestions
                        if item.handling is handling
                    },
                    key=lambda name: (
                        labels.get(name, _fallback_model_label(name)).casefold(),
                        name,
                    ),
                )
                for related_items in (
                    tuple(
                        item
                        for item in suggestions
                        if item.handling is handling
                        and item.relation_model == relation_model
                    ),
                )
            ),
        )
        for handling in _GROUP_ORDER
        if any(item.handling is handling for item in suggestions)
    )
    selectable_model_names = tuple(
        sorted(
            {
                model.name
                for group in groups
                for model in group.models
                if model.can_select
            }
        )
    )
    exception_models_by_name = {
        model_name: RelatedDataExceptionModelView(
            name=model_name,
            label=labels.get(model_name, _fallback_model_label(model_name)),
            preserve_allowed=preserve_allowed_by_model[model_name],
            items=tuple(
                RelatedDataExceptionItemView(
                    source_label=item.source_label,
                    field_label=item.field_label,
                    required=item.required,
                    recommended_action_title=_exception_recommendation_title(
                        item,
                        root_models=root_models,
                    ),
                )
                for item in sorted(
                    (
                        candidate
                        for candidate in suggestions
                        if candidate.relation_model == model_name
                    ),
                    key=lambda candidate: (
                        candidate.source_label.casefold(),
                        candidate.field_label.casefold(),
                        candidate.source_model,
                        candidate.field_name,
                    ),
                )
            ),
        )
        for model_name in conflicting_model_names
    }
    exception_models = tuple(
        exception_models_by_name[name]
        for name in sorted(
            exception_models_by_name,
            key=lambda name: (
                exception_models_by_name[name].label.casefold(),
                name,
            ),
        )
    )
    base_url = f"/workspaces/{workspace_id}/schema"
    action_counts = {
        action: sum(
            1 for candidate in effective_action_by_model.values()
            if candidate is action
        )
        for action in (
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
            OdooRelationshipCaptureAction.PRESERVE_LINKED,
            OdooRelationshipCaptureAction.MATCH_EXISTING,
            OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
        )
    }
    safe_default_count = sum(
        1
        for item in review.pending
        if item.relation_model not in conflicting_models
        and item.relation_model in available
    )
    return RelatedDataScopeView(
        groups=groups,
        saved_choice_models=tuple(
            sorted(
                (
                    model
                    for group in groups
                    for model in group.models
                    if model.saved_choice
                ),
                key=lambda model: (model.label.casefold(), model.name),
            )
        ),
        selectable_model_names=selectable_model_names,
        all_choices_url=f"{base_url}#odoo-data-choices",
        complete=review.complete,
        pending_count=len(review.pending),
        unavailable_count=len(
            {item.relation_model for item in review.unavailable}
            | (all_effective_conflicts - set(available))
        ),
        safe_default_count=safe_default_count,
        decision_required_count=len(conflicting_model_names),
        conflicting_model_names=conflicting_model_names,
        conflicting_model_labels=tuple(
            labels.get(name, _fallback_model_label(name))
            for name in conflicting_model_names
        ),
        exception_models=exception_models,
        capture_default_count=action_counts[
            OdooRelationshipCaptureAction.CAPTURE_LINKED
        ],
        preserve_default_count=action_counts[
            OdooRelationshipCaptureAction.PRESERVE_LINKED
        ],
        match_default_count=action_counts[
            OdooRelationshipCaptureAction.MATCH_EXISTING
        ],
        exclude_default_count=action_counts[
            OdooRelationshipCaptureAction.DO_NOT_CAPTURE
        ],
    )


def _fallback_model_label(model_name: str) -> str:
    """Keep missing catalogue labels readable without hiding their identity."""

    return model_name.rsplit(".", 1)[-1].replace("_", " ").title()
