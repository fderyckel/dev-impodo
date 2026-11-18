"""Prove saved relationship lineage and contributing original input columns.

This export projection validates authored reference rules but never evaluates
source values. The complete canonical lineage remains an evidence envelope;
only the validated rule dependencies contribute physical cells to the workbook.
"""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    RelationshipMapping,
    RelationshipValueSource,
    ResolverOrigin,
    matched_reference_components,
    relationship_input_columns,
)
from impodo.domain.mapping.reference_preparation import (
    preparation_source_keys,
    validate_reference_preparation,
)


@dataclass(frozen=True, slots=True)
class RelationshipOriginalInputs:
    """Keep complete expected saved lineage separate from its actual input cohort."""

    saved_keys: tuple[str, ...]
    input_keys: tuple[str, ...]


def relationship_original_inputs(
    relationship: RelationshipMapping,
) -> RelationshipOriginalInputs | None:
    """Prove direct inputs, reviewed scoped choices, or valid reference preparation.

    Invalid or unsupported metadata provides no proof. A valid constant-only
    reference has an empty actual cohort. Repeated input occurrences count once,
    in saved-lineage order, without changing canonical preparation evidence.
    """

    resolver = relationship.resolver
    anchors = relationship.source_column_keys
    if relationship.value_source is RelationshipValueSource.CONSTANT_EXISTING:
        return (
            RelationshipOriginalInputs((), ())
            if isinstance(relationship.constant_reference, ConstantBusinessReference)
            and not anchors
            and resolver.origin is ResolverOrigin.TARGET_CATALOG
            and not resolver.key_mappings
            and not resolver.scope_mappings
            and not resolver.value_mappings
            and resolver.dataset_projection_field is None
            else None
        )
    if (
        relationship.value_source is not RelationshipValueSource.SOURCE
        or relationship.kind not in {"many2one", "many2many"}
        or not anchors
        or relationship.constant_reference is not None
        or resolver.dataset_projection_field is not None
    ):
        return None
    components = (*resolver.key_mappings, *resolver.scope_mappings)
    if any(
        not isinstance(component.source_column_key, str)
        or not component.source_column_key.strip()
        or not isinstance(component.target_field, str)
        or not component.target_field.strip()
        for component in components
    ):
        return None
    if resolver.value_mappings and resolver.scope_mappings:
        return _translated_inputs(relationship)
    if relationship_input_columns(relationship) != anchors:
        return None
    prepared = any(component.preparation is not None for component in components)
    if not prepared:
        return _direct_inputs(relationship)
    if (
        relationship.kind != "many2one"
        or resolver.origin
        not in {ResolverOrigin.TARGET_CATALOG, ResolverOrigin.TARGET_THEN_DATASET}
        or not resolver.key_mappings
        or resolver.value_mappings
        or tuple(component.source_column_key for component in components) != anchors
        or len({component.target_field for component in components}) != len(components)
    ):
        return None
    try:
        # The ordinary dependency helper deliberately tolerates malformed drafts;
        # validation must precede it before dependencies authorize portable values.
        for component in components:
            validate_reference_preparation(component)
        dependencies = tuple(
            dict.fromkeys(
                key
                for component in components
                for key in preparation_source_keys(component)
            )
        )
    except (TypeError, ValueError):
        return None
    saved_keys = tuple(dict.fromkeys((*anchors, *dependencies)))
    actual = frozenset(dependencies)
    return RelationshipOriginalInputs(
        saved_keys=saved_keys,
        input_keys=tuple(key for key in saved_keys if key in actual),
    )


def _translated_inputs(
    relationship: RelationshipMapping,
) -> RelationshipOriginalInputs | None:
    """Prove one reviewed choice repeated into saved key and scope components."""

    resolver = relationship.resolver
    keys = relationship.source_column_keys
    components = (*resolver.key_mappings, *resolver.scope_mappings)
    if (
        relationship.kind != "many2one"
        or resolver.origin is not ResolverOrigin.TARGET_CATALOG
        or len(keys) != 1
        or len(resolver.key_mappings) != 1
        or any(
            component.source_column_key != keys[0] or component.preparation is not None
            for component in components
        )
        or len({component.target_field for component in components}) != len(components)
    ):
        return None
    try:
        # Validate only the saved rule shape. No choice is looked up or applied.
        for mapping in resolver.value_mappings:
            matched_reference_components(mapping.target_value, len(components))
    except (TypeError, ValueError):
        return None
    return RelationshipOriginalInputs(keys, keys)


def _direct_inputs(
    relationship: RelationshipMapping,
) -> RelationshipOriginalInputs | None:
    """Prove direct single inputs and exact ordered composite key/scope groups."""

    resolver = relationship.resolver
    keys = relationship.source_column_keys
    components = (*resolver.key_mappings, *resolver.scope_mappings)
    if not resolver.scope_mappings and len(keys) == 1:
        return (
            RelationshipOriginalInputs(keys, keys)
            if all(component.source_column_key == keys[0] for component in components)
            else None
        )
    if (
        relationship.kind != "many2one"
        or not resolver.key_mappings
        or resolver.origin
        not in {ResolverOrigin.TARGET_CATALOG, ResolverOrigin.TARGET_THEN_DATASET}
        or resolver.value_mappings
        or tuple(component.source_column_key for component in components) != keys
        or len(set(keys)) != len(keys)
        or len({component.target_field for component in components}) != len(components)
    ):
        return None
    return RelationshipOriginalInputs(keys, keys)
