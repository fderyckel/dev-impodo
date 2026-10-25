"""Persist derived or overridden Odoo-source capture intent per schema edge.

Stage 2 derives compatible, direction-aware defaults and keeps model-level
overrides under Advanced. This contract expands either source into immutable
field-level evidence. It records source-capture intent and whether an included
related model may expand; destination matching supplies its portable identity
and must honor any saved no-write constraint.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from impodo.domain.serialization import canonical_json, content_hash

from .odoo_source_scope import (
    RelatedDataHandling,
    RelatedDataSuggestion,
    recommended_related_data_action,
    related_model_can_be_selected,
)

ODOO_RELATIONSHIP_SCOPE_CONTRACT_VERSION = 6
_SUPPORTED_RELATIONSHIP_SCOPE_CONTRACT_VERSIONS = frozenset({1, 2, 3, 4, 5, 6})
_RELATIONSHIP_TYPES = frozenset({"many2one", "many2many", "one2many"})
_TECHNICAL_MODEL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_TECHNICAL_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class OdooRelationshipScopeError(ValueError):
    """Raised when saved Odoo relationship intent is not trustworthy."""


class OdooRelationshipCaptureAction(StrEnum):
    """Source-capture outcome saved for one Odoo relationship field."""

    CAPTURE_LINKED = "CAPTURE_LINKED"
    PRESERVE_LINKED = "PRESERVE_LINKED"
    MATCH_EXISTING = "MATCH_EXISTING"
    DO_NOT_CAPTURE = "DO_NOT_CAPTURE"
    ODOO_MANAGED = "ODOO_MANAGED"
    SEPARATE_PROCESS = "SEPARATE_PROCESS"
    EXCLUDE_HISTORY = "EXCLUDE_HISTORY"


@dataclass(frozen=True, slots=True)
class OdooRelationshipActionDefaults:
    """Compatible model policies derived from saved intent and safe defaults."""

    actions_by_model: tuple[
        tuple[str, OdooRelationshipCaptureAction], ...
    ]
    conflicting_models: tuple[str, ...]

    @property
    def actions(self) -> dict[str, OdooRelationshipCaptureAction]:
        """Return a mutable mapping for command-boundary validation."""

        return dict(self.actions_by_model)


_AUTOMATIC_ACTIONS = {
    RelatedDataHandling.ODOO_MANAGED: OdooRelationshipCaptureAction.ODOO_MANAGED,
    RelatedDataHandling.SEPARATE_PROCESS: (
        OdooRelationshipCaptureAction.SEPARATE_PROCESS
    ),
    RelatedDataHandling.EXCLUDE_HISTORY: (
        OdooRelationshipCaptureAction.EXCLUDE_HISTORY
    ),
}


@dataclass(frozen=True, slots=True)
class OdooRelationshipScopeDecision:
    """One immutable source-capture decision for one relational schema field."""

    source_model: str
    field_name: str
    relation_model: str
    required: bool
    handling: RelatedDataHandling
    action: OdooRelationshipCaptureAction
    relationship_type: str | None = None
    recommendation_profile_id: str | None = None
    recommendation_profile_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "handling", RelatedDataHandling(self.handling))
        object.__setattr__(
            self,
            "action",
            OdooRelationshipCaptureAction(self.action),
        )
        if (
            _TECHNICAL_MODEL.fullmatch(self.source_model) is None
            or _TECHNICAL_FIELD.fullmatch(self.field_name) is None
            or _TECHNICAL_MODEL.fullmatch(self.relation_model) is None
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship decision identity is invalid"
            )
        if not isinstance(self.required, bool):
            raise OdooRelationshipScopeError(
                "Odoo relationship requiredness is invalid"
            )
        if (
            self.relationship_type is not None
            and self.relationship_type not in _RELATIONSHIP_TYPES
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship type is invalid"
            )
        profile_id = self.recommendation_profile_id
        profile_version = self.recommendation_profile_version
        if (profile_id is None) != (profile_version is None):
            raise OdooRelationshipScopeError(
                "Odoo relationship recommendation provenance is incomplete"
            )
        if profile_id is not None and (
            not profile_id.strip()
            or len(profile_id) > 200
            or profile_version is None
            or profile_version < 1
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship recommendation provenance is invalid"
            )
        if related_model_can_be_selected(self.handling):
            selectable_actions = {
                OdooRelationshipCaptureAction.CAPTURE_LINKED,
                OdooRelationshipCaptureAction.PRESERVE_LINKED,
                OdooRelationshipCaptureAction.MATCH_EXISTING,
                OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
            }
            if self.handling is RelatedDataHandling.SEPARATE_PROCESS:
                # Scopes written before separate-process recommendations became
                # selectable remain readable and can be replaced on review.
                selectable_actions.add(
                    OdooRelationshipCaptureAction.SEPARATE_PROCESS
                )
            if self.action not in selectable_actions:
                raise OdooRelationshipScopeError(
                    "Selectable Odoo relationship decision is invalid"
                )
            if (
                self.handling is RelatedDataHandling.REUSE_DESTINATION
                and self.action is OdooRelationshipCaptureAction.PRESERVE_LINKED
            ):
                raise OdooRelationshipScopeError(
                    "Destination-owned Odoo records cannot use minimum creation"
                )
        elif _AUTOMATIC_ACTIONS.get(self.handling) is not self.action:
            raise OdooRelationshipScopeError(
                "Automatic Odoo relationship decision is invalid"
            )

    @property
    def identity(self) -> tuple[str, str]:
        """Return the stable source-model and field identity of this edge."""

        return self.source_model, self.field_name

    def matches_suggestion(self, suggestion: RelatedDataSuggestion) -> bool:
        """Return whether this decision still describes current discovery."""

        return bool(
            self.identity == (suggestion.source_model, suggestion.field_name)
            and self.relation_model == suggestion.relation_model
            and self.relationship_type == suggestion.relationship_type
            and self.required is suggestion.required
            and self.handling is suggestion.handling
            and self.recommendation_profile_id
            == suggestion.recommendation_profile_id
            and self.recommendation_profile_version
            == suggestion.recommendation_profile_version
        )

    def to_dict(
        self,
        *,
        contract_version: int = ODOO_RELATIONSHIP_SCOPE_CONTRACT_VERSION,
    ) -> dict[str, object]:
        """Return the complete portable decision payload."""

        result: dict[str, object] = {
            "action": self.action.value,
            "field_name": self.field_name,
            "handling": self.handling.value,
            "recommendation_profile_id": self.recommendation_profile_id,
            "recommendation_profile_version": self.recommendation_profile_version,
            "relation_model": self.relation_model,
            "required": self.required,
            "source_model": self.source_model,
        }
        if contract_version >= 4:
            result["relationship_type"] = self.relationship_type
        return result

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        contract_version: int = ODOO_RELATIONSHIP_SCOPE_CONTRACT_VERSION,
    ) -> OdooRelationshipScopeDecision:
        """Restore one decision while rejecting changed or incomplete shapes."""

        expected_keys = {
            "action",
            "field_name",
            "handling",
            "recommendation_profile_id",
            "recommendation_profile_version",
            "relation_model",
            "required",
            "source_model",
        }
        if contract_version >= 4:
            expected_keys.add("relationship_type")
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise OdooRelationshipScopeError(
                "Odoo relationship decision shape is invalid"
            )
        try:
            return cls(
                source_model=str(value["source_model"]),
                field_name=str(value["field_name"]),
                relation_model=str(value["relation_model"]),
                required=value["required"],
                handling=RelatedDataHandling(value["handling"]),
                action=OdooRelationshipCaptureAction(value["action"]),
                relationship_type=(
                    str(value["relationship_type"])
                    if contract_version >= 4
                    else None
                ),
                recommendation_profile_id=(
                    str(value["recommendation_profile_id"])
                    if value["recommendation_profile_id"] is not None
                    else None
                ),
                recommendation_profile_version=(
                    int(value["recommendation_profile_version"])
                    if value["recommendation_profile_version"] is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, OdooRelationshipScopeError):
                raise
            raise OdooRelationshipScopeError(
                "Odoo relationship decision is invalid"
            ) from error


def relationship_scope_decisions(
    suggestions: Iterable[RelatedDataSuggestion],
    *,
    included_models: Iterable[str] = (),
    actions_by_model: Mapping[
        str, OdooRelationshipCaptureAction | str
    ] | None = None,
    available_models: Iterable[str] | None = None,
    current_scope: OdooRelationshipScope | None = None,
    unresolved_models: Iterable[str] = (),
) -> tuple[OdooRelationshipScopeDecision, ...]:
    """Expand grouped model choices into deterministic field-level decisions.

    When model availability is supplied, a selectable edge can be decided only
    while its related model is available to the user. Any current decision for
    an unavailable edge is retained, while an unseen unavailable edge remains
    absent so review can report it as a blocker instead of inferring exclusion.
    Selectable edges for an unresolved model are likewise omitted so a newly
    discovered model-policy conflict remains reviewable instead of becoming an
    implicit exclusion.
    """

    current = tuple(suggestions)
    included = frozenset(included_models)
    unresolved = frozenset(str(model) for model in unresolved_models)
    try:
        actions = {
            str(model): OdooRelationshipCaptureAction(action)
            for model, action in (actions_by_model or {}).items()
        }
    except ValueError as error:
        raise OdooRelationshipScopeError(
            "Odoo relationship action is invalid"
        ) from error
    if actions_by_model is not None and included:
        raise OdooRelationshipScopeError(
            "Choose Odoo relationship actions or included models, not both"
        )
    assigned_and_unresolved = unresolved & (included | set(actions))
    if assigned_and_unresolved:
        raise OdooRelationshipScopeError(
            f"{min(assigned_and_unresolved)} cannot be both unresolved "
            "and assigned an Odoo relationship action"
        )
    available = (
        frozenset(available_models)
        if available_models is not None
        else None
    )
    current_selectable_models = {
        suggestion.relation_model
        for suggestion in current
        if related_model_can_be_selected(suggestion.handling)
    }
    decidable = tuple(
        suggestion
        for suggestion in current
        if (
            not related_model_can_be_selected(suggestion.handling)
            or available is None
            or suggestion.relation_model in available
        )
    )
    decisions: list[OdooRelationshipScopeDecision] = []
    selectable_models: set[str] = set()
    for suggestion in decidable:
        if related_model_can_be_selected(suggestion.handling):
            selectable_models.add(suggestion.relation_model)
            if suggestion.relation_model in unresolved:
                continue
            action = actions.get(suggestion.relation_model)
            if action is None:
                action = (
                    OdooRelationshipCaptureAction.CAPTURE_LINKED
                    if suggestion.relation_model in included
                    else OdooRelationshipCaptureAction.DO_NOT_CAPTURE
                )
            if action not in {
                OdooRelationshipCaptureAction.CAPTURE_LINKED,
                OdooRelationshipCaptureAction.PRESERVE_LINKED,
                OdooRelationshipCaptureAction.MATCH_EXISTING,
                OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
            }:
                raise OdooRelationshipScopeError(
                    f"{suggestion.relation_model} has an invalid Odoo relationship action"
                )
        else:
            action = _AUTOMATIC_ACTIONS[suggestion.handling]
        decisions.append(
            OdooRelationshipScopeDecision(
                source_model=suggestion.source_model,
                field_name=suggestion.field_name,
                relation_model=suggestion.relation_model,
                required=suggestion.required,
                handling=suggestion.handling,
                action=action,
                relationship_type=suggestion.relationship_type,
                recommendation_profile_id=suggestion.recommendation_profile_id,
                recommendation_profile_version=(
                    suggestion.recommendation_profile_version
                ),
            )
        )
    unknown = (
        (included | set(actions)) - selectable_models
    ) | (set(unresolved) - current_selectable_models)
    if unknown:
        raise OdooRelationshipScopeError(
            f"{min(unknown)} is not a selectable Odoo relationship"
        )
    decided_identities = {item.identity for item in decisions}
    current_identities = {
        (item.source_model, item.field_name) for item in current
    }
    suggestion_by_identity = {item.identity: item for item in current}
    for item in (
        current_scope.decisions
        if current_scope is not None and current_scope.contract_version >= 6
        else ()
    ):
        if (
            item.identity not in current_identities
            or item.identity in decided_identities
        ):
            continue
        suggestion = suggestion_by_identity[item.identity]
        if (
            related_model_can_be_selected(suggestion.handling)
            and suggestion.relation_model in unresolved
        ):
            continue
        try:
            decisions.append(
                _decision_from_suggestion(suggestion, item.action)
            )
        except OdooRelationshipScopeError:
            # Changed capabilities can make a legacy action unsafe. Do not
            # smuggle that action into a current contract; the review remains
            # unresolved and names the unavailable edge as a blocker.
            continue
    return tuple(
        sorted(
            decisions,
            key=lambda item: (
                item.source_model,
                item.field_name,
                item.relation_model,
            ),
        )
    )


def recommended_relationship_capture_action(
    suggestion: RelatedDataSuggestion,
    *,
    selected_root: bool = False,
) -> OdooRelationshipCaptureAction:
    """Translate the canonical direction-aware default to capture intent."""

    if (
        selected_root
        and suggestion.handling is not RelatedDataHandling.REUSE_DESTINATION
    ):
        return OdooRelationshipCaptureAction.CAPTURE_LINKED
    return OdooRelationshipCaptureAction(
        recommended_related_data_action(suggestion).value
    )


def propose_relationship_action_defaults(
    suggestions: Iterable[RelatedDataSuggestion],
    *,
    root_models: Iterable[str] = (),
    current_scope: OdooRelationshipScope | None = None,
    explicit_actions_by_model: Mapping[
        str, OdooRelationshipCaptureAction | str
    ] | None = None,
) -> OdooRelationshipActionDefaults:
    """Derive safe model policies and name incompatible edge defaults.

    Source-scope evidence is stored per edge, while destination write policy is
    still model-wide. A model reached by edges that require different policies
    is therefore an exception; callers must obtain one deliberate model policy
    instead of silently collapsing those edges.
    """

    roots = frozenset(root_models)
    try:
        explicit = {
            str(model): OdooRelationshipCaptureAction(action)
            for model, action in (explicit_actions_by_model or {}).items()
        }
    except ValueError as error:
        raise OdooRelationshipScopeError(
            "Odoo relationship action is invalid"
        ) from error
    saved_explicit = (
        current_scope.explicit_actions
        if explicit_actions_by_model is None
        and current_scope is not None
        and current_scope.contract_version >= 6
        else {}
    )
    actions_by_model: dict[str, set[OdooRelationshipCaptureAction]] = {}
    selectable_models: set[str] = set()
    decision_required_models: set[str] = set()
    for suggestion in suggestions:
        if not related_model_can_be_selected(suggestion.handling):
            continue
        model = suggestion.relation_model
        selectable_models.add(model)
        if model in explicit:
            action = explicit[model]
        elif model in saved_explicit:
            action = saved_explicit[model]
        elif _requires_explicit_collection_action(suggestion):
            # Required custom collections cannot be omitted merely because
            # no standard profile classifies their ownership semantics.
            decision_required_models.add(model)
            continue
        else:
            action = recommended_relationship_capture_action(
                suggestion,
                selected_root=model in roots,
            )
        # Rebuild through the current suggestion to apply capability guards to
        # forged overrides and to actions retained from an older contract.
        try:
            _decision_from_suggestion(suggestion, action)
        except OdooRelationshipScopeError:
            if model in explicit:
                raise
            decision_required_models.add(model)
            continue
        actions_by_model.setdefault(model, set()).add(action)
    unknown = set(explicit) - selectable_models
    if unknown:
        raise OdooRelationshipScopeError(
            f"{min(unknown)} is not a selectable Odoo relationship"
        )
    conflicts = tuple(
        sorted(
            decision_required_models
            | {
                model
                for model, actions in actions_by_model.items()
                if len(actions) > 1
            }
        )
    )
    return OdooRelationshipActionDefaults(
        actions_by_model=tuple(
            sorted(
                (model, next(iter(actions)))
                for model, actions in actions_by_model.items()
                if len(actions) == 1
            )
        ),
        conflicting_models=conflicts,
    )


def _requires_explicit_collection_action(
    suggestion: RelatedDataSuggestion,
) -> bool:
    """Protect required custom collections from automatic exclusion."""

    return bool(
        suggestion.required
        and suggestion.relationship_type in {"many2many", "one2many"}
        and suggestion.recommendation_profile_id is None
    )


def _decision_from_suggestion(
    suggestion: RelatedDataSuggestion,
    action: OdooRelationshipCaptureAction,
) -> OdooRelationshipScopeDecision:
    """Hydrate current edge evidence while preserving its chosen action."""

    return OdooRelationshipScopeDecision(
        source_model=suggestion.source_model,
        field_name=suggestion.field_name,
        relation_model=suggestion.relation_model,
        required=suggestion.required,
        handling=suggestion.handling,
        action=action,
        relationship_type=suggestion.relationship_type,
        recommendation_profile_id=suggestion.recommendation_profile_id,
        recommendation_profile_version=(
            suggestion.recommendation_profile_version
        ),
    )


@dataclass(frozen=True, slots=True)
class OdooRelationshipScopeReview:
    """State of the recursive source-relationship review for current schema."""

    reviewed_identities: frozenset[tuple[str, str]]
    pending: tuple[RelatedDataSuggestion, ...]
    unavailable: tuple[RelatedDataSuggestion, ...]

    @property
    def complete(self) -> bool:
        """Return whether every selectable current edge has a saved decision."""

        return not self.pending and not self.unavailable

    @property
    def unresolved(self) -> tuple[RelatedDataSuggestion, ...]:
        """Return all unresolved edges in deterministic discovery order."""

        return (*self.pending, *self.unavailable)


def review_relationship_scope(
    suggestions: Iterable[RelatedDataSuggestion],
    scope: OdooRelationshipScope | None,
    *,
    available_models: Iterable[str] | None = None,
) -> OdooRelationshipScopeReview:
    """Find newly discovered or changed selectable edges requiring review.

    Missing automatic Odoo-managed, separate-process, and excluded-history
    outcomes do not require a user action. A selectable edge is complete only
    when its saved decision still matches the current relation, requiredness,
    handling, and recommendation provenance. When model availability is
    supplied, an unresolved edge whose related model is unavailable becomes a
    named blocker instead of a checkbox the browser cannot honor.
    """

    current = tuple(suggestions)
    decisions = {
        item.identity: item
        for item in (scope.decisions if scope is not None else ())
    }
    explicit = (
        scope.explicit_actions
        if scope is not None and scope.contract_version >= 6
        else {}
    )
    reviewed_identities: set[tuple[str, str]] = set()
    if scope is not None and scope.contract_version >= 6:
        for suggestion in current:
            decision = decisions.get(suggestion.identity)
            if decision is None or not decision.matches_suggestion(suggestion):
                continue
            expected_action = explicit.get(suggestion.relation_model)
            if expected_action is None:
                if _requires_explicit_collection_action(suggestion):
                    continue
                expected_action = recommended_relationship_capture_action(
                    suggestion,
                    selected_root=suggestion.relation_model in scope.root_models,
                )
            if decision.action is expected_action:
                reviewed_identities.add(suggestion.identity)
    reviewed = frozenset(reviewed_identities)
    selectable = tuple(
        suggestion
        for suggestion in current
        if related_model_can_be_selected(suggestion.handling)
        and (suggestion.source_model, suggestion.field_name) not in reviewed
    )
    if available_models is None:
        pending = selectable
        unavailable: tuple[RelatedDataSuggestion, ...] = ()
    else:
        available = frozenset(available_models)
        pending = tuple(
            item for item in selectable if item.relation_model in available
        )
        unavailable = tuple(
            item for item in selectable if item.relation_model not in available
        )
    return OdooRelationshipScopeReview(
        reviewed_identities=reviewed,
        pending=pending,
        unavailable=unavailable,
    )


def relationship_expansion_suggestions(
    suggestions: Iterable[RelatedDataSuggestion],
    scope: OdooRelationshipScope | None,
) -> tuple[RelatedDataSuggestion, ...]:
    """Close optional leaf expansion while retaining required create inputs."""

    return _filter_relationship_expansion_suggestions(
        suggestions,
        reachable_models=(
            frozenset(scope.root_models) | scope.included_models
            if scope is not None and scope.root_models
            else None
        ),
        reference_models=(
            scope.reference_models if scope is not None else frozenset()
        ),
        create_if_missing_models=(
            scope.create_if_missing_models
            if scope is not None
            else frozenset()
        ),
        root_models=(
            frozenset(scope.root_models)
            if scope is not None
            else frozenset()
        ),
    )


def relationship_expansion_suggestions_for_decisions(
    suggestions: Iterable[RelatedDataSuggestion],
    decisions: Iterable[OdooRelationshipScopeDecision],
    *,
    root_models: Iterable[str] = (),
) -> tuple[RelatedDataSuggestion, ...]:
    """Apply the canonical frontier policy to a not-yet-persisted decision set."""

    current = tuple(decisions)
    included_models = frozenset(
        item.relation_model
        for item in current
        if item.action
        in {
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
            OdooRelationshipCaptureAction.PRESERVE_LINKED,
            OdooRelationshipCaptureAction.MATCH_EXISTING,
        }
    )
    roots = frozenset(root_models)
    return _filter_relationship_expansion_suggestions(
        suggestions,
        reachable_models=(roots | included_models if roots else None),
        reference_models=frozenset(
            item.relation_model
            for item in current
            if item.action is OdooRelationshipCaptureAction.MATCH_EXISTING
        ),
        create_if_missing_models=frozenset(
            item.relation_model
            for item in current
            if item.action is OdooRelationshipCaptureAction.PRESERVE_LINKED
        ),
        root_models=roots,
    )


def _filter_relationship_expansion_suggestions(
    suggestions: Iterable[RelatedDataSuggestion],
    *,
    reachable_models: frozenset[str] | None,
    reference_models: frozenset[str],
    create_if_missing_models: frozenset[str],
    root_models: frozenset[str],
) -> tuple[RelatedDataSuggestion, ...]:
    """Keep one shared implementation of graph reachability and leaf closure."""

    return tuple(
        item for item in suggestions
        if (reachable_models is None or item.source_model in reachable_models)
        and (
            item.source_model in root_models
            or item.source_model not in reference_models
            or item.identity_scope
        )
        and (
            item.source_model in root_models
            or item.source_model not in create_if_missing_models
            or item.required
            or item.identity_scope
            or item.handling is RelatedDataHandling.INCLUDE_SUPPORTING
            or item.handling is RelatedDataHandling.TRANSFER_RELATED
        )
    )


@dataclass(frozen=True, slots=True)
class OdooRelationshipScope:
    """One immutable revision of every reviewed Odoo relationship edge."""

    scope_id: str
    version: int
    decisions: tuple[OdooRelationshipScopeDecision, ...]
    recorded_at: datetime
    recorded_by: str
    content_hash: str
    root_models: tuple[str, ...] = ()
    explicit_actions_by_model: tuple[
        tuple[str, OdooRelationshipCaptureAction], ...
    ] = ()
    contract_version: int = ODOO_RELATIONSHIP_SCOPE_CONTRACT_VERSION
    _calculate_content_hash: InitVar[bool] = False

    def __post_init__(self, _calculate_content_hash: bool) -> None:
        try:
            UUID(self.scope_id)
        except (AttributeError, ValueError) as error:
            raise OdooRelationshipScopeError(
                "Odoo relationship scope ID is invalid"
            ) from error
        if (
            self.contract_version
            not in _SUPPORTED_RELATIONSHIP_SCOPE_CONTRACT_VERSIONS
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship scope contract version is unsupported"
            )
        if self.version < 1:
            raise OdooRelationshipScopeError(
                "Odoo relationship scope version must be positive"
            )
        decisions = tuple(self.decisions)
        expected_order = tuple(
            sorted(
                decisions,
                key=lambda item: (
                    item.source_model,
                    item.field_name,
                    item.relation_model,
                ),
            )
        )
        if decisions != expected_order or len(
            {item.identity for item in decisions}
        ) != len(decisions):
            raise OdooRelationshipScopeError(
                "Odoo relationship decisions must be ordered and unique by field"
            )
        roots = tuple(self.root_models)
        if (
            roots != tuple(sorted(set(roots)))
            or any(_TECHNICAL_MODEL.fullmatch(item) is None for item in roots)
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship root models must be ordered and unique"
            )
        if self.contract_version == 1 and roots:
            raise OdooRelationshipScopeError(
                "Legacy Odoo relationship scopes cannot contain root models"
            )
        explicit_actions = tuple(
            (str(model), OdooRelationshipCaptureAction(action))
            for model, action in self.explicit_actions_by_model
        )
        if (
            explicit_actions
            != tuple(sorted(explicit_actions, key=lambda item: item[0]))
            or len({model for model, _action in explicit_actions})
            != len(explicit_actions)
            or any(
                _TECHNICAL_MODEL.fullmatch(model) is None
                or action
                not in {
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                    OdooRelationshipCaptureAction.PRESERVE_LINKED,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                    OdooRelationshipCaptureAction.DO_NOT_CAPTURE,
                }
                for model, action in explicit_actions
            )
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship explicit actions are invalid"
            )
        if self.contract_version < 6 and explicit_actions:
            raise OdooRelationshipScopeError(
                "Legacy Odoo relationship scopes cannot contain explicit actions"
            )
        decision_models = {
            item.relation_model for item in decisions
            if related_model_can_be_selected(item.handling)
        }
        if any(
            model not in decision_models
            or any(
                item.action is not action
                for item in decisions
                if item.relation_model == model
                and related_model_can_be_selected(item.handling)
            )
            for model, action in explicit_actions
        ):
            raise OdooRelationshipScopeError(
                "Odoo relationship explicit actions do not match the decisions"
            )
        if self.contract_version < 3 and any(
            item.action is OdooRelationshipCaptureAction.PRESERVE_LINKED
            for item in decisions
        ):
            raise OdooRelationshipScopeError(
                "Legacy Odoo relationship scopes cannot preserve linked records"
            )
        if self.contract_version >= 4 and any(
            item.relationship_type not in _RELATIONSHIP_TYPES
            for item in decisions
        ):
            raise OdooRelationshipScopeError(
                "Current Odoo relationship scopes require relationship types"
            )
        if self.contract_version < 4 and any(
            item.relationship_type is not None for item in decisions
        ):
            raise OdooRelationshipScopeError(
                "Legacy Odoo relationship scopes cannot contain relationship types"
            )
        object.__setattr__(self, "root_models", roots)
        object.__setattr__(self, "explicit_actions_by_model", explicit_actions)
        if self.recorded_at.tzinfo is None:
            raise OdooRelationshipScopeError(
                "Odoo relationship scope time must be timezone-aware"
            )
        if not self.recorded_by.strip() or len(self.recorded_by) > 200:
            raise OdooRelationshipScopeError(
                "Odoo relationship scope actor is invalid"
            )
        expected_hash = content_hash(self._semantic_dict())
        if _calculate_content_hash:
            if self.content_hash:
                raise OdooRelationshipScopeError(
                    "New Odoo relationship scope already has a content hash"
                )
            object.__setattr__(self, "content_hash", expected_hash)
        elif _HASH.fullmatch(self.content_hash) is None:
            raise OdooRelationshipScopeError(
                "Odoo relationship scope content hash is invalid"
            )
        if self.content_hash != expected_hash:
            raise OdooRelationshipScopeError(
                "Odoo relationship scope content hash does not match its decisions"
            )

    @classmethod
    def create(
        cls,
        *,
        scope_id: str,
        version: int,
        decisions: tuple[OdooRelationshipScopeDecision, ...],
        root_models: tuple[str, ...] = (),
        explicit_actions_by_model: tuple[
            tuple[str, OdooRelationshipCaptureAction], ...
        ] = (),
        recorded_at: datetime,
        recorded_by: str,
    ) -> OdooRelationshipScope:
        """Create one validated scope revision and calculate its content hash."""

        return cls(
            scope_id=scope_id,
            version=version,
            decisions=decisions,
            root_models=tuple(sorted(set(root_models))),
            explicit_actions_by_model=tuple(
                sorted(
                    (
                        (str(model), OdooRelationshipCaptureAction(action))
                        for model, action in explicit_actions_by_model
                    ),
                    key=lambda item: item[0],
                )
            ),
            recorded_at=recorded_at,
            recorded_by=recorded_by,
            content_hash="",
            _calculate_content_hash=True,
        )

    @property
    def included_models(self) -> frozenset[str]:
        """Return related models needed for transfer or destination matching."""

        return frozenset(
            item.relation_model
            for item in self.decisions
            if item.action in {
                OdooRelationshipCaptureAction.CAPTURE_LINKED,
                OdooRelationshipCaptureAction.PRESERVE_LINKED,
                OdooRelationshipCaptureAction.MATCH_EXISTING,
            }
        )

    @property
    def required_capture_models(self) -> frozenset[str]:
        """Return the one authoritative set requiring source capture plans."""

        return frozenset(self.root_models) | self.included_models

    @property
    def explicit_actions(self) -> dict[str, OdooRelationshipCaptureAction]:
        """Return deliberate model-wide exceptions saved by the data manager."""

        return dict(self.explicit_actions_by_model)

    @property
    def expanding_models(self) -> frozenset[str]:
        """Return related models whose outgoing relationships remain in scope."""

        return frozenset(
            item.relation_model
            for item in self.decisions
            if item.action is OdooRelationshipCaptureAction.CAPTURE_LINKED
        )

    @property
    def reference_models(self) -> frozenset[str]:
        """Return identity-only leaves that must already exist at destination."""

        return frozenset(
            item.relation_model
            for item in self.decisions
            if item.action is OdooRelationshipCaptureAction.MATCH_EXISTING
        )

    @property
    def create_if_missing_models(self) -> frozenset[str]:
        """Return graph leaves reusable or minimally creatable at destination."""

        return frozenset(
            item.relation_model
            for item in self.decisions
            if item.action is OdooRelationshipCaptureAction.PRESERVE_LINKED
        )

    @property
    def leaf_models(self) -> frozenset[str]:
        """Return included models whose optional outgoing graph stays closed."""

        return self.reference_models | self.create_if_missing_models

    def _semantic_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "contract_version": self.contract_version,
            "decisions": [
                item.to_dict(contract_version=self.contract_version)
                for item in self.decisions
            ],
            "scope_id": self.scope_id,
            "version": self.version,
        }
        if self.contract_version >= 2:
            result["root_models"] = list(self.root_models)
        if self.contract_version >= 6:
            result["explicit_actions_by_model"] = [
                [model, action.value]
                for model, action in self.explicit_actions_by_model
            ]
        return result

    def to_json(self) -> str:
        """Serialize the complete scope revision as canonical JSON."""

        return canonical_json(
            {
                **self._semantic_dict(),
                "content_hash": self.content_hash,
                "recorded_at": self.recorded_at.isoformat(),
                "recorded_by": self.recorded_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> OdooRelationshipScope:
        """Restore a scope revision while verifying its exact shape and hash."""

        try:
            payload = json.loads(value)
            if not isinstance(payload, dict):
                raise OdooRelationshipScopeError(
                    "Odoo relationship scope shape is invalid"
                )
            contract_version = int(payload.get("contract_version", 0))
            expected_keys = {
                "content_hash",
                "contract_version",
                "decisions",
                "recorded_at",
                "recorded_by",
                "scope_id",
                "version",
            }
            if contract_version >= 2:
                expected_keys.add("root_models")
            if contract_version >= 6:
                expected_keys.add("explicit_actions_by_model")
            if set(payload) != expected_keys:
                raise OdooRelationshipScopeError(
                    "Odoo relationship scope shape is invalid"
                )
            decisions = payload["decisions"]
            if not isinstance(decisions, list):
                raise OdooRelationshipScopeError(
                    "Odoo relationship scope decisions are invalid"
                )
            return cls(
                scope_id=str(payload["scope_id"]),
                version=int(payload["version"]),
                decisions=tuple(
                    OdooRelationshipScopeDecision.from_dict(
                        item,
                        contract_version=contract_version,
                    )
                    for item in decisions
                ),
                recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
                recorded_by=str(payload["recorded_by"]),
                content_hash=str(payload["content_hash"]),
                root_models=tuple(
                    str(item) for item in payload.get("root_models", ())
                ),
                explicit_actions_by_model=_explicit_actions_from_payload(
                    payload.get("explicit_actions_by_model", ()),
                    contract_version=contract_version,
                ),
                contract_version=contract_version,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, OdooRelationshipScopeError):
                raise
            raise OdooRelationshipScopeError(
                "Odoo relationship scope is invalid"
            ) from error


def _explicit_actions_from_payload(
    value: object,
    *,
    contract_version: int,
) -> tuple[tuple[str, OdooRelationshipCaptureAction], ...]:
    """Decode v6 model overrides without weakening legacy JSON shapes."""

    if contract_version < 6:
        return ()
    if not isinstance(value, list):
        raise OdooRelationshipScopeError(
            "Odoo relationship explicit actions are invalid"
        )
    parsed: list[tuple[str, OdooRelationshipCaptureAction]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise OdooRelationshipScopeError(
                "Odoo relationship explicit actions are invalid"
            )
        parsed.append(
            (str(item[0]), OdooRelationshipCaptureAction(item[1]))
        )
    return tuple(parsed)


__all__ = [
    "ODOO_RELATIONSHIP_SCOPE_CONTRACT_VERSION",
    "OdooRelationshipActionDefaults",
    "OdooRelationshipCaptureAction",
    "OdooRelationshipScope",
    "OdooRelationshipScopeDecision",
    "OdooRelationshipScopeError",
    "OdooRelationshipScopeReview",
    "propose_relationship_action_defaults",
    "recommended_relationship_capture_action",
    "relationship_expansion_suggestions",
    "relationship_expansion_suggestions_for_decisions",
    "relationship_scope_decisions",
    "review_relationship_scope",
]
