"""Build exact batched External ID reads and validate their target bindings."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from impodo.domain.mapping.source_record_identity import valid_external_id
from impodo.domain.odoo.contracts import (
    ConnectorConfigurationError,
    ConnectorIncompleteResultError,
)

MAX_SOURCE_EXTERNAL_IDS = 500


def external_id_domain(external_ids: Sequence[str]) -> list[Any]:
    """Narrow each namespace to its own requested names without a cross product."""

    if not external_ids or len(external_ids) > MAX_SOURCE_EXTERNAL_IDS or any(not valid_external_id(value) for value in external_ids):
        raise ConnectorConfigurationError("Source record External IDs are outside the safe lookup bound")
    groups: dict[str, list[str]] = defaultdict(list)
    for value in sorted(set(external_ids)):
        module, name = value.split(".", 1)
        groups[module].append(name)
    return ["|"] * (len(groups) - 1) + [
        term for module, names in sorted(groups.items())
        for term in ("&", ("module", "=", module), ("name", "in", names))
    ]


def external_id_bindings(model: str, external_ids: Sequence[str], rows: object) -> dict[int, tuple[str, ...]]:
    """Reject foreign, repeated, malformed or wrong-model External ID evidence."""

    if not isinstance(rows, list):
        raise ConnectorIncompleteResultError("Odoo returned invalid source record identities")
    allowed = frozenset(external_ids)
    seen: set[str] = set()
    by_id: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConnectorIncompleteResultError("Odoo returned invalid source record identities")
        external_id = f"{row.get('module')}.{row.get('name')}"
        identifier = row.get("res_id")
        if external_id not in allowed or external_id in seen or row.get("model") != model or type(identifier) is not int or identifier <= 0:
            raise ConnectorIncompleteResultError("An Odoo External ID is duplicated, belongs to another record type, or has an invalid binding. Review the source identity name and existing External IDs.")
        seen.add(external_id)
        by_id[identifier].append(external_id)
    return {identifier: tuple(sorted(names)) for identifier, names in by_id.items()}


def external_lookup_values(domains: Sequence[Sequence[tuple[str, str, Any]]], field: str) -> tuple[str, ...]:
    """Recognise only one exact reviewed External ID per lookup slot."""

    values = []
    for domain in domains:
        if len(domain) != 1 or domain[0][0] != field or domain[0][1] != "=" or not isinstance(domain[0][2], str) or not valid_external_id(domain[0][2]):
            raise ConnectorConfigurationError("Source record lookup requires one exact Odoo External ID")
        values.append(domain[0][2])
    return tuple(values)
