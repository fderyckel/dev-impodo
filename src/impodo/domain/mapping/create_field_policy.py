"""Classify Odoo create-field coverage from one captured target contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .contracts import TargetFieldHandling


CREATE_DEFAULT_SCALAR_TYPES = frozenset(
    {
        "boolean",
        "char",
        "date",
        "datetime",
        "float",
        "html",
        "integer",
        "monetary",
        "selection",
        "text",
    }
)


class CreateFieldView(Protocol):
    """Minimal field evidence used by workspace and Recipe compatibility."""

    name: str
    type: str
    required: bool
    readonly: bool
    computed: bool | None
    related: bool | None
    create_default_present: bool
    create_default_value: bool | int | float | str | None


class CreateFieldCoverage(StrEnum):
    """One deterministic outcome for an Odoo create payload field."""

    OPTIONAL = "OPTIONAL"
    PROVIDED = "PROVIDED"
    READONLY_IGNORED = "READONLY_IGNORED"
    READONLY_CONFLICT = "READONLY_CONFLICT"
    DEFAULT_AVAILABLE = "DEFAULT_AVAILABLE"
    DEFAULT_CONFIRMED = "DEFAULT_CONFIRMED"
    DEFAULT_UNVERIFIED = "DEFAULT_UNVERIFIED"
    ODOO_MANAGED_CONFIRMED = "ODOO_MANAGED_CONFIRMED"
    ODOO_MANAGED_INVALID = "ODOO_MANAGED_INVALID"
    REQUIRED_VALUE_MISSING = "REQUIRED_VALUE_MISSING"


@dataclass(frozen=True, slots=True)
class CreateFieldAssessment:
    """Return field coverage without deciding how a caller presents it."""

    coverage: CreateFieldCoverage
    default_value: bool | int | float | str | None = None


def evaluate_create_field(
    field: CreateFieldView,
    *,
    provided: bool,
    handling: TargetFieldHandling | None,
) -> CreateFieldAssessment:
    """Apply the shared conservative create-field policy."""

    if field.readonly:
        return CreateFieldAssessment(
            CreateFieldCoverage.READONLY_CONFLICT
            if provided
            else CreateFieldCoverage.READONLY_IGNORED
        )
    if provided:
        return CreateFieldAssessment(CreateFieldCoverage.PROVIDED)
    if not field.required:
        return CreateFieldAssessment(CreateFieldCoverage.OPTIONAL)
    if handling is TargetFieldHandling.ODOO_DEFAULT:
        return CreateFieldAssessment(
            (
                CreateFieldCoverage.DEFAULT_CONFIRMED
                if field.create_default_present
                else CreateFieldCoverage.DEFAULT_UNVERIFIED
            ),
            field.create_default_value,
        )
    if handling is TargetFieldHandling.ODOO_MANAGED:
        return CreateFieldAssessment(
            CreateFieldCoverage.ODOO_MANAGED_CONFIRMED
            if is_odoo_managed_candidate(field)
            else CreateFieldCoverage.ODOO_MANAGED_INVALID
        )
    if field.create_default_present:
        return CreateFieldAssessment(
            CreateFieldCoverage.DEFAULT_AVAILABLE,
            field.create_default_value,
        )
    return CreateFieldAssessment(CreateFieldCoverage.REQUIRED_VALUE_MISSING)


def is_odoo_managed_candidate(field: CreateFieldView) -> bool:
    """Return the narrow existing signal; callers still require confirmation."""

    return bool(
        field.type in {"one2many", "many2many"}
        or field.computed is True
        or field.related is True
    )


def supports_create_default_capture(field: CreateFieldView) -> bool:
    """Return whether one field can receive bounded ``default_get`` evidence."""

    return bool(
        field.required
        and not field.readonly
        and field.type in CREATE_DEFAULT_SCALAR_TYPES
    )
