"""Safe, human-readable business-key recommendations for captured Odoo models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
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


BUSINESS_KEY_POLICY_VERSION = 1


class BusinessKeyRecommendationBasis(StrEnum):
    """Explain why Impodo may prefill a non-binding matching rule."""

    ODOO_ENFORCED = "ODOO_ENFORCED"
    CURATED_CONVENTION = "CURATED_CONVENTION"


class BusinessKeyRecommendationOutcome(StrEnum):
    """Classify whether one recommendation is safe to prefill."""

    SUGGESTED = "SUGGESTED"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"


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
    basis: BusinessKeyRecommendationBasis
    warning: str = ""


@dataclass(frozen=True, slots=True)
class BusinessKeyRecommendationAssessment:
    """Return the preferred rule and any unresolved enforced alternatives."""

    outcome: BusinessKeyRecommendationOutcome
    preferred: BusinessKeyRecommendation | None
    alternatives: tuple[BusinessKeyRecommendation, ...] = ()


@dataclass(frozen=True, slots=True)
class _CuratedField:
    name: str
    field_type: str
    relation: str | None = None


@dataclass(frozen=True, slots=True)
class _CuratedRule:
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    fields: tuple[_CuratedField, ...]
    reason: str
    description: str
    warning: str = ""


# Curated rules are exact model contracts, not guesses based on field names.
# Generic standard and custom models are handled through captured UNIQUE rules.
_CURATED_RULES = {
    "res.country": _CuratedRule(
        key_fields=COUNTRY_REFERENCE_KEY.key_fields,
        scope_fields=COUNTRY_REFERENCE_KEY.scope_fields,
        fields=(_CuratedField("code", "char"),),
        reason=COUNTRY_REFERENCE_KEY.reason,
        description="Unique country code",
    ),
    "res.lang": _CuratedRule(
        key_fields=LANGUAGE_REFERENCE_KEY.key_fields,
        scope_fields=LANGUAGE_REFERENCE_KEY.scope_fields,
        fields=(_CuratedField("code", "char"),),
        reason=LANGUAGE_REFERENCE_KEY.reason,
        description="Unique language code",
    ),
    "res.currency": _CuratedRule(
        key_fields=CURRENCY_REFERENCE_KEY.key_fields,
        scope_fields=CURRENCY_REFERENCE_KEY.scope_fields,
        fields=(_CuratedField("name", "char"),),
        reason=CURRENCY_REFERENCE_KEY.reason,
        description="Unique currency code",
    ),
    "res.partner": _CuratedRule(
        key_fields=("ref",),
        scope_fields=(),
        fields=(_CuratedField("ref", "char"),),
        reason="Reference is the usual portable identity for contacts and companies.",
        description="Contact reference",
        warning=(
            "Odoo permits blank or duplicate references. Data readiness must prove "
            "that every incoming reference identifies at most one contact."
        ),
    ),
    "res.company": _CuratedRule(
        key_fields=("name",),
        scope_fields=(),
        fields=(_CuratedField("name", "char"),),
        reason="Company name is the usual portable identity when no external company code is available.",
        description="Company name",
        warning=(
            "Odoo does not require company names to be unique. Data readiness must "
            "prove this choice for the connected database."
        ),
    ),
    "product.template": _CuratedRule(
        key_fields=("default_code",),
        scope_fields=(),
        fields=(_CuratedField("default_code", "char"),),
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
        fields=(_CuratedField("default_code", "char"),),
        reason="Internal Reference is the usual portable identity for product variants.",
        description="Product variant internal reference",
        warning=(
            "Odoo allows duplicate references. Data readiness must prove this choice."
        ),
    ),
    "product.category": _CuratedRule(
        key_fields=("name",),
        scope_fields=("parent_id",),
        fields=(
            _CuratedField("name", "char"),
            _CuratedField("parent_id", "many2one", "product.category"),
        ),
        reason="A category name is normally identified within its parent category.",
        description="Category name within parent category",
        warning=(
            "Odoo can permit repeated category names under one parent. Data "
            "readiness must prove that each complete category path is unique."
        ),
    ),
    "uom.uom": _CuratedRule(
        key_fields=("name",),
        scope_fields=("category_id",),
        fields=(
            _CuratedField("name", "char"),
            _CuratedField("category_id", "many2one", "uom.category"),
        ),
        reason="A unit name is normally identified within its unit category.",
        description="Unit name within unit category",
        warning=(
            "Translated or customized unit names may not be portable. Data "
            "readiness must prove the selected values in this database."
        ),
    ),
    "mrp.bom": _CuratedRule(
        key_fields=("code",),
        scope_fields=(),
        fields=(_CuratedField("code", "char"),),
        reason="Reference is the usual portable identity for a bill of materials.",
        description="Bill of materials reference",
        warning=(
            "Odoo permits a blank or repeated reference. Data readiness must prove "
            "that each incoming reference identifies at most one bill of materials."
        ),
    ),
    "mrp.bom.line": _CuratedRule(
        key_fields=("sequence",),
        scope_fields=("bom_id",),
        fields=(
            _CuratedField("sequence", "integer"),
            _CuratedField("bom_id", "many2one", "mrp.bom"),
        ),
        reason="A bill of materials line is normally identified by its line sequence within its parent bill of materials.",
        description="Line sequence within parent bill of materials",
        warning=(
            "Odoo permits repeated line sequences. Data readiness must prove that "
            "each sequence occurs at most once within its parent bill of materials."
        ),
    ),
    "mrp.routing.workcenter": _CuratedRule(
        key_fields=("name",),
        scope_fields=("bom_id",),
        fields=(
            _CuratedField("name", "char"),
            _CuratedField("bom_id", "many2one", "mrp.bom"),
        ),
        reason="An operation is normally identified by its name within its parent bill of materials.",
        description="Operation name within parent bill of materials",
        warning=(
            "Odoo permits repeated operation names. Data readiness must prove that "
            "each name occurs at most once within its parent bill of materials."
        ),
    ),
}


def recommend_business_key(model: SchemaModel) -> BusinessKeyRecommendation | None:
    """Return one safe recommendation or ``None`` when evidence is ambiguous."""

    return assess_business_key_recommendation(model).preferred


def assess_business_key_recommendation(
    model: SchemaModel,
) -> BusinessKeyRecommendationAssessment:
    """Assess captured constraints and reviewed Odoo conventions without guessing."""

    fields = {field.name: field for field in model.fields}
    candidates = _constraint_candidates(model, fields)
    curated = _CURATED_RULES.get(model.name)
    if curated is not None and _fields_are_available(curated, fields):
        enforced = any(
            candidate == (curated.key_fields, curated.scope_fields)
            for candidate, _constraint_name, _nullable in candidates
        )
        recommendation = _recommendation(
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
            basis=(
                BusinessKeyRecommendationBasis.ODOO_ENFORCED
                if enforced
                else BusinessKeyRecommendationBasis.CURATED_CONVENTION
            ),
            warning=curated.warning,
        )
        return BusinessKeyRecommendationAssessment(
            outcome=BusinessKeyRecommendationOutcome.SUGGESTED,
            preferred=recommendation,
        )

    alternatives = tuple(
        _constraint_recommendation(model, fields, candidate)
        for candidate in candidates
    )
    if len(alternatives) > 1:
        return BusinessKeyRecommendationAssessment(
            outcome=BusinessKeyRecommendationOutcome.MULTIPLE_CANDIDATES,
            preferred=None,
            alternatives=alternatives,
        )
    if not alternatives:
        return BusinessKeyRecommendationAssessment(
            outcome=BusinessKeyRecommendationOutcome.NO_SAFE_CANDIDATE,
            preferred=None,
        )
    return BusinessKeyRecommendationAssessment(
        outcome=BusinessKeyRecommendationOutcome.SUGGESTED,
        preferred=alternatives[0],
    )


def _constraint_recommendation(
    model: SchemaModel,
    fields: dict[str, SchemaField],
    candidate: tuple[tuple[tuple[str, ...], tuple[str, ...]], str, bool],
) -> BusinessKeyRecommendation:
    (key_fields, scope_fields), _constraint_name, nullable = candidate
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
        basis=BusinessKeyRecommendationBasis.ODOO_ENFORCED,
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
    expected = {field.name: field for field in rule.fields}
    if set(expected) != set((*rule.key_fields, *rule.scope_fields)):
        return False
    for name, contract in expected.items():
        field = fields.get(name)
        if (
            field is None
            or name == "id"
            or field.readonly
            or field.type not in _SUPPORTED_KEY_TYPES
            or field.type != contract.field_type
            or field.relation != contract.relation
        ):
            return False
    return True


def _recommendation(
    model: SchemaModel,
    fields: dict[str, SchemaField],
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
    *,
    reason: str,
    evidence: str,
    description: str,
    basis: BusinessKeyRecommendationBasis,
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
        basis=basis,
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
