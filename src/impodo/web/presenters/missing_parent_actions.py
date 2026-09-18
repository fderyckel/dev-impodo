"""Find the source column that can safely draft a missing-parent exclusion."""

from __future__ import annotations

from impodo.domain.mapping.contracts import DatasetMapping, RelationshipValueSource
from impodo.domain.preflight.missing_parents import MissingParentGroup


def missing_parent_source_key(
    mapping: DatasetMapping, group: MissingParentGroup
) -> str | None:
    """Return a raw source key only for a direct, unchanged single-field link."""

    for relation in mapping.relationships:
        if (
            relation.target_field == group.field
            and relation.kind == "many2one"
            and relation.value_source is RelationshipValueSource.SOURCE
            and len(relation.source_column_keys) == 1
            and not relation.resolver.value_mappings
            and len(relation.resolver.key_mappings) == 1
            and relation.resolver.key_mappings[0].target_field == group.key_field
            and relation.resolver.key_mappings[0].source_column_key
            == relation.source_column_keys[0]
            and not relation.resolver.scope_mappings
        ):
            return relation.source_column_keys[0]
    return None
