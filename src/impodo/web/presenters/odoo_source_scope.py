"""Present related Odoo data as business scope, not a model checklist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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
    checked: bool
    recommended: bool
    available: bool
    can_select: bool
    required: bool
    items: tuple[RelatedDataItemView, ...]


@dataclass(frozen=True, slots=True)
class RelatedDataScopeView:
    """Complete source-page projection for related-data decisions."""

    groups: tuple[RelatedDataGroupView, ...]
    selectable_model_names: tuple[str, ...]
    all_choices_url: str


_GROUP_COPY = {
    RelatedDataHandling.INCLUDE_SUPPORTING: (
        "Needed to keep the records meaningful",
        "Impodo recommends including these supporting records and matching "
        "them in the destination before the main records are loaded.",
    ),
    RelatedDataHandling.OPTIONAL_BUSINESS_DATA: (
        "Include when it belongs to this migration",
        "Choose these only when the related product information is part of "
        "the agreed migration scope.",
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
        "Needs a decision",
        "Impodo cannot safely infer the business meaning of these links. "
        "Review them before freezing the source.",
    ),
}

_GROUP_ORDER = tuple(_GROUP_COPY)

_ATTENTION_HANDLINGS = frozenset(
    {
        RelatedDataHandling.INCLUDE_SUPPORTING,
        RelatedDataHandling.OPTIONAL_BUSINESS_DATA,
        RelatedDataHandling.REUSE_DESTINATION,
        RelatedDataHandling.SEPARATE_PROCESS,
        RelatedDataHandling.NEEDS_DECISION,
    }
)


def build_related_data_scope_view(
    workspace_id: str,
    suggestions: tuple[RelatedDataSuggestion, ...],
    *,
    model_labels: Mapping[str, str] | None = None,
    selected_models: frozenset[str] = frozenset(),
    available_models: frozenset[str] | None = None,
) -> RelatedDataScopeView:
    """Group domain decisions and expose safe inline model choices."""

    labels = model_labels or {}
    available = available_models if available_models is not None else frozenset(labels)
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
                    checked=relation_model in selected_models,
                    recommended=(
                        handling in {
                            RelatedDataHandling.INCLUDE_SUPPORTING,
                            RelatedDataHandling.REUSE_DESTINATION,
                        }
                        and relation_model not in selected_models
                    ),
                    available=relation_model in available,
                    can_select=(
                        relation_model in available
                        and selection_handling_by_model.get(relation_model)
                        is handling
                    ),
                    required=any(item.required for item in related_items),
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
    base_url = f"/workspaces/{workspace_id}/schema"
    return RelatedDataScopeView(
        groups=groups,
        selectable_model_names=selectable_model_names,
        all_choices_url=f"{base_url}#odoo-data-choices",
    )


def _fallback_model_label(model_name: str) -> str:
    """Keep missing catalogue labels readable without hiding their identity."""

    return model_name.rsplit(".", 1)[-1].replace("_", " ").title()
