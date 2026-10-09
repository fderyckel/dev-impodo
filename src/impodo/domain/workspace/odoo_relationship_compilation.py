"""Compile reviewed Odoo transfer links into canonical relationship meaning.

Stage 2 records why a source relationship is in scope.  Stage 4 supplies the
portable business key and the destination create-or-reuse policy.  Neither
stage can produce a complete relationship resolver alone, so this module joins
their immutable evidence without inventing a second relationship language.

The resulting ``DatasetMapping`` values are a semantic projection.  Protected
source Odoo identifiers are still resolved through the captured relationship
crosswalk; ``source_column_keys`` describe the portable business-key values
produced by that crosswalk, not numeric Odoo IDs.
"""

from __future__ import annotations

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    OdooRelationshipScopeDecision,
)

from .destination_matching import (
    DestinationMatchPlan,
    DestinationModelMatch,
    DestinationRelationshipMatch,
    resolver_origin_for_destination_handling,
)


class OdooRelationshipCompilationError(ValueError):
    """Raised when Stage 2 and Stage 4 relationship evidence cannot align."""


def compile_odoo_relationship_datasets(
    plan: DestinationMatchPlan,
    *,
    relationship_scope: OdooRelationshipScope | None = None,
) -> tuple[DatasetMapping, ...]:
    """Project one destination match plan into canonical dataset mappings.

    Destination matching is the first point where the related dataset's
    reviewed business key is known.  A transfer-capable related model therefore
    becomes ``TARGET_THEN_DATASET``; either no-write destination policy becomes
    ``TARGET_CATALOG``.  When Stage 2 evidence is available, every non-self
    relationship must be backed by its exact saved edge decision or by the
    saved inverse one-to-many edge that selected the child records.
    """

    models_by_dataset = {item.dataset_id: item for item in plan.model_matches}
    if len(models_by_dataset) != len(plan.model_matches):
        raise OdooRelationshipCompilationError(
            "Destination matching contains duplicate Odoo datasets"
        )
    decisions = (
        {item.identity: item for item in relationship_scope.decisions}
        if relationship_scope is not None
        else {}
    )
    relationships_by_owner: dict[str, list[RelationshipMapping]] = {
        item.dataset_id: [] for item in plan.model_matches
    }
    for relationship in plan.relationship_matches:
        owner = models_by_dataset.get(relationship.dataset_id)
        related = models_by_dataset.get(relationship.related_dataset_id)
        if owner is None or related is None:
            raise OdooRelationshipCompilationError(
                "A destination relationship refers to an unknown Odoo dataset"
            )
        _validate_relationship_identity(relationship, owner, related)
        if relationship_scope is not None:
            decision = _relationship_scope_decision(relationship, decisions)
            if decision is None and relationship.model != relationship.related_model:
                raise OdooRelationshipCompilationError(
                    "Review the source relationship scope for "
                    f"{relationship.model}.{relationship.field_name}"
                )
            if (
                decision is not None
                and decision.action not in {
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                    OdooRelationshipCaptureAction.MATCH_EXISTING,
                }
            ):
                raise OdooRelationshipCompilationError(
                    "The saved source relationship decision does not include "
                    f"{decision.source_model}.{decision.field_name}"
                )
            if (
                decision is not None
                and decision.action
                is OdooRelationshipCaptureAction.MATCH_EXISTING
                and decision.relation_model == related.model
                and related.destination_handling == "transfer"
            ):
                raise OdooRelationshipCompilationError(
                    "The saved source relationship requires existing destination "
                    f"records for {related.model}; transfer is not allowed"
                )
        relationships_by_owner[owner.dataset_id].append(
            _compile_relationship(relationship, related)
        )

    return tuple(
        DatasetMapping(
            dataset_id=item.dataset_id,
            target_model=item.model,
            mode=(
                MappingTargetMode.UPSERT
                if item.destination_handling == "transfer"
                else MappingTargetMode.REFERENCE
            ),
            source_identity_column_keys=item.source_column_keys,
            relationships=tuple(
                sorted(
                    relationships_by_owner[item.dataset_id],
                    key=lambda relation: relation.target_field,
                )
            ),
        )
        for item in sorted(plan.model_matches, key=lambda model: model.dataset_id)
    )


def _compile_relationship(
    relationship: DestinationRelationshipMatch,
    related: DestinationModelMatch,
) -> RelationshipMapping:
    origin = resolver_origin_for_destination_handling(
        related.destination_handling
    )
    if (
        origin is ResolverOrigin.TARGET_CATALOG
        and relationship.incoming_link_count
    ):
        raise OdooRelationshipCompilationError(
            "A destination-only relationship cannot require an incoming record"
        )
    if relationship.kind == "many2many" and len(related.key_fields) != 1:
        raise OdooRelationshipCompilationError(
            "A many-to-many Odoo relationship currently requires one reviewed "
            f"business-key field for {related.model}"
        )
    key_mappings = tuple(
        ReferenceKeyMapping(
            source_column_key=source_column_key,
            target_field=target_field,
        )
        for source_column_key, target_field in zip(
            related.source_column_keys,
            related.key_fields,
            strict=True,
        )
    )
    return RelationshipMapping(
        target_field=relationship.field_name,
        kind=relationship.kind,
        source_column_keys=related.source_column_keys,
        resolver=RelationshipResolver(
            origin=origin,
            dataset_id=(
                related.dataset_id
                if origin is ResolverOrigin.TARGET_THEN_DATASET
                else None
            ),
            model=related.model,
            key_mappings=key_mappings,
        ),
        required=relationship.required,
        required_on_create=relationship.required,
        operation="replace",
        categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
    )


def _relationship_scope_decision(
    relationship: DestinationRelationshipMatch,
    decisions: dict[tuple[str, str], OdooRelationshipScopeDecision],
) -> OdooRelationshipScopeDecision | None:
    direct = decisions.get((relationship.model, relationship.field_name))
    if direct is not None:
        if direct.relation_model != relationship.related_model:
            raise OdooRelationshipCompilationError(
                "The saved source relationship model changed for "
                f"{relationship.model}.{relationship.field_name}"
            )
        return direct
    if relationship.inverse_field is None:
        return None
    inverse = decisions.get(
        (relationship.related_model, relationship.inverse_field)
    )
    if inverse is not None and inverse.relation_model != relationship.model:
        raise OdooRelationshipCompilationError(
            "The saved inverse source relationship model changed for "
            f"{relationship.related_model}.{relationship.inverse_field}"
        )
    return inverse


def _validate_relationship_identity(
    relationship: DestinationRelationshipMatch,
    owner: DestinationModelMatch,
    related: DestinationModelMatch,
) -> None:
    if (
        relationship.model != owner.model
        or relationship.dataset_name != owner.dataset_name
        or relationship.related_model != related.model
        or relationship.related_dataset_name != related.dataset_name
        or relationship.related_key_fields != related.key_fields
    ):
        raise OdooRelationshipCompilationError(
            "Destination relationship identity does not match its Odoo datasets"
        )


__all__ = [
    "OdooRelationshipCompilationError",
    "compile_odoo_relationship_datasets",
]
