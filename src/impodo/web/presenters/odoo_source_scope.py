"""Present related Odoo data as business scope, not a model checklist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode

from impodo.domain.odoo_source_scope import (
    RelatedDataHandling,
    RelatedDataSuggestion,
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
    items: tuple[RelatedDataItemView, ...]


@dataclass(frozen=True, slots=True)
class RelatedDataScopeView:
    """Complete source-page projection for related-data decisions."""

    groups: tuple[RelatedDataGroupView, ...]
    recommended_review_url: str | None
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
) -> RelatedDataScopeView:
    """Group domain decisions and prepare a safe model-review link."""

    labels = model_labels or {}
    groups = tuple(
        RelatedDataGroupView(
            handling=handling,
            title=_GROUP_COPY[handling][0],
            description=_GROUP_COPY[handling][1],
            attention=handling in _ATTENTION_HANDLINGS,
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
                for item in suggestions
                if item.handling is handling
            ),
        )
        for handling in _GROUP_ORDER
        if any(item.handling is handling for item in suggestions)
    )
    recommended_models = tuple(
        sorted(
            {
                item.relation_model
                for item in suggestions
                if item.handling is RelatedDataHandling.INCLUDE_SUPPORTING
            }
        )
    )
    query = urlencode(
        [("suggested_model", model) for model in recommended_models]
    )
    base_url = f"/workspaces/{workspace_id}/schema"
    return RelatedDataScopeView(
        groups=groups,
        recommended_review_url=(
            f"{base_url}?{query}#odoo-data-choices"
            if recommended_models
            else None
        ),
        all_choices_url=f"{base_url}#odoo-data-choices",
    )


def _fallback_model_label(model_name: str) -> str:
    """Keep missing catalogue labels readable without hiding their identity."""

    return model_name.rsplit(".", 1)[-1].replace("_", " ").title()
