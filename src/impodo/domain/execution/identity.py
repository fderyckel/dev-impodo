"""Portable identity keys and exact reviewed identity predicates.

These pure contracts keep field order, nested scopes, and canonical values
identical wherever execution and reconciliation resolve the same record.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    canonical_json_text,
    portable_value,
)
from impodo.domain.workspace.errors import WorkspaceError


def portable_key(value: tuple[Any, ...]) -> str:
    """Return the canonical typed key shared by execution and reconciliation."""

    return canonical_json_text(portable_value(value))


def identity_domain(
    identity_fields: tuple[str, ...],
    identity: tuple[Any, ...],
    scope_fields: tuple[str, ...],
    scope: tuple[Any, ...],
) -> tuple[tuple[str, str, Any], ...]:
    """Return ordered exact predicates for a reviewed key and nested scope.

    Scalar normalization preserves the established lookup contract. Adapters
    remain responsible for encoding these predicates in transport requests.
    """

    if (
        not identity_fields
        or len(identity_fields) != len(identity)
        or len(scope_fields) != len(scope)
    ):
        raise WorkspaceError(
            "The Odoo business-key shape is incomplete",
            load_failure_code="LOAD_THE_ODOO_BUSINESS_KEY_SHAPE_IS_INCOMPLETE",
        )
    terms: list[tuple[str, str, Any]] = []
    for field_name, value in zip(
        (*identity_fields, *scope_fields),
        (*identity, *scope),
        strict=True,
    ):
        if isinstance(value, BusinessReference):
            nested = identity_domain(
                value.target_fields, value.key, value.target_scope_fields, value.scope
            )
            terms.extend(
                (f"{field_name}.{name}", operator, scalar)
                for name, operator, scalar in nested
            )
        else:
            terms.append((field_name, "=", _identity_scalar(value)))
    return tuple(terms)


def _identity_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError(
            "A relational business key cannot be used as a scalar",
            load_failure_code="LOAD_A_RELATIONAL_BUSINESS_KEY_CANNOT_BE_USED_AS_A_SCALAR",
        )
    if value is None or type(value) in {str, int, bool, float}:
        return value
    raise WorkspaceError(
        "A prepared value is not supported by the Odoo API",
        load_failure_code="LOAD_A_PREPARED_VALUE_IS_NOT_SUPPORTED_BY_THE_ODOO_API",
    )
