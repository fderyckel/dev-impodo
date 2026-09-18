"""Ephemeral, exact scalar identities for Odoo-to-Odoo transfer rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from impodo.domain.serialization import canonical_json


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
