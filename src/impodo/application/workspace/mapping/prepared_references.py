"""Prepare narrow matching projections before bounded destination lookups.

The inputs retain original source keys. Each calculated value belongs to one
matching component and cannot replace another component's source evidence.
"""

from collections.abc import Iterable

from impodo.domain.mapping.contracts import DatasetMapping, ReferenceKeyMapping
from impodo.domain.mapping.reference_preparation import (
    ReferencePreparationError,
    preparation_source_keys,
    prepare_reference_value,
)
from impodo.domain.workspace.errors import WorkspaceError


def reference_projection_columns(
    columns: tuple[str, ...],
    mappings: tuple[ReferenceKeyMapping | None, ...],
) -> tuple[str, ...]:
    """Project each required source column once in deterministic order."""
    return tuple(
        dict.fromkeys(
            (
                *columns,
                *(
                    key
                    for mapping in mappings
                    if mapping is not None
                    for key in preparation_source_keys(mapping)
                ),
            )
        )
    )


def prepared_reference_rows(
    columns: tuple[str, ...],
    projected_columns: tuple[str, ...],
    rows: Iterable[tuple[str | None, ...]],
    mappings: tuple[ReferenceKeyMapping | None, ...],
    *, blank_groups: tuple[tuple[int, int, str], ...] = (),
) -> tuple[tuple[str | None, ...], ...]:
    """Return exact component values; reject invalid or partial references.

    Optional all-empty references stay empty. An invalid partial reference must
    never become an optional blank, and no partial reference is sent to Odoo.
    """
    if len(mappings) != len(columns):
        raise ValueError("Reference preparations must match their input components")
    dependencies = tuple(
        preparation_source_keys(mapping) if mapping is not None else ()
        for mapping in mappings
    )
    calculated: dict[tuple[int, tuple[str | None, ...]], str | None] = {}
    result: list[tuple[str | None, ...]] = []
    for row in rows:
        values = dict(zip(projected_columns, row, strict=True))
        prepared: list[str | None] = []
        for index, (column, mapping) in enumerate(zip(columns, mappings, strict=True)):
            if any(start <= index < start + width and (
                values.get(blank_key) is None or str(values.get(blank_key)).strip() == ""
            ) for start, width, blank_key in blank_groups):
                prepared.append(None)
                continue
            if mapping is None or mapping.preparation is None:
                prepared.append(values[column])
                continue
            identity = (index, tuple(values.get(key) for key in dependencies[index]))
            if identity in calculated:
                prepared.append(calculated[identity])
                continue
            try:
                value = prepare_reference_value(mapping, values)
            except ReferencePreparationError as error:
                raise WorkspaceError(
                    "A calculated linked reference is incomplete or invalid. "
                    "Check the source values and formula for this field."
                ) from error
            normalized = None if value is None else str(value).strip() or None
            calculated[identity] = normalized
            prepared.append(normalized)
        result.append(tuple(prepared))
    return tuple(result)


def resolver_preparations(
    columns: tuple[str, ...],
    mappings: tuple[ReferenceKeyMapping, ...],
) -> tuple[ReferenceKeyMapping | None, ...]:
    """Align independently prepared resolver components with their source inputs."""
    if len(mappings) == len(columns) and all(
        mapping.source_column_key == key
        for key, mapping in zip(columns, mappings, strict=True)
    ):
        return tuple(
            mapping if mapping.preparation is not None else None for mapping in mappings
        )
    by_source = {
        mapping.source_column_key: mapping
        for mapping in mappings
        if mapping.preparation is not None
    }
    return tuple(by_source.get(key) for key in columns)


def prepared_check_columns(
    dataset: DatasetMapping, *, include_identity: bool
) -> tuple[str, ...]:
    """Plan one shared narrow scan only when a check prepares references."""
    identities = (
        (*dataset.target_identity, *dataset.target_scope) if include_identity else ()
    )
    resolvers = tuple(
        component.resolver for component in identities if component.resolver is not None
    )
    resolvers += tuple(relationship.resolver for relationship in dataset.relationships)
    references = tuple(
        mapping
        for resolver in resolvers
        for mapping in (*resolver.key_mappings, *resolver.scope_mappings)
    )
    if not any(mapping.preparation is not None for mapping in references):
        return ()
    return tuple(
        dict.fromkeys(
            (
                *(
                    key
                    for component in identities
                    for key in component.source_column_keys
                ),
                *(
                    key
                    for relationship in dataset.relationships
                    for key in relationship.source_column_keys
                ),
                *(
                    key
                    for mapping in references
                    for key in preparation_source_keys(mapping)
                ),
            )
        )
    )
