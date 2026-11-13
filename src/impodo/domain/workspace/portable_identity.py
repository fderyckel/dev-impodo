"""Ephemeral, exact scalar identities for Odoo-to-Odoo transfer rows."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.models import BusinessReference, portable_value
from impodo.domain.workspace.errors import WorkspaceError


def portable_identity(values: Sequence[object]) -> str:
    """Use the legacy key for one field and an unambiguous tuple for several."""

    parts = portable_components(values)
    if not parts or any(not part for part in parts):
        return ""
    return parts[0] if len(parts) == 1 else canonical_json(parts)


def record_identity(values: Mapping[str, object], fields: Sequence[str]) -> str:
    return portable_identity(tuple(values.get(field) for field in fields))


def portable_components(values: Sequence[object]) -> tuple[str, ...]:
    return tuple(
        "" if value is None or value is False else str(value).strip()
        for value in values
    )


def scoped_identity(
    values: Sequence[object], scope: Sequence[BusinessReference] = (),
) -> str:
    """Keep legacy scalar keys and bind scoped keys to portable related values."""

    key = portable_identity(values)
    if not key or not scope:
        return key
    return canonical_json({"identity": key, "scope": portable_value(tuple(scope))})


def scoped_record_identity(
    values: Mapping[str, object], fields: Sequence[str], scope_fields: Sequence[str],
    scope_references: Mapping[int, BusinessReference],
) -> str:
    """Translate instance-bound relation IDs only inside the current target read."""

    references: list[BusinessReference] = []
    for field in scope_fields:
        value = values.get(field)
        if isinstance(value, (tuple, list)) and value:
            value = value[0]
        if type(value) is not int or value not in scope_references:
            return ""
        references.append(scope_references[value])
    return scoped_identity(tuple(values.get(field) for field in fields), references)


def captured_scope_references(
    *, parent_model: str, parent_fields: tuple[str, ...],
    parent_values: Sequence[Sequence[object]], parent_origins: tuple[OdooOriginBatch, ...],
    owner_origins: tuple[OdooOriginBatch, ...], scope_field: str, owner_row_count: int,
    excluded_parent_rows: Sequence[int] = (), excluded_owner_rows: Sequence[int] = (),
) -> tuple[tuple[BusinessReference, ...], ...]:
    """Replace captured parent IDs with their reviewed portable scalar identities."""

    parent_ids: list[int] = []
    for batch in sorted(parent_origins, key=lambda item: item.first_row_ordinal):
        if batch.first_row_ordinal != len(parent_ids) + 1:
            raise WorkspaceError("The captured matching-scope parent rows are incomplete")
        parent_ids.extend(batch.odoo_ids)
    if len(parent_ids) != len(parent_values) or len(set(parent_ids)) != len(parent_ids):
        raise WorkspaceError("The captured matching-scope parent rows are inconsistent")
    excluded_parents = set(excluded_parent_rows)
    keys = [portable_identity(row) for row in parent_values]
    counts = Counter(key for index, key in enumerate(keys, 1) if index not in excluded_parents)
    parents = {
        identifier: BusinessReference(parent_model, portable_components(values), target_fields=parent_fields)
        for index, (identifier, values, key) in enumerate(zip(parent_ids, parent_values, keys, strict=True), 1)
        if index not in excluded_parents and key and counts[key] == 1
    }
    result: list[tuple[BusinessReference, ...]] = []
    excluded_owners = set(excluded_owner_rows)
    for batch in sorted(owner_origins, key=lambda item: item.first_row_ordinal):
        if batch.first_row_ordinal != len(result) + 1:
            raise WorkspaceError("The captured matching-scope relationship rows are incomplete")
        columns = tuple(column for column in batch.relationships if column.field_name == scope_field)
        if len(columns) != 1 or columns[0].kind != "many2one" or columns[0].relation_model != parent_model:
            raise WorkspaceError("The captured matching-scope relationship is unavailable or changed")
        for members in columns[0].values:
            if len(result) + 1 in excluded_owners:
                result.append(())
            elif len(members) != 1 or members[0] not in parents:
                raise WorkspaceError("The matching scope is blank or its captured parent identity is missing or ambiguous")
            else:
                result.append((parents[members[0]],))
    if len(result) != owner_row_count:
        raise WorkspaceError("The captured matching-scope relationship rows are inconsistent")
    return tuple(result)
