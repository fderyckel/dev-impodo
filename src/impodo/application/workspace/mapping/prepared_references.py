"""Prepare narrow matching projections before bounded destination lookups.

The inputs retain original source keys. Each calculated value belongs to one
matching component and cannot replace another component's source evidence.
"""

from collections import Counter
from collections.abc import Iterable, Mapping

from impodo.domain.mapping.contracts import DatasetMapping, ReferenceKeyMapping
from impodo.domain.mapping.reference_preparation import (
    IncompleteReferenceError,
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
    *,
    blank_groups: tuple[tuple[int, int, str], ...] = (),
    source_column_names: Mapping[str, str] | None = None,
    row_counts: Mapping[tuple[str | None, ...], int] | None = None,
) -> tuple[tuple[str | None, ...], ...]:
    """Return exact component values; reject invalid or partial references.

    Optional all-empty references stay empty. Count partial references by
    included source row, even when lookup projections group identical inputs.
    """
    if len(mappings) != len(columns):
        raise ValueError("Reference preparations must match their input components")
    dependencies = tuple(
        preparation_source_keys(mapping) if mapping is not None else ()
        for mapping in mappings
    )
    calculated: dict[
        tuple[int, tuple[str | None, ...]], str | None | tuple[str, ...]
    ] = {}
    blank_counts: Counter[str] = Counter()
    result: list[tuple[str | None, ...]] = []
    for row in rows:
        values = dict(zip(projected_columns, row, strict=True))
        prepared: list[str | None] = []
        row_blanks: set[str] | None = None
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
            if identity not in calculated:
                calculated[identity] = _calculate_reference(mapping, values)
            prepared_value = calculated[identity]
            if isinstance(prepared_value, tuple):
                if row_blanks is None:
                    row_blanks = set()
                row_blanks.update(prepared_value)
            else:
                prepared.append(prepared_value)
        if row_blanks:
            count = row_counts[row] if row_counts is not None else 1
            blank_counts.update(dict.fromkeys(row_blanks, count))
        elif not blank_counts:
            result.append(tuple(prepared))
    if blank_counts:
        raise WorkspaceError(_blank_reference_message(blank_counts, source_column_names or {}))
    return tuple(result)


def _calculate_reference(
    mapping: ReferenceKeyMapping, values: Mapping[str, str | None],
) -> str | None | tuple[str, ...]:
    try:
        value = prepare_reference_value(mapping, values)
    except IncompleteReferenceError as error:
        return error.blank_source_keys
    except ReferencePreparationError as error:
        raise WorkspaceError(
            "A linked reference could not be calculated. "
            "Check the source values and formula for this field."
        ) from error
    return None if value is None else str(value).strip() or None


def _blank_reference_message(counts: Counter[str], column_names: Mapping[str, str]) -> str:
    details = []
    for key in sorted(counts, key=lambda key: (-counts[key], key))[:3]:
        name = " ".join(column_names.get(key, key).split())
        name = name if len(name) <= 64 else f"{name[:63]}…"
        count = counts[key]
        rows = "row" if count == 1 else "rows"
        details.append(f"“{name}” is blank in {count:,} included {rows}.")
    if len(counts) > 3:
        remaining = len(counts) - 3
        if remaining == 1:
            details.append("One other source column has blanks.")
        else:
            details.append(f"{remaining} other source columns have blanks.")
    return (
        "A linked reference is incomplete. " + " ".join(details) + " "
        "Fill in the missing values or exclude those rows in Rows to use, then check matches again."
    )


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
