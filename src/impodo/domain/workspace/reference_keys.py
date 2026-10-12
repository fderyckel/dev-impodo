"""Exact portable keys for standard Odoo models used as references."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Iterable, Protocol

from impodo.domain.serialization import content_hash


REFERENCE_POLICY_VERSION = 4


class ReferenceReadPurpose(StrEnum):
    """Name the governed workflow that needs a supporting Odoo reference."""

    MATCH_VALIDATION = "MATCH_VALIDATION"
    MATCH_CHOICES = "MATCH_CHOICES"
    PREFLIGHT = "PREFLIGHT"
    RECIPE_PUBLICATION = "RECIPE_PUBLICATION"
    RECIPE_APPLICATION = "RECIPE_APPLICATION"


class ReferenceEvidenceKind(StrEnum):
    """Explain which evidence authorizes one supporting-model read."""

    CAPTURED_GOVERNED = "CAPTURED_GOVERNED"
    REVIEWED_STANDARD = "REVIEWED_STANDARD"
    BOUNDED_MATCH_PROBE = "BOUNDED_MATCH_PROBE"


class ReferencePolicyDenial(StrEnum):
    """Give rejected reference shapes stable identities without UI wording."""

    RELATION_MISMATCH = "RELATION_MISMATCH"
    IDENTITY_NOT_GOVERNED = "IDENTITY_NOT_GOVERNED"
    MODEL_NOT_REVIEWED = "MODEL_NOT_REVIEWED"
    ODOO_VERSION_MISMATCH = "ODOO_VERSION_MISMATCH"
    FIELD_NOT_ALLOWED = "FIELD_NOT_ALLOWED"
    CAPTURED_METADATA_MISMATCH = "CAPTURED_METADATA_MISMATCH"
    UNBOUNDED_METADATA = "UNBOUNDED_METADATA"
    WRITE_USE_FORBIDDEN = "WRITE_USE_FORBIDDEN"


@dataclass(frozen=True, slots=True)
class StandardReferenceFieldContract:
    """Exact major-version metadata for one reviewed reference field."""

    name: str
    field_type: str
    required: bool
    readonly: bool
    relation_model: str | None = None


class CapturedReferenceFieldView(Protocol):
    """Describe the captured-field attributes needed by the shared policy."""

    name: str
    type: str
    required: bool
    readonly: bool
    relation: str | None


@dataclass(frozen=True, slots=True)
class StandardReferenceKey:
    """Reviewed identity for a standard Odoo model used only as a relation."""

    model: str
    odoo_major_version: int
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    display_field: str
    field_contracts: tuple[StandardReferenceFieldContract, ...]
    description: str
    reason: str

    def field_contract(
        self,
        field_name: str,
    ) -> StandardReferenceFieldContract | None:
        """Return reviewed metadata without guessing from a technical name."""

        return next(
            (item for item in self.field_contracts if item.name == field_name),
            None,
        )


@dataclass(frozen=True, slots=True)
class SupportingReferenceCandidate:
    """A reviewed lookup candidate whose uniqueness must be proved per target.

    Unlike ``StandardReferenceKey``, this contract does not declare a globally
    safe identity.  It only authorizes one bounded Match-data probe.  The
    resulting target-bound lookup snapshot is what governs later validation.
    """

    model: str
    odoo_major_version: int
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    display_field: str
    field_contracts: tuple[StandardReferenceFieldContract, ...]
    description: str
    reason: str
    warning: str


@dataclass(frozen=True, slots=True)
class GovernedReferenceRequest:
    """Describe one exact supporting relationship use without adapter types."""

    parent_model: str
    relationship_field: str
    relationship_type: str
    relationship_model: str | None
    related_model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    requested_fields: tuple[str, ...]
    purpose: ReferenceReadPurpose
    odoo_major_version: int = 19
    governed_key: bool = False
    write_use: bool = False
    all_fields: bool = False
    include_unique_constraints: bool = False


@dataclass(frozen=True, slots=True)
class GovernedReferenceDecision:
    """Return one shared authorization result to every reference consumer."""

    accepted: bool
    evidence_kind: ReferenceEvidenceKind | None
    policy_hash: str
    contract: StandardReferenceKey | None = None
    denial: ReferencePolicyDenial | None = None
    affected_field: str | None = None


COUNTRY_REFERENCE_KEY = StandardReferenceKey(
    model="res.country",
    odoo_major_version=19,
    key_fields=("code",),
    scope_fields=(),
    display_field="name",
    field_contracts=(
        StandardReferenceFieldContract("code", "char", True, False),
        StandardReferenceFieldContract("name", "char", True, False),
    ),
    description="Country code",
    reason="Odoo uses the two-character country code as a stable identity.",
)

LANGUAGE_REFERENCE_KEY = StandardReferenceKey(
    model="res.lang",
    odoo_major_version=19,
    key_fields=("code",),
    scope_fields=(),
    display_field="name",
    field_contracts=(
        StandardReferenceFieldContract("code", "char", True, False),
        StandardReferenceFieldContract("name", "char", True, False),
    ),
    description="Language code",
    reason="Odoo uses the locale code as the stable language identity.",
)

CURRENCY_REFERENCE_KEY = StandardReferenceKey(
    model="res.currency",
    odoo_major_version=19,
    key_fields=("name",),
    scope_fields=(),
    display_field="name",
    field_contracts=(
        StandardReferenceFieldContract("name", "char", True, False),
    ),
    description="Currency code",
    reason="Odoo uses the ISO currency code as the stable currency identity.",
)

ODOO_20_COUNTRY_REFERENCE_KEY = replace(
    COUNTRY_REFERENCE_KEY,
    odoo_major_version=20,
)
ODOO_20_LANGUAGE_REFERENCE_KEY = replace(
    LANGUAGE_REFERENCE_KEY,
    odoo_major_version=20,
)
ODOO_20_CURRENCY_REFERENCE_KEY = replace(
    CURRENCY_REFERENCE_KEY,
    odoo_major_version=20,
)

BOM_REFERENCE_CANDIDATE = SupportingReferenceCandidate(
    model="mrp.bom",
    odoo_major_version=19,
    key_fields=("code",),
    scope_fields=(),
    display_field="code",
    field_contracts=(
        StandardReferenceFieldContract("code", "char", False, False),
    ),
    description="Bill of materials reference",
    reason="Reference is the usual portable identity for a bill of materials.",
    warning=(
        "Odoo permits a blank or repeated reference. The connected destination "
        "must prove that every incoming reference identifies at most one bill "
        "of materials."
    ),
)
PRODUCT_VARIANT_REFERENCE_CANDIDATE = SupportingReferenceCandidate(
    model="product.product",
    odoo_major_version=19,
    key_fields=("default_code",),
    scope_fields=(),
    display_field="default_code",
    field_contracts=(
        StandardReferenceFieldContract("default_code", "char", False, False),
    ),
    description="Product variant internal reference",
    reason="Internal Reference is the usual portable identity for product variants.",
    warning=(
        "Odoo allows duplicate Internal References. The connected destination "
        "must prove that every incoming reference identifies at most one product."
    ),
)


def _candidate_for_major(
    candidate: SupportingReferenceCandidate,
    major: int,
) -> SupportingReferenceCandidate:
    return replace(candidate, odoo_major_version=major)


_SUPPORTING_REFERENCE_CANDIDATES_BY_MAJOR = {
    major: {
        item.model: _candidate_for_major(item, major)
        for item in (
            BOM_REFERENCE_CANDIDATE,
            PRODUCT_VARIANT_REFERENCE_CANDIDATE,
        )
    }
    for major in (19, 20)
}


# This allowlist is intentionally narrower than general business-key
# recommendations. Each entry must be stable and unambiguous without caveats.
_STANDARD_REFERENCE_KEYS_BY_MAJOR = {
    19: {
        item.model: item
        for item in (
            COUNTRY_REFERENCE_KEY,
            LANGUAGE_REFERENCE_KEY,
            CURRENCY_REFERENCE_KEY,
        )
    },
    20: {
        item.model: item
        for item in (
            ODOO_20_COUNTRY_REFERENCE_KEY,
            ODOO_20_LANGUAGE_REFERENCE_KEY,
            ODOO_20_CURRENCY_REFERENCE_KEY,
        )
    },
}


def _reference_policy_hash(
    references: dict[str, StandardReferenceKey],
    candidates: dict[str, SupportingReferenceCandidate],
) -> str:
    return content_hash({
        "contract": "governed-reference-policy",
        "version": REFERENCE_POLICY_VERSION,
        "bounded_match_probe": {
            "purpose": ReferenceReadPurpose.MATCH_CHOICES,
            "relationship_type": "many2one",
            "identities": (
                {
                    "key_fields": ("name",),
                    "scope_fields": (),
                    "requested_fields": ("name",),
                },
                {
                    "key_fields": ("name",),
                    "scope_fields": ("company_id",),
                    "requested_fields": ("name", "company_id"),
                    "company_relation": "res.company",
                },
            ),
            "reviewed_candidates": [
                {
                    "model": item.model,
                    "key_fields": item.key_fields,
                    "scope_fields": item.scope_fields,
                    "display_field": item.display_field,
                    "field_contracts": [
                        {
                            "name": field.name,
                            "field_type": field.field_type,
                            "relation_model": field.relation_model,
                        }
                        for field in item.field_contracts
                    ],
                }
                for item in sorted(
                    candidates.values(),
                    key=lambda candidate: candidate.model,
                )
            ],
        },
        "references": [
            {
                "model": item.model,
                "odoo_major_version": item.odoo_major_version,
                "key_fields": item.key_fields,
                "scope_fields": item.scope_fields,
                "display_field": item.display_field,
                "field_contracts": [
                    {
                        "name": field.name,
                        "field_type": field.field_type,
                        "required": field.required,
                        "readonly": field.readonly,
                        "relation_model": field.relation_model,
                    }
                    for field in item.field_contracts
                ],
            }
            for item in sorted(
                references.values(),
                key=lambda reference: reference.model,
            )
        ],
    })


REFERENCE_POLICY_HASHES = {
    major: _reference_policy_hash(
        references,
        _SUPPORTING_REFERENCE_CANDIDATES_BY_MAJOR[major],
    )
    for major, references in _STANDARD_REFERENCE_KEYS_BY_MAJOR.items()
}
# Compatibility alias for existing Odoo 19 Recipes and evidence.
REFERENCE_POLICY_HASH = REFERENCE_POLICY_HASHES[19]
PREVIOUS_REFERENCE_POLICY_HASH = (
    "sha256:984830d24799fff9c59d53e13db6dc1b7221c2cec6e9fc5a758cdf9e0c12a12b"
)
PREVIOUS_REFERENCE_POLICY_HASHES = frozenset(
    {
        PREVIOUS_REFERENCE_POLICY_HASH,
        "sha256:b0f241e8d0b5a302799ac4a8db0673123fb3d4d6523ed6e3d6dede30bfc359b8",
        "sha256:462e13bfba360a4c4ddc1124a8ed3426fad99755bb6d62f98305d6ecf1fe5a11",
    }
)
READABLE_REFERENCE_POLICY_HASHES = frozenset(
    {*REFERENCE_POLICY_HASHES.values(), *PREVIOUS_REFERENCE_POLICY_HASHES}
)


def reference_policy_hash(odoo_major_version: int) -> str | None:
    """Return the reviewed policy identity for one Odoo major."""

    return REFERENCE_POLICY_HASHES.get(odoo_major_version)


def authorize_supporting_match_probe(
    request: GovernedReferenceRequest,
    *,
    captured_fields: tuple[StandardReferenceFieldContract, ...] | None = None,
) -> GovernedReferenceDecision:
    """Authorize one bounded, target-bound Match-data identity probe.

    This is deliberately narrower than normal reference authorization.  It
    lets Match data inspect either the generic ``name`` fallback or an exact
    reviewed supporting candidate on the related model named by a captured
    parent field. The returned field types must be verified before any choices
    become evidence.
    """

    name_only = request.scope_fields == () and request.requested_fields == ("name",)
    name_within_company = (
        request.scope_fields == ("company_id",)
        and request.requested_fields == ("name", "company_id")
    )
    current_policy_hash = reference_policy_hash(request.odoo_major_version)
    candidate = supporting_reference_candidate(
        request.related_model,
        odoo_major_version=request.odoo_major_version,
    )
    candidate_identity = bool(
        candidate is not None
        and candidate.key_fields == request.key_fields
        and candidate.scope_fields == request.scope_fields
        and set(request.requested_fields)
        == {
            *candidate.key_fields,
            *candidate.scope_fields,
            candidate.display_field,
        }
    )
    accepted = bool(
        current_policy_hash is not None
        and request.purpose is ReferenceReadPurpose.MATCH_CHOICES
        and request.relationship_type == "many2one"
        and request.relationship_model == request.related_model
        and bool(request.parent_model)
        and bool(request.relationship_field)
        and (
            (
                candidate is None
                and request.key_fields == ("name",)
                and (name_only or name_within_company)
            )
            or candidate_identity
        )
        and request.governed_key
        and not request.write_use
        and not request.all_fields
        and not request.include_unique_constraints
    )
    if accepted and captured_fields is not None:
        fields = {field.name: field for field in captured_fields}
        if candidate_identity:
            assert candidate is not None
            expected_fields = {
                field.name: field for field in candidate.field_contracts
            }
            accepted = all(
                (actual := fields.get(field_name)) is not None
                and actual.field_type == expected.field_type
                and actual.relation_model == expected.relation_model
                for field_name, expected in expected_fields.items()
            )
        else:
            name = fields.get("name")
            accepted = name is not None and name.field_type in {
                "char", "text", "selection"
            }
        if accepted and name_within_company and not candidate_identity:
            company = fields.get("company_id")
            accepted = bool(
                company is not None
                and company.field_type == "many2one"
                and company.relation_model == "res.company"
            )
    return GovernedReferenceDecision(
        accepted=accepted,
        evidence_kind=(
            ReferenceEvidenceKind.BOUNDED_MATCH_PROBE if accepted else None
        ),
        policy_hash=current_policy_hash or REFERENCE_POLICY_HASH,
        denial=(
            None
            if accepted
            else (
                ReferencePolicyDenial.ODOO_VERSION_MISMATCH
                if current_policy_hash is None
                else ReferencePolicyDenial.MODEL_NOT_REVIEWED
            )
        ),
    )


def standard_reference_key(
    model: str,
    *,
    odoo_major_version: int = 19,
) -> StandardReferenceKey | None:
    """Return an exact reviewed reference rule, never a field-name guess."""

    return _STANDARD_REFERENCE_KEYS_BY_MAJOR.get(odoo_major_version, {}).get(model)


def supporting_reference_candidate(
    model: str,
    *,
    odoo_major_version: int = 19,
) -> SupportingReferenceCandidate | None:
    """Return a bounded candidate, never a globally trusted reference key."""

    return _SUPPORTING_REFERENCE_CANDIDATES_BY_MAJOR.get(
        odoo_major_version,
        {},
    ).get(model)


def captured_reference_field_contracts(
    fields: Iterable[CapturedReferenceFieldView],
) -> tuple[StandardReferenceFieldContract, ...]:
    """Adapt immutable captured schema fields without adding policy decisions."""

    return tuple(
        StandardReferenceFieldContract(
            name=field.name,
            field_type=field.type,
            required=field.required,
            readonly=field.readonly,
            relation_model=field.relation,
        )
        for field in fields
    )


def authorize_governed_reference(
    request: GovernedReferenceRequest,
    *,
    captured_fields: tuple[StandardReferenceFieldContract, ...] | None,
) -> GovernedReferenceDecision:
    """Authorize one exact reference shape and fail closed for every mismatch.

    ``captured_fields`` is ``None`` only when the related model is outside the
    captured project schema. Callers adapt their own immutable schema field
    types into this small domain contract before requesting a decision.
    """

    current_policy_hash = reference_policy_hash(request.odoo_major_version)

    def reject(
        denial: ReferencePolicyDenial,
        *,
        affected_field: str | None = None,
        contract: StandardReferenceKey | None = None,
    ) -> GovernedReferenceDecision:
        return GovernedReferenceDecision(
            accepted=False,
            evidence_kind=None,
            policy_hash=current_policy_hash or REFERENCE_POLICY_HASH,
            contract=contract,
            denial=denial,
            affected_field=affected_field,
        )

    if current_policy_hash is None:
        return reject(ReferencePolicyDenial.ODOO_VERSION_MISMATCH)
    if (
        not request.parent_model
        or not request.relationship_field
        or request.relationship_type not in {"many2one", "many2many"}
        or request.relationship_model != request.related_model
    ):
        return reject(ReferencePolicyDenial.RELATION_MISMATCH)
    if request.write_use:
        return reject(ReferencePolicyDenial.WRITE_USE_FORBIDDEN)
    if request.all_fields or request.include_unique_constraints:
        return reject(ReferencePolicyDenial.UNBOUNDED_METADATA)

    standard = standard_reference_key(
        request.related_model,
        odoo_major_version=request.odoo_major_version,
    )
    standard_identity = bool(
        standard is not None
        and standard.key_fields == request.key_fields
        and standard.scope_fields == request.scope_fields
    )
    if captured_fields is None:
        if standard is None:
            return reject(ReferencePolicyDenial.MODEL_NOT_REVIEWED)
        if request.odoo_major_version != standard.odoo_major_version:
            return reject(
                ReferencePolicyDenial.ODOO_VERSION_MISMATCH,
                contract=standard,
            )
        if not standard_identity:
            return reject(
                ReferencePolicyDenial.IDENTITY_NOT_GOVERNED,
                contract=standard,
            )
        allowed_fields = {
            *standard.key_fields,
            *standard.scope_fields,
            standard.display_field,
        }
        disallowed = next(
            (
                field
                for field in request.requested_fields
                if field not in allowed_fields
            ),
            None,
        )
        if disallowed is not None:
            return reject(
                ReferencePolicyDenial.FIELD_NOT_ALLOWED,
                affected_field=disallowed,
                contract=standard,
            )
        return GovernedReferenceDecision(
            accepted=True,
            evidence_kind=ReferenceEvidenceKind.REVIEWED_STANDARD,
            policy_hash=current_policy_hash,
            contract=standard,
        )

    captured_by_name = {field.name: field for field in captured_fields}
    missing = next(
        (
            field
            for field in request.requested_fields
            if field not in captured_by_name
        ),
        None,
    )
    if missing is not None:
        return reject(
            ReferencePolicyDenial.FIELD_NOT_ALLOWED,
            affected_field=missing,
            contract=standard,
        )

    if standard_identity:
        assert standard is not None
        if request.odoo_major_version != standard.odoo_major_version:
            return reject(
                ReferencePolicyDenial.ODOO_VERSION_MISMATCH,
                contract=standard,
            )
        for field_name in request.requested_fields:
            expected = standard.field_contract(field_name)
            actual = captured_by_name.get(field_name)
            if expected is None:
                return reject(
                    ReferencePolicyDenial.FIELD_NOT_ALLOWED,
                    affected_field=field_name,
                    contract=standard,
                )
            if actual != expected:
                return reject(
                    ReferencePolicyDenial.CAPTURED_METADATA_MISMATCH,
                    affected_field=field_name,
                    contract=standard,
                )
        return GovernedReferenceDecision(
            accepted=True,
            evidence_kind=ReferenceEvidenceKind.REVIEWED_STANDARD,
            policy_hash=current_policy_hash,
            contract=standard,
        )

    if not request.governed_key:
        return reject(
            ReferencePolicyDenial.IDENTITY_NOT_GOVERNED,
            contract=standard,
        )
    return GovernedReferenceDecision(
        accepted=True,
        evidence_kind=ReferenceEvidenceKind.CAPTURED_GOVERNED,
        policy_hash=current_policy_hash,
        contract=None,
    )
