"""Bind reviewed supporting choices to exact evidence and narrow read policies.

These choices explain business scope. They neither authorize target writes nor
prove source values; Match data and Final review keep those responsibilities.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from enum import StrEnum

from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.schema.governance import BusinessKeyStatus, SchemaGovernance
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaField
from impodo.domain.workspace.reference_keys import (
    GovernedReferenceRequest,
    ReferenceReadPurpose,
    authorize_governed_reference,
    authorize_supporting_match_probe,
    captured_reference_field_contracts,
    reference_policy_hash,
    standard_reference_key,
    supporting_reference_candidate,
)


class SupportingChoiceAction(StrEnum):
    """Closed business choices for one captured relationship edge."""

    INCOMING = "INCOMING"
    REUSE = "REUSE"
    CHECKED_DEFAULT = "CHECKED_DEFAULT"
    ODOO_MANAGED = "ODOO_MANAGED"
    SKIP_OPTIONAL = "SKIP_OPTIONAL"


@dataclass(frozen=True, slots=True, order=True)
class SupportingChoice:
    """Review an exact parent field without carrying values or target IDs."""

    source_model: str
    field_name: str
    action: SupportingChoiceAction
    dataset_id: str = ""
    key_fields: tuple[str, ...] = ()
    scope_fields: tuple[str, ...] = ()
    display_field: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", SupportingChoiceAction(self.action))
        object.__setattr__(self, "key_fields", tuple(self.key_fields))
        object.__setattr__(self, "scope_fields", tuple(self.scope_fields))
        if (
            not self.source_model
            or not self.field_name
            or max(len(self.source_model), len(self.field_name), len(self.dataset_id))
            > 200
        ):
            raise ValueError("Supporting choice identity is invalid")
        if bool(self.dataset_id) != (self.action is SupportingChoiceAction.INCOMING):
            raise ValueError("Only incoming choices require one accepted table")
        if self.action is not SupportingChoiceAction.REUSE and (
            self.key_fields or self.scope_fields or self.display_field
        ):
            raise ValueError("Only reuse choices may retain a supporting read scope")


@dataclass(frozen=True, slots=True)
class SupportingReview:
    """One immutable, idempotent revision of the complete reviewed choice set."""

    workspace_id: str
    version: int
    operation_id: str
    binding_hash: str
    choices: tuple[SupportingChoice, ...]
    recorded_at: str
    actor_issuer: str
    actor_subject: str
    actor_display_name: str
    contract_version: int = 1

    def __post_init__(self) -> None:
        if self.contract_version != 1 or self.version < 1 or len(self.choices) > 1_000:
            raise ValueError("Supporting review contract is invalid")
        ordered = tuple(sorted(self.choices))
        if len({(item.source_model, item.field_name) for item in ordered}) != len(
            ordered
        ):
            raise ValueError("Review each supporting relationship only once")
        object.__setattr__(self, "choices", ordered)

    @property
    def request_hash(self) -> str:
        """Identify retry meaning independently of time and operation identity."""
        return content_hash(
            {
                "binding": self.binding_hash,
                "choices": [asdict(item) for item in self.choices],
            }
        )

    @property
    def content_hash(self) -> str:
        """Bind the saved revision and its verified recording identity."""
        return content_hash(asdict(self))

    def to_json(self) -> str:
        """Serialize complete evidence with a verifiable content hash."""
        return canonical_json({**asdict(self), "content_hash": self.content_hash})

    @classmethod
    def from_json(cls, value: str) -> SupportingReview:
        """Reject damaged evidence instead of guessing a reviewed disposition."""
        raw = json.loads(value)
        expected = raw.pop("content_hash")
        raw["choices"] = tuple(SupportingChoice(**item) for item in raw["choices"])
        result = cls(**raw)
        if result.content_hash != expected:
            raise ValueError("Supporting review evidence hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class SupportingReviewSummary:
    """Bounded committed counts and evidence pointers, without customer detail."""

    workspace_id: str
    version: int
    binding_hash: str
    decision_hash: str
    evidence_hash: str
    choice_count: int
    incoming_count: int
    reuse_count: int

    def __post_init__(self) -> None:
        counts = (self.choice_count, self.incoming_count, self.reuse_count)
        if (
            self.version < 1
            or any(type(value) is not int or value < 0 for value in counts)
            or not (
                self.incoming_count + self.reuse_count <= self.choice_count <= 1_000
            )
        ):
            raise ValueError("Supporting review summary counts are invalid")

    @classmethod
    def from_review(cls, review: SupportingReview) -> SupportingReviewSummary:
        return cls(
            review.workspace_id,
            review.version,
            review.binding_hash,
            review.request_hash,
            review.content_hash,
            len(review.choices),
            sum(
                item.action is SupportingChoiceAction.INCOMING
                for item in review.choices
            ),
            sum(item.action is SupportingChoiceAction.REUSE for item in review.choices),
        )

    def state_for(self, binding_hash: str) -> str:
        """Require current verified inputs before presenting a ready state."""
        return "CURRENT" if self.binding_hash == binding_hash else "STALE"

    def to_json(self) -> str:
        return canonical_json(
            {**asdict(self), "content_hash": content_hash(asdict(self))}
        )

    @classmethod
    def from_json(cls, value: str) -> SupportingReviewSummary:
        raw = json.loads(value)
        expected = raw.pop("content_hash")
        result = cls(**raw)
        if content_hash(asdict(result)) != expected or not (
            result.version > 0
            and 0
            <= result.incoming_count + result.reuse_count
            <= result.choice_count
            <= 1_000
        ):
            raise ValueError("Supporting review summary is invalid")
        return result


def supporting_read_request(
    schema: OdooSchemaCatalog,
    governance: SchemaGovernance | None,
    choice: SupportingChoice,
) -> GovernedReferenceRequest:
    """Derive the exact allowed key, scope, display fields, purpose and version."""
    models = {item.name: item for item in schema.models}
    parent = models.get(choice.source_model)
    field = (
        next((item for item in parent.fields if item.name == choice.field_name), None)
        if parent
        else None
    )
    if (
        field is None
        or not field.relation
        or field.type not in {"many2one", "many2many"}
    ):
        raise ValueError("The supporting relationship is not captured")
    decision = assess_odoo_operation(schema.odoo_version, OdooOperation.COMPARE)
    major = decision.version.major
    if not decision.allowed or major is None or reference_policy_hash(major) is None:
        raise ValueError("Supporting reads require a qualified Odoo version")
    standard = standard_reference_key(field.relation, odoo_major_version=major)
    candidate = supporting_reference_candidate(field.relation, odoo_major_version=major)
    keys = tuple(
        item
        for item in (
            governance.business_keys
            if governance and governance.catalog_hash == schema.content_hash
            else ()
        )
        if item.model == field.relation and item.status is BusinessKeyStatus.CONFIRMED
    )
    if standard is not None:
        key_fields, scope_fields, display = (
            standard.key_fields,
            standard.scope_fields,
            standard.display_field,
        )
    elif len(keys) == 1:
        key_fields, scope_fields = keys[0].key_fields, keys[0].scope_fields
        related = models.get(field.relation)
        display = (
            "name"
            if related and any(item.name == "name" for item in related.fields)
            else key_fields[0]
        )
    elif candidate is not None:
        key_fields, scope_fields, display = (
            candidate.key_fields,
            candidate.scope_fields,
            candidate.display_field,
        )
    else:
        key_fields, scope_fields, display = ("name",), (), "name"
    request = GovernedReferenceRequest(
        parent_model=choice.source_model,
        relationship_field=choice.field_name,
        relationship_type=field.type,
        relationship_model=field.relation,
        related_model=field.relation,
        key_fields=key_fields,
        scope_fields=scope_fields,
        requested_fields=tuple(dict.fromkeys((*key_fields, *scope_fields, display))),
        purpose=ReferenceReadPurpose.MATCH_CHOICES,
        odoo_major_version=major,
        governed_key=bool(
            standard
            or (
                len(keys) == 1
                and key_fields == keys[0].key_fields
                and scope_fields == keys[0].scope_fields
            )
        ),
    )
    related = models.get(field.relation)
    captured = captured_reference_field_contracts(related.fields) if related else None
    authorized = authorize_governed_reference(request, captured_fields=captured)
    probe = authorize_supporting_match_probe(
        replace(request, governed_key=True), captured_fields=captured
    )
    if not authorized.accepted and not probe.accepted:
        raise ValueError(
            "This supporting identity is not authorized; review its matching rule"
        )
    return request


def supporting_read_display_field(
    schema: OdooSchemaCatalog, request: GovernedReferenceRequest
) -> str:
    """Keep the reviewed display projection identical to the shared capture path."""
    for contract in (
        standard_reference_key(
            request.related_model, odoo_major_version=request.odoo_major_version
        ),
        supporting_reference_candidate(
            request.related_model, odoo_major_version=request.odoo_major_version
        ),
    ):
        if (
            contract
            and contract.key_fields == request.key_fields
            and contract.scope_fields == request.scope_fields
        ):
            return contract.display_field
    related = next(
        (model for model in schema.models if model.name == request.related_model), None
    )
    return (
        "name"
        if related and any(field.name == "name" for field in related.fields)
        else request.key_fields[0]
    )


def eligible_supporting_fields(
    schema: OdooSchemaCatalog,
) -> dict[tuple[str, str], SchemaField]:
    """Index captured relationships once, independently of row and value counts."""
    return {
        (model.name, field.name): field
        for model in schema.models
        for field in model.fields
        if field.relation
        and field.type in {"many2one", "many2many", "one2many"}
        and field.exportable is not False
        and field.company_dependent is not True
    }
