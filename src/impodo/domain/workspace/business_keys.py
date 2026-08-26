"""Safe, human-readable business-key recommendations for captured Odoo models."""

from __future__ import annotations

from dataclasses import dataclass
import re

from impodo.domain.workspace.reference_keys import (
    COUNTRY_REFERENCE_KEY,
    CURRENCY_REFERENCE_KEY,
    LANGUAGE_REFERENCE_KEY,
)
from impodo.domain.workspace.contracts import SchemaField, SchemaModel


_UNIQUE_DEFINITION = re.compile(
    r"^\s*unique(?:\s+nulls\s+not\s+distinct)?\s*\(([^()]*)\)\s*$",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_SUPPORTED_KEY_TYPES = frozenset(
    {
        "boolean",
        "char",
        "date",
        "datetime",
        "float",
        "integer",
        "many2one",
        "monetary",
        "selection",
        "text",
    }
)


@dataclass(frozen=True, slots=True)
class BusinessKeyRecommendation:
    """One non-binding recommendation that still requires confirmation."""

    model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    title: str
    technical_summary: str
    reason: str
    evidence: str
    description: str
    warning: str = ""


@dataclass(frozen=True, slots=True)
class _CuratedRule:
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    reason: str
    description: str
    warning: str = ""


# Curated rules are exact model contracts, not guesses based on field names.
# Generic standard and custom models are handled through captured UNIQUE rules.
_CURATED_RULES = {
    "res.country": _CuratedRule(
        key_fields=COUNTRY_REFERENCE_KEY.key_fields,
        scope_fields=COUNTRY_REFERENCE_KEY.scope_fields,
        reason=COUNTRY_REFERENCE_KEY.reason,
        description="Unique country code",
    ),
    "res.lang": _CuratedRule(
        key_fields=LANGUAGE_REFERENCE_KEY.key_fields,
        scope_fields=LANGUAGE_REFERENCE_KEY.scope_fields,
        reason=LANGUAGE_REFERENCE_KEY.reason,
        description="Unique language code",
    ),
    "res.currency": _CuratedRule(
        key_fields=CURRENCY_REFERENCE_KEY.key_fields,
        scope_fields=CURRENCY_REFERENCE_KEY.scope_fields,
        reason=CURRENCY_REFERENCE_KEY.reason,
        description="Unique currency code",
    ),
    "product.template": _CuratedRule(
        key_fields=("default_code",),
        scope_fields=(),
        reason="Internal Reference is a common matching field for single-variant products.",
        description="Product internal reference",
        warning=(
            "Odoo allows duplicate references, and multi-variant templates may not "
            "have a template-level reference. Data readiness must prove this choice."
        ),
    ),
    "product.product": _CuratedRule(
        key_fields=("default_code",),
        scope_fields=(),
        reason="Internal Reference is the usual portable identity for product variants.",
        description="Product variant internal reference",
        warning=(
            "Odoo allows duplicate references. Data readiness must prove this choice."
        ),
    ),
}


def recommend_business_key(model: SchemaModel) -> BusinessKeyRecommendation | None:
    """Return one safe recommendation or ``None`` when evidence is ambiguous."""

    fields = {field.name: field for field in model.fields}
    candidates = _constraint_candidates(model, fields)
    curated = _CURATED_RULES.get(model.name)
    if curated is not None and _fields_are_available(curated, fields):
        enforced = any(
            candidate == (curated.key_fields, curated.scope_fields)
            for candidate, _constraint_name, _nullable in candidates
        )
        return _recommendation(
            model,
            fields,
            curated.key_fields,
            curated.scope_fields,
            reason=curated.reason,
            evidence=(
                "Enforced by Odoo"
                if enforced
                else "Common Odoo convention"
            ),
            description=curated.description,
            warning=curated.warning,
        )

    if len(candidates) != 1:
        return None
    (key_fields, scope_fields), _constraint_name, nullable = candidates[0]
    warning = (
        "This Odoo uniqueness rule permits blank values. Data readiness must "
        "confirm every incoming key is populated."
        if nullable
        else ""
    )
    return _recommendation(
        model,
        fields,
        key_fields,
        scope_fields,
        reason="Odoo declares this combination as unique for this model.",
        evidence=(
            "Enforced by Odoo when populated"
            if nullable
            else "Enforced by Odoo"
        ),
        description=_description(fields, key_fields, scope_fields),
        warning=warning,
    )


def describe_business_key(
    model: SchemaModel,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> str:
    """Describe a confirmed or drafted key with labels before technical names."""

    fields = {field.name: field for field in model.fields}
    key = " + ".join(_field_display(fields, name) for name in key_fields)
    if not scope_fields:
        return key
    scope = " + ".join(_field_display(fields, name) for name in scope_fields)
    return f"{key}, within {scope}"


def selectable_business_key_fields(model: SchemaModel) -> tuple[SchemaField, ...]:
    """Return simple matching-field choices while keeping system noise hidden."""

    return tuple(
        field
        for field in model.fields
        if field.name != "id"
        and not field.readonly
        and field.type in _SUPPORTED_KEY_TYPES
    )


def _constraint_candidates(
    model: SchemaModel,
    fields: dict[str, SchemaField],
) -> list[tuple[tuple[tuple[str, ...], tuple[str, ...]], str, bool]]:
    candidates = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for constraint in model.unique_constraints:
        names = _unique_fields(constraint.definition)
        if not names or any(name not in fields for name in names):
            continue
        selected = tuple(fields[name] for name in names)
        if any(
            field.name == "id"
            or field.readonly
            or field.type not in _SUPPORTED_KEY_TYPES
            for field in selected
        ):
            continue
        relational = tuple(
            field.name for field in selected if field.type == "many2one"
        )
        scalar = tuple(
            field.name for field in selected if field.type != "many2one"
        )
        key_fields = scalar if scalar else tuple(field.name for field in selected)
        scope_fields = relational if scalar else ()
        shape = (key_fields, scope_fields)
        if shape in seen:
            continue
        seen.add(shape)
        candidates.append(
            (shape, constraint.name, any(not field.required for field in selected))
        )
    return candidates


def _unique_fields(definition: str) -> tuple[str, ...]:
    match = _UNIQUE_DEFINITION.fullmatch(definition)
    if match is None:
        return ()
    result = []
    for raw_name in match.group(1).split(","):
        name = raw_name.strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('""', '"')
        if _IDENTIFIER.fullmatch(name) is None:
            return ()
        result.append(name)
    return tuple(result) if len(result) == len(set(result)) else ()


def _fields_are_available(
    rule: _CuratedRule,
    fields: dict[str, SchemaField],
) -> bool:
    return all(
        name in fields
        and name != "id"
        and fields[name].type in _SUPPORTED_KEY_TYPES
        for name in (*rule.key_fields, *rule.scope_fields)
    )


def _recommendation(
    model: SchemaModel,
    fields: dict[str, SchemaField],
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
    *,
    reason: str,
    evidence: str,
    description: str,
    warning: str,
) -> BusinessKeyRecommendation:
    title = " + ".join(fields[name].label for name in key_fields)
    if scope_fields:
        title += ", within " + " + ".join(
            fields[name].label for name in scope_fields
        )
    return BusinessKeyRecommendation(
        model=model.name,
        key_fields=key_fields,
        scope_fields=scope_fields,
        title=title,
        technical_summary=describe_business_key(model, key_fields, scope_fields),
        reason=reason,
        evidence=evidence,
        description=description,
        warning=warning,
    )


def _description(
    fields: dict[str, SchemaField],
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> str:
    key = " and ".join(fields[name].label for name in key_fields)
    if not scope_fields:
        return f"Unique {key.casefold()}"
    scope = " and ".join(fields[name].label for name in scope_fields)
    return f"Unique {key.casefold()} within {scope.casefold()}"


def _field_display(fields: dict[str, SchemaField], name: str) -> str:
    field = fields.get(name)
    return f"{field.label} ({name})" if field else name
