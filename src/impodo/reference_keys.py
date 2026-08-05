"""Exact portable keys for standard Odoo models used as references."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StandardReferenceKey:
    """Reviewed identity for a standard Odoo model used only as a relation."""

    model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    display_field: str
    description: str
    reason: str


COUNTRY_REFERENCE_KEY = StandardReferenceKey(
    model="res.country",
    key_fields=("code",),
    scope_fields=(),
    display_field="name",
    description="Country code",
    reason="Odoo uses the two-character country code as a stable identity.",
)

LANGUAGE_REFERENCE_KEY = StandardReferenceKey(
    model="res.lang",
    key_fields=("code",),
    scope_fields=(),
    display_field="name",
    description="Language code",
    reason="Odoo uses the locale code as the stable language identity.",
)

CURRENCY_REFERENCE_KEY = StandardReferenceKey(
    model="res.currency",
    key_fields=("name",),
    scope_fields=(),
    display_field="name",
    description="Currency code",
    reason="Odoo uses the ISO currency code as the stable currency identity.",
)


# This allowlist is intentionally narrower than general business-key
# recommendations. Each entry must be stable and unambiguous without caveats.
_STANDARD_REFERENCE_KEYS = {
    item.model: item
    for item in (
        COUNTRY_REFERENCE_KEY,
        LANGUAGE_REFERENCE_KEY,
        CURRENCY_REFERENCE_KEY,
    )
}


def standard_reference_key(model: str) -> StandardReferenceKey | None:
    """Return an exact reviewed reference rule, never a field-name guess."""

    return _STANDARD_REFERENCE_KEYS.get(model)


def matches_standard_reference_key(
    model: str,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> bool:
    """Whether the supplied identity exactly matches a reviewed reference rule."""

    rule = standard_reference_key(model)
    return bool(
        rule is not None
        and rule.key_fields == key_fields
        and rule.scope_fields == scope_fields
    )
