"""Classify related Odoo data without expanding a capture implicitly.

The source workflow starts from business records chosen by the data manager.
This module turns their captured relationship metadata into a bounded proposal;
it does not traverse Odoo's model graph or change the saved model scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .odoo_relationship_profiles import (
    DEFAULT_ODOO_RELATIONSHIP_PROFILES,
    OdooRelationshipProfile,
)
from .workspace.business_keys import recommend_business_key
from .workspace.contracts import SchemaField, SchemaModel


class RelatedDataHandling(StrEnum):
    """User decision Impodo needs for one relationship outside the scope."""

    INCLUDE_SUPPORTING = "INCLUDE_SUPPORTING"
    OPTIONAL_BUSINESS_DATA = "OPTIONAL_BUSINESS_DATA"
    REUSE_DESTINATION = "REUSE_DESTINATION"
    ODOO_MANAGED = "ODOO_MANAGED"
    SEPARATE_PROCESS = "SEPARATE_PROCESS"
    EXCLUDE_HISTORY = "EXCLUDE_HISTORY"
    NEEDS_DECISION = "NEEDS_DECISION"


@dataclass(frozen=True, slots=True)
class RelatedDataSuggestion:
    """One eligible source relationship classified for scope review."""

    source_model: str
    source_label: str
    field_name: str
    field_label: str
    relation_model: str
    required: bool
    handling: RelatedDataHandling
    recommendation_profile_id: str | None = None
    recommendation_profile_version: int | None = None
    identity_scope: bool = False


_SELECTABLE_HANDLINGS = frozenset(
    {
        RelatedDataHandling.INCLUDE_SUPPORTING,
        RelatedDataHandling.OPTIONAL_BUSINESS_DATA,
        RelatedDataHandling.REUSE_DESTINATION,
        RelatedDataHandling.SEPARATE_PROCESS,
        RelatedDataHandling.NEEDS_DECISION,
    }
)


def propose_related_odoo_data(
    models: Iterable[SchemaModel],
    *,
    include_selected: bool = False,
    profiles: tuple[
        OdooRelationshipProfile, ...
    ] = DEFAULT_ODOO_RELATIONSHIP_PROFILES,
) -> tuple[RelatedDataSuggestion, ...]:
    """Return deterministic suggestions for eligible links leaving the scope.

    Only relationships already present in captured schema evidence are
    considered. By default the result contains links outside the selected
    scope. ``include_selected`` also retains selected supporting, optional,
    and destination-reuse models so the browser can explain their saved
    scope. Unknown and custom relationships fail closed into an explicit
    decision instead of being followed automatically.
    """

    captured_models = tuple(models)
    selected_names = {model.name for model in captured_models}
    suggestions: list[RelatedDataSuggestion] = []
    for model in captured_models:
        identity = recommend_business_key(model)
        identity_scope_fields = frozenset(
            identity.scope_fields if identity is not None else ()
        )
        for field in model.fields:
            if field.relation == model.name:
                continue
            classification = _classify_relationship(
                model.name,
                field,
                profiles=profiles,
            )
            if not _is_scope_candidate(
                field,
                selected_names,
                handling=classification[0],
                include_selected=include_selected,
            ) or field.relation is None:
                continue
            handling, profile = classification
            suggestions.append(
                RelatedDataSuggestion(
                    source_model=model.name,
                    source_label=model.label,
                    field_name=field.name,
                    field_label=field.label,
                    relation_model=field.relation,
                    required=field.required,
                    handling=handling,
                    recommendation_profile_id=(
                        profile.profile_id if profile is not None else None
                    ),
                    recommendation_profile_version=(
                        profile.version if profile is not None else None
                    ),
                    identity_scope=field.name in identity_scope_fields,
                )
            )
    return tuple(
        sorted(
            suggestions,
            key=lambda item: (
                item.source_label.casefold(),
                item.field_label.casefold(),
                item.source_model,
                item.field_name,
            ),
        )
    )


def related_model_can_be_selected(handling: RelatedDataHandling) -> bool:
    """Return whether this handling permits an inline source-scope choice."""

    return handling in _SELECTABLE_HANDLINGS


def related_model_should_default_linked_only(
    handling: RelatedDataHandling,
) -> bool:
    """Return whether an inline related-data choice should capture reached rows."""

    return handling in _SELECTABLE_HANDLINGS


def _is_scope_candidate(
    field: SchemaField,
    selected_names: set[str],
    *,
    handling: RelatedDataHandling,
    include_selected: bool,
) -> bool:
    """Keep metadata-safe edges visible without granting write capability."""

    return bool(
        field.type in {"many2one", "many2many", "one2many"}
        and field.relation
        and (
            field.relation not in selected_names
            or (
                include_selected
                and related_model_can_be_selected(handling)
            )
        )
        and field.exportable is True
        and field.company_dependent is False
    )


def _classify_relationship(
    source_model: str,
    field: SchemaField,
    *,
    profiles: tuple[OdooRelationshipProfile, ...],
) -> tuple[RelatedDataHandling, OdooRelationshipProfile | None]:
    """Apply capabilities first, then the first exact profile recommendation."""

    if field.related is True or (
        field.computed is True and field.has_inverse is not True
    ) or (
        field.type != "one2many"
        and field.readonly
        and field.has_inverse is not True
    ):
        return RelatedDataHandling.ODOO_MANAGED, None
    for profile in profiles:
        recommendation = profile.recommend(source_model, field)
        if recommendation is not None:
            return RelatedDataHandling(recommendation.value), profile
    return RelatedDataHandling.NEEDS_DECISION, None


__all__ = [
    "RelatedDataHandling",
    "RelatedDataSuggestion",
    "propose_related_odoo_data",
    "related_model_can_be_selected",
    "related_model_should_default_linked_only",
]
