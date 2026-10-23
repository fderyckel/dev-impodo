"""Classify related Odoo data without expanding a capture implicitly.

The source workflow starts from business records chosen by the data manager.
This module turns their captured relationship metadata into a bounded proposal;
it does not traverse Odoo's model graph or change the saved model scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .odoo_relationship_profiles import (
    DEFAULT_ODOO_RELATIONSHIP_PROFILES,
    OdooRelationshipProfile,
)
from .workspace.business_keys import recommend_business_key
from .workspace.contracts import SchemaField, SchemaModel


class RelatedDataHandling(StrEnum):
    """Business handling available for one relationship outside root scope."""

    INCLUDE_SUPPORTING = "INCLUDE_SUPPORTING"
    TRANSFER_RELATED = "TRANSFER_RELATED"
    OPTIONAL_BUSINESS_DATA = "OPTIONAL_BUSINESS_DATA"
    REUSE_DESTINATION = "REUSE_DESTINATION"
    ODOO_MANAGED = "ODOO_MANAGED"
    SEPARATE_PROCESS = "SEPARATE_PROCESS"
    EXCLUDE_HISTORY = "EXCLUDE_HISTORY"
    NEEDS_DECISION = "NEEDS_DECISION"


class RelatedDataDefaultAction(StrEnum):
    """Direction-aware default exposed to source-scope presenters."""

    CAPTURE_LINKED = "CAPTURE_LINKED"
    PRESERVE_LINKED = "PRESERVE_LINKED"
    MATCH_EXISTING = "MATCH_EXISTING"
    DO_NOT_CAPTURE = "DO_NOT_CAPTURE"


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
    relationship_type: str = "many2one"
    recommendation_profile_id: str | None = None
    recommendation_profile_version: int | None = None
    identity_scope: bool = False

    @property
    def identity(self) -> tuple[str, str]:
        """Return the stable source-model and field identity of this edge."""

        return self.source_model, self.field_name


_SELECTABLE_HANDLINGS = frozenset(
    {
        RelatedDataHandling.INCLUDE_SUPPORTING,
        RelatedDataHandling.TRANSFER_RELATED,
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
                    relationship_type=field.type,
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


def is_writable_related_many2one(field: SchemaField) -> bool:
    """Return whether a stored related Many2one can carry an explicit value."""

    return bool(
        field.type == "many2one"
        and field.related is True
        and not field.readonly
        and field.stored is not False
    )


def related_model_should_default_linked_only(
    handling: RelatedDataHandling,
) -> bool:
    """Return whether an inline related-data choice should capture reached rows."""

    return handling in _SELECTABLE_HANDLINGS


def recommended_related_data_action(
    suggestion: RelatedDataSuggestion,
) -> RelatedDataDefaultAction:
    """Return the bounded default for one directional relationship edge.

    A forward Many2one stores one value on the selected record, so preserving
    that value is the ordinary safe default. Collections expand membership and
    therefore stay closed unless a profile identifies them as supporting data.
    Destination-owned records remain existing-only, while a separate-process
    Many2one is also matched rather than silently omitted.
    """

    if suggestion.handling is RelatedDataHandling.REUSE_DESTINATION:
        return RelatedDataDefaultAction.MATCH_EXISTING
    if suggestion.handling is RelatedDataHandling.TRANSFER_RELATED:
        return RelatedDataDefaultAction.CAPTURE_LINKED
    if suggestion.relationship_type == "many2one":
        if suggestion.handling is RelatedDataHandling.SEPARATE_PROCESS:
            return RelatedDataDefaultAction.MATCH_EXISTING
        if related_model_can_be_selected(suggestion.handling):
            return RelatedDataDefaultAction.PRESERVE_LINKED
        return RelatedDataDefaultAction.DO_NOT_CAPTURE
    if suggestion.relationship_type in {"many2many", "one2many"}:
        if (
            suggestion.handling is RelatedDataHandling.INCLUDE_SUPPORTING
            and suggestion.recommendation_profile_id is not None
        ):
            # A profiled owned collection is business-row membership, not a
            # minimum-create leaf. Capture its reached rows with their full
            # selected business values, then govern their outgoing links in
            # the same bounded graph.
            return RelatedDataDefaultAction.CAPTURE_LINKED
        return RelatedDataDefaultAction.DO_NOT_CAPTURE
    return RelatedDataDefaultAction.DO_NOT_CAPTURE


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

    recommendation: tuple[
        RelatedDataHandling, OdooRelationshipProfile
    ] | None = None
    for profile in profiles:
        profile_recommendation = profile.recommend(source_model, field)
        if profile_recommendation is not None:
            recommendation = (
                RelatedDataHandling(profile_recommendation.value),
                profile,
            )
            break
    if (
        recommendation is not None
        and recommendation[0] is RelatedDataHandling.ODOO_MANAGED
    ):
        return recommendation
    writable_related_many2one = is_writable_related_many2one(field)
    profiled_writable_owned_collection = bool(
        field.type == "one2many"
        and field.relation_field
        and not field.readonly
        and recommendation is not None
        and recommendation[0] is RelatedDataHandling.INCLUDE_SUPPORTING
    )
    readonly_unowned_inverse_collection = bool(
        field.type == "one2many"
        and field.readonly
        and field.stored is False
        and not field.relation_field
        and field.has_inverse is not True
    )
    if not (
        writable_related_many2one or profiled_writable_owned_collection
    ) and (
        field.related is True
        or (field.computed is True and field.has_inverse is not True)
        or readonly_unowned_inverse_collection
        or (
            field.type != "one2many"
            and field.readonly
            and field.has_inverse is not True
        )
    ):
        return RelatedDataHandling.ODOO_MANAGED, None
    if recommendation is not None:
        return recommendation
    return RelatedDataHandling.NEEDS_DECISION, None


__all__ = [
    "RelatedDataDefaultAction",
    "RelatedDataHandling",
    "RelatedDataSuggestion",
    "is_writable_related_many2one",
    "propose_related_odoo_data",
    "recommended_related_data_action",
    "related_model_can_be_selected",
    "related_model_should_default_linked_only",
]
