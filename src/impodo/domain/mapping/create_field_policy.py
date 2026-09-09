"""Classify Odoo create-field coverage from one captured target contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .contracts import TargetFieldHandling


CREATE_DEFAULT_TYPES = frozenset(
    {
        "boolean",
        "char",
        "date",
        "datetime",
        "float",
        "html",
        "integer",
        "many2one",
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
    company_dependent: bool | None
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


class VerifiedCreateDefaultAction(StrEnum):
    """Decide whether exact target evidence still needs business review."""

    APPLY_AUTOMATICALLY = "APPLY_AUTOMATICALLY"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"


@dataclass(frozen=True, slots=True)
class VerifiedCreateDefaultDecision:
    """Explain how one verified Odoo default may be used for this target."""

    action: VerifiedCreateDefaultAction
    reason: str


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
    if field.computed is True or field.related is True:
        return CreateFieldAssessment(CreateFieldCoverage.ODOO_MANAGED_CONFIRMED)
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
    """Return whether one field can receive bounded ``default_get`` evidence.

    A Many2one default is safe here because Impodo never copies its numeric ID
    into another target. The evidence belongs to this exact Odoo context and
    confirming it means omitting the field so that Odoo applies the default.
    """

    return bool(
        field.required
        and not field.readonly
        and field.type in CREATE_DEFAULT_TYPES
    )


def decide_verified_create_default(
    field: CreateFieldView,
) -> VerifiedCreateDefaultDecision:
    """Classify one usable ``default_get`` value without model-name rules.

    Impodo may automatically omit low-risk target-only fields because the exact
    target will apply its own value. Defaults that select another record, a
    workflow choice, a monetary amount, or an unproven company scope retain a
    deliberate review step.
    """

    if (
        not supports_create_default_capture(field)
        or not field.create_default_present
    ):
        raise ValueError("A verified required Odoo create default is required")
    if field.type == "many2one":
        return VerifiedCreateDefaultDecision(
            VerifiedCreateDefaultAction.REQUIRE_REVIEW,
            "This default selects a linked Odoo record.",
        )
    if field.type == "selection":
        return VerifiedCreateDefaultDecision(
            VerifiedCreateDefaultAction.REQUIRE_REVIEW,
            "This default selects an Odoo workflow choice.",
        )
    if field.type == "monetary":
        return VerifiedCreateDefaultDecision(
            VerifiedCreateDefaultAction.REQUIRE_REVIEW,
            "This default supplies a business amount.",
        )
    if field.company_dependent is True:
        return VerifiedCreateDefaultDecision(
            VerifiedCreateDefaultAction.REQUIRE_REVIEW,
            "This default can differ by Odoo company.",
        )
    if field.company_dependent is None:
        return VerifiedCreateDefaultDecision(
            VerifiedCreateDefaultAction.REQUIRE_REVIEW,
            "Odoo did not prove whether this default is company-specific.",
        )
    return VerifiedCreateDefaultDecision(
        VerifiedCreateDefaultAction.APPLY_AUTOMATICALLY,
        (
            "The exact target supplies this non-company-specific value, so "
            "Impodo can leave the field to Odoo."
        ),
    )
