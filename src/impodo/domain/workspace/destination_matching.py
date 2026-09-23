"""Destination-aware matching evidence for Odoo-to-Odoo transfers.

The current plan stores portable technical names, aggregate counts, and
one-way snapshot hashes. Raw source values and destination numeric identifiers
remain inside their protected read boundaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field as dataclass_field, replace
from datetime import UTC, datetime
import json
from math import isfinite
import re
from typing import Any
from uuid import UUID, uuid4

from impodo.domain.serialization import canonical_json, content_hash


DESTINATION_MATCH_CONTRACT_VERSION = 8
_SUPPORTED_DESTINATION_MATCH_CONTRACT_VERSIONS = frozenset(
    {1, 2, 3, 4, 5, 6, 7, 8}
)
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


@dataclass(frozen=True, slots=True)
class DestinationCreateFieldDecision:
    """Value-safe handling for one required destination create field."""

    dataset_id: str
    model: str
    model_label: str
    field_name: str
    field_label: str
    field_type: str
    provider_kind: str
    decision_kind: str
    reason: str
    field_contract_hash: str
    value_hash: str
    reviewed: bool = False
    source_field_name: str | None = None
    related_model: str | None = None
    related_identity_fields: tuple[str, ...] = ()
    source_dataset_id: str | None = None
    source_reference_requires_create: bool | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.dataset_id,
                self.model,
                self.model_label,
                self.field_name,
                self.field_label,
                self.field_type,
                self.reason,
            )
        ):
            raise ValueError("Destination create-field decision is incomplete")
        if (
            _TECHNICAL_NAME.fullmatch(self.model) is None
            or _TECHNICAL_NAME.fullmatch(self.field_name) is None
            or self.provider_kind not in {
                "odoo_default", "fixed_value", "source_field",
                "existing_reference", "incoming_reference",
            }
            or self.decision_kind not in {"automatic", "review"}
            or any(
                _HASH.fullmatch(value) is None
                for value in (self.field_contract_hash, self.value_hash)
            )
            or not isinstance(self.reviewed, bool)
            or (self.decision_kind == "automatic" and not self.reviewed)
            or (
                self.provider_kind == "source_field"
                and (
                    self.source_field_name is None
                    or _TECHNICAL_NAME.fullmatch(self.source_field_name) is None
                )
            )
            or (
                self.provider_kind != "source_field"
                and self.source_field_name is not None
            )
            or (
                self.field_type == "many2one"
                and (
                    self.provider_kind not in {
                        "odoo_default", "existing_reference", "incoming_reference"
                    }
                    or self.related_model is None
                    or _TECHNICAL_NAME.fullmatch(self.related_model) is None
                    or (
                        self.provider_kind in {
                            "existing_reference", "incoming_reference"
                        }
                        and not self.related_identity_fields
                    )
                    or any(
                        _TECHNICAL_NAME.fullmatch(name) is None
                        for name in self.related_identity_fields
                    )
                )
            )
            or (
                self.field_type != "many2one"
                and (self.related_model is not None or self.related_identity_fields)
            )
            or (
                self.provider_kind == "incoming_reference"
                and (
                    self.source_dataset_id is None
                    or not self.source_dataset_id.strip()
                )
            )
            or (
                self.provider_kind != "incoming_reference"
                and self.source_dataset_id is not None
            )
            or (
                self.provider_kind == "incoming_reference"
                and not isinstance(self.source_reference_requires_create, bool)
            )
            or (
                self.provider_kind != "incoming_reference"
                and self.source_reference_requires_create is not None
            )
        ):
            raise ValueError("Destination create-field decision is invalid")

    @property
    def key(self) -> tuple[str, str]:
        return self.model, self.field_name


@dataclass(frozen=True, slots=True)
class DestinationCreateReferenceCandidate:
    """One encrypted, uniquely identified destination record choice."""

    choice_hash: str
    related_model: str
    identity_fields: tuple[str, ...]
    identity: tuple[bool | int | float | str, ...]
    display_value: str
    odoo_id: int
    target_binding_hash: str

    def __post_init__(self) -> None:
        if (
            _HASH.fullmatch(self.choice_hash) is None
            or _HASH.fullmatch(self.target_binding_hash) is None
            or _TECHNICAL_NAME.fullmatch(self.related_model) is None
            or not self.identity_fields
            or len(self.identity_fields) != len(self.identity)
            or any(
                _TECHNICAL_NAME.fullmatch(name) is None for name in self.identity_fields
            )
            or any(value is None for value in self.identity)
            or not self.display_value.strip()
            or type(self.odoo_id) is not int
            or self.odoo_id <= 0
        ):
            raise ValueError("Destination create-field reference choice is invalid")


@dataclass(frozen=True, slots=True)
class DestinationCreateIncomingReferenceCandidate:
    """One encrypted source record choice from another selected dataset."""

    choice_hash: str
    source_dataset_id: str
    source_dataset_name: str
    related_model: str
    identity_fields: tuple[str, ...]
    identity: tuple[bool | int | float | str, ...]
    display_value: str
    requires_create: bool
    target_binding_hash: str = ""

    def __post_init__(self) -> None:
        if (
            _HASH.fullmatch(self.choice_hash) is None
            or not self.source_dataset_id.strip()
            or not self.source_dataset_name.strip()
            or _TECHNICAL_NAME.fullmatch(self.related_model) is None
            or not self.identity_fields
            or len(self.identity_fields) != len(self.identity)
            or any(
                _TECHNICAL_NAME.fullmatch(name) is None
                for name in self.identity_fields
            )
            or any(value is None for value in self.identity)
            or not self.display_value.strip()
            or not isinstance(self.requires_create, bool)
            or (self.requires_create and self.target_binding_hash)
            or (
                not self.requires_create
                and _HASH.fullmatch(self.target_binding_hash) is None
            )
        ):
            raise ValueError("Destination create-field incoming choice is invalid")


@dataclass(frozen=True, slots=True)
class DestinationCreateFieldEvidenceValue:
    """Protected provider evidence or one unresolved create-field choice."""

    dataset_id: str
    model: str
    field_name: str
    field_type: str
    value: bool | int | float | str | None
    display_value: str
    field_contract_hash: str
    value_hash: str
    field_label: str = ""
    provider_kind: str = "odoo_default"
    source_field_name: str | None = None
    source_candidates: tuple[tuple[str, str], ...] = ()
    selection: tuple[tuple[str, str], ...] = ()
    reference_candidates: tuple[DestinationCreateReferenceCandidate, ...] = ()
    incoming_reference_candidates: tuple[
        DestinationCreateIncomingReferenceCandidate, ...
    ] = ()
    incoming_reference_choice_hash: str | None = None

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.dataset_id,
                self.model,
                self.field_name,
                self.field_type,
            )
        ):
            raise ValueError("Destination create-field evidence is incomplete")
        if (
            _TECHNICAL_NAME.fullmatch(self.model) is None
            or _TECHNICAL_NAME.fullmatch(self.field_name) is None
            or any(
                _HASH.fullmatch(value) is None
                for value in (self.field_contract_hash, self.value_hash)
            )
            or isinstance(self.value, (dict, list, tuple))
            or self.provider_kind
            not in {
                "unresolved", "odoo_default", "fixed_value", "source_field",
                "existing_reference", "incoming_reference",
            }
            or (
                self.provider_kind in {
                    "odoo_default", "fixed_value", "existing_reference"
                }
                and self.value is None
            )
            or (self.provider_kind == "unresolved" and self.value is not None)
            or (
                self.provider_kind == "incoming_reference"
                and self.value is not None
            )
            or (
                self.provider_kind == "source_field"
                and (
                    self.value is not None
                    or self.source_field_name is None
                    or _TECHNICAL_NAME.fullmatch(self.source_field_name) is None
                )
            )
            or (
                self.provider_kind != "source_field"
                and self.source_field_name is not None
            )
            or self.source_candidates
            != tuple(sorted(set(self.source_candidates), key=lambda item: item[0]))
            or any(
                _TECHNICAL_NAME.fullmatch(name) is None or not label.strip()
                for name, label in self.source_candidates
            )
            or self.selection
            != tuple(sorted(set(self.selection), key=lambda item: item[0]))
            or any(not key.strip() or not label.strip() for key, label in self.selection)
            or self.reference_candidates
            != tuple(
                sorted(
                    set(self.reference_candidates),
                    key=lambda item: item.choice_hash,
                )
            )
            or len({item.choice_hash for item in self.reference_candidates})
            != len(self.reference_candidates)
            or self.incoming_reference_candidates
            != tuple(
                sorted(
                    set(self.incoming_reference_candidates),
                    key=lambda item: item.choice_hash,
                )
            )
            or len(
                {item.choice_hash for item in self.incoming_reference_candidates}
            )
            != len(self.incoming_reference_candidates)
            or (
                self.provider_kind == "incoming_reference"
                and (
                    self.incoming_reference_choice_hash is None
                    or self.incoming_reference_choice_hash
                    not in {
                        item.choice_hash
                        for item in self.incoming_reference_candidates
                    }
                )
            )
            or (
                self.provider_kind != "incoming_reference"
                and self.incoming_reference_choice_hash is not None
            )
        ):
            raise ValueError("Destination create-field evidence is invalid")

    @property
    def key(self) -> tuple[str, str]:
        return self.model, self.field_name


@dataclass(frozen=True, slots=True)
class DestinationCreateFieldEvidence:
    """Encrypted exact defaults bound to one destination matching read."""

    evidence_id: str
    workspace_id: str
    source_selection_hash: str
    source_schema_hash: str
    destination_target_hash: str
    destination_read_principal_hash: str
    destination_read_context_hash: str
    destination_schema_snapshot_hash: str
    values: tuple[DestinationCreateFieldEvidenceValue, ...]
    recorded_at: datetime
    contract_version: int = 1

    def __post_init__(self) -> None:
        try:
            UUID(self.evidence_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("Destination create-field evidence ID is invalid") from error
        if not self.workspace_id.strip() or self.recorded_at.tzinfo is None:
            raise ValueError("Destination create-field evidence provenance is invalid")
        hashes = (
            self.source_selection_hash,
            self.source_schema_hash,
            self.destination_target_hash,
            self.destination_read_principal_hash,
            self.destination_read_context_hash,
            self.destination_schema_snapshot_hash,
        )
        if self.contract_version != 1 or any(
            _HASH.fullmatch(value) is None for value in hashes
        ):
            raise ValueError("Destination create-field evidence binding is invalid")
        if self.values != tuple(sorted(self.values, key=lambda item: item.key)):
            raise ValueError("Destination create-field evidence must be ordered")
        if len({item.key for item in self.values}) != len(self.values):
            raise ValueError("Destination create-field evidence must be unique")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "evidence_id": self.evidence_id,
            "workspace_id": self.workspace_id,
            "source_selection_hash": self.source_selection_hash,
            "source_schema_hash": self.source_schema_hash,
            "destination_target_hash": self.destination_target_hash,
            "destination_read_principal_hash": self.destination_read_principal_hash,
            "destination_read_context_hash": self.destination_read_context_hash,
            "destination_schema_snapshot_hash": self.destination_schema_snapshot_hash,
            "values": [asdict(item) for item in self.values],
            "recorded_at": self.recorded_at.isoformat(),
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "DestinationCreateFieldEvidence":
        try:
            payload = json.loads(value)
            result = cls(
                evidence_id=str(payload["evidence_id"]),
                workspace_id=str(payload["workspace_id"]),
                source_selection_hash=str(payload["source_selection_hash"]),
                source_schema_hash=str(payload["source_schema_hash"]),
                destination_target_hash=str(payload["destination_target_hash"]),
                destination_read_principal_hash=str(
                    payload["destination_read_principal_hash"]
                ),
                destination_read_context_hash=str(
                    payload["destination_read_context_hash"]
                ),
                destination_schema_snapshot_hash=str(
                    payload["destination_schema_snapshot_hash"]
                ),
                values=tuple(
                    DestinationCreateFieldEvidenceValue(
                        dataset_id=str(item["dataset_id"]),
                        model=str(item["model"]),
                        field_name=str(item["field_name"]),
                        field_type=str(item["field_type"]),
                        value=item["value"],
                        display_value=str(item["display_value"]),
                        field_contract_hash=str(item["field_contract_hash"]),
                        value_hash=str(item["value_hash"]),
                        field_label=str(item.get("field_label", item["field_name"])),
                        provider_kind=str(item.get("provider_kind", "odoo_default")),
                        source_field_name=(
                            str(item["source_field_name"])
                            if item.get("source_field_name") is not None
                            else None
                        ),
                        source_candidates=tuple(
                            (str(choice[0]), str(choice[1]))
                            for choice in item.get("source_candidates", ())
                        ),
                        selection=tuple(
                            (str(choice[0]), str(choice[1]))
                            for choice in item.get("selection", ())
                        ),
                        reference_candidates=tuple(
                            DestinationCreateReferenceCandidate(
                                choice_hash=str(choice["choice_hash"]),
                                related_model=str(choice["related_model"]),
                                identity_fields=tuple(choice["identity_fields"]),
                                identity=tuple(choice["identity"]),
                                display_value=str(choice["display_value"]),
                                odoo_id=int(choice["odoo_id"]),
                                target_binding_hash=str(choice["target_binding_hash"]),
                            )
                            for choice in item.get("reference_candidates", ())
                        ),
                        incoming_reference_candidates=tuple(
                            DestinationCreateIncomingReferenceCandidate(
                                choice_hash=str(choice["choice_hash"]),
                                source_dataset_id=str(
                                    choice["source_dataset_id"]
                                ),
                                source_dataset_name=str(
                                    choice["source_dataset_name"]
                                ),
                                related_model=str(choice["related_model"]),
                                identity_fields=tuple(choice["identity_fields"]),
                                identity=tuple(choice["identity"]),
                                display_value=str(choice["display_value"]),
                                requires_create=bool(choice["requires_create"]),
                                target_binding_hash=str(
                                    choice.get("target_binding_hash", "")
                                ),
                            )
                            for choice in item.get(
                                "incoming_reference_candidates", ()
                            )
                        ),
                        incoming_reference_choice_hash=(
                            str(item["incoming_reference_choice_hash"])
                            if item.get("incoming_reference_choice_hash") is not None
                            else None
                        ),
                    )
                    for item in payload["values"]
                ),
                recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored destination create-field evidence is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored destination create-field evidence hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class DestinationModelMatch:
    """Aggregate matching outcome for one frozen Odoo dataset."""

    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    source_column_key: str
    key_field: str
    key_field_label: str
    source_row_count: int
    source_distinct_key_count: int
    source_blank_row_count: int
    source_duplicate_key_count: int
    destination_existing_key_count: int
    destination_duplicate_key_count: int
    destination_create_key_count: int
    destination_key_binding_hash: str
    compatible_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    incompatible_fields: tuple[str, ...]
    destination_limit_reached: bool
    source_column_keys: tuple[str, ...] = ()
    key_fields: tuple[str, ...] = ()
    unresolved_create_fields: tuple[str, ...] = ()
    requires_workflow_handler: bool = False

    def __post_init__(self) -> None:
        text_values = (
            self.dataset_id,
            self.dataset_name,
            self.model,
            self.model_label,
            self.source_column_key,
            self.key_field,
            self.key_field_label,
        )
        if any(not value.strip() for value in text_values):
            raise ValueError("Destination model matching identity is incomplete")
        if not self.source_column_keys or (
            len(self.source_column_keys) == 1
            and self.source_column_keys[0] != self.source_column_key
        ):
            object.__setattr__(self, "source_column_keys", (self.source_column_key,))
        if not self.key_fields or (
            len(self.key_fields) == 1 and self.key_fields[0] != self.key_field
        ):
            object.__setattr__(self, "key_fields", (self.key_field,))
        if (
            not 1 <= len(self.key_fields) <= 3
            or len(self.source_column_keys) != len(self.key_fields)
            or self.source_column_keys[0] != self.source_column_key
            or self.key_fields[0] != self.key_field
            or len(set(self.source_column_keys)) != len(self.source_column_keys)
            or len(set(self.key_fields)) != len(self.key_fields)
            or any(_TECHNICAL_NAME.fullmatch(field) is None for field in self.key_fields)
        ):
            raise ValueError("Destination composite matching identity is invalid")
        counts = (
            self.source_row_count,
            self.source_distinct_key_count,
            self.source_blank_row_count,
            self.source_duplicate_key_count,
            self.destination_existing_key_count,
            self.destination_duplicate_key_count,
            self.destination_create_key_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Destination model matching counts must be nonnegative")
        if (
            self.destination_existing_key_count
            + self.destination_create_key_count
            != self.source_distinct_key_count
        ):
            raise ValueError("Destination model matching totals are inconsistent")
        if _HASH.fullmatch(self.destination_key_binding_hash) is None:
            raise ValueError("Destination key classification binding is invalid")
        if not isinstance(self.requires_workflow_handler, bool):
            raise ValueError("Destination workflow requirement is invalid")
        field_groups = (
            self.compatible_fields,
            self.missing_fields,
            self.incompatible_fields,
            self.unresolved_create_fields,
        )
        if any(group != tuple(sorted(set(group))) for group in field_groups):
            raise ValueError("Destination field results must be sorted and unique")
        if len(set().union(*map(set, field_groups[:3]))) != sum(
            len(group) for group in field_groups[:3]
        ):
            raise ValueError("Destination field results overlap")

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Return deterministic reasons this model cannot advance."""

        reasons: list[str] = []
        if self.source_blank_row_count:
            reasons.append("SOURCE_KEY_BLANK")
        if self.source_duplicate_key_count:
            reasons.append("SOURCE_KEY_DUPLICATE")
        if self.destination_duplicate_key_count:
            reasons.append("DESTINATION_KEY_DUPLICATE")
        if self.destination_limit_reached:
            reasons.append("DESTINATION_MATCH_LIMIT_REACHED")
        return tuple(reasons)

    @property
    def write_blocking_reasons(self) -> tuple[str, ...]:
        """Field issues that matter only when this model will receive writes."""

        reasons = []
        if self.missing_fields:
            reasons.append("DESTINATION_FIELDS_MISSING")
        if self.incompatible_fields:
            reasons.append("DESTINATION_FIELDS_INCOMPATIBLE")
        if self.destination_create_key_count and self.unresolved_create_fields:
            reasons.append("DESTINATION_CREATE_FIELDS_UNRESOLVED")
        if self.requires_workflow_handler:
            reasons.append("DESTINATION_WORKFLOW_HANDLER_REQUIRED")
        return tuple(reasons)

    @property
    def ready(self) -> bool:
        return not self.blocking_reasons


@dataclass(frozen=True, slots=True)
class DestinationRelationshipMatch:
    """Portable resolution evidence for one captured Odoo relationship.

    Numeric Odoo identifiers and business-key values stay outside this plan.
    The plan retains only technical identities, aggregate resolution counts,
    and the operation that a later ordered transfer must perform.
    """

    dataset_id: str
    dataset_name: str
    model: str
    model_label: str
    field_name: str
    field_label: str
    kind: str
    related_dataset_id: str
    related_dataset_name: str
    related_model: str
    related_model_label: str
    related_key_field: str
    operation: str
    inverse_field: str | None
    source_owner_count: int
    source_link_count: int
    source_blank_owner_count: int
    destination_reused_link_count: int
    incoming_link_count: int
    missing_related_record_count: int
    ambiguous_destination_link_count: int
    source_evidence_available: bool
    required: bool
    related_key_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        text_values = (
            self.dataset_id,
            self.dataset_name,
            self.model,
            self.model_label,
            self.field_name,
            self.field_label,
            self.related_dataset_id,
            self.related_dataset_name,
            self.related_model,
            self.related_model_label,
            self.related_key_field,
        )
        if any(not value.strip() for value in text_values):
            raise ValueError("Destination relationship identity is incomplete")
        if not self.related_key_fields:
            object.__setattr__(self, "related_key_fields", (self.related_key_field,))
        if (
            self.related_key_fields[0] != self.related_key_field
            or len(set(self.related_key_fields)) != len(self.related_key_fields)
            or any(_TECHNICAL_NAME.fullmatch(field) is None for field in self.related_key_fields)
        ):
            raise ValueError("Destination relationship composite identity is invalid")
        for value in (
            self.model,
            self.field_name,
            self.related_model,
            self.related_key_field,
        ):
            if _TECHNICAL_NAME.fullmatch(value) is None:
                raise ValueError("Destination relationship identity is invalid")
        if self.kind not in {"many2one", "many2many"}:
            raise ValueError("Destination relationship kind is unsupported")
        expected_operation = "set" if self.kind == "many2one" else "replace"
        if self.operation != expected_operation:
            raise ValueError("Destination relationship operation is invalid")
        if self.inverse_field is not None and (
            _TECHNICAL_NAME.fullmatch(self.inverse_field) is None
        ):
            raise ValueError("Destination relationship inverse is invalid")
        counts = (
            self.source_owner_count,
            self.source_link_count,
            self.source_blank_owner_count,
            self.destination_reused_link_count,
            self.incoming_link_count,
            self.missing_related_record_count,
            self.ambiguous_destination_link_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Destination relationship counts must be nonnegative")
        if self.source_blank_owner_count > self.source_owner_count:
            raise ValueError("Destination relationship owner totals are inconsistent")
        resolved_links = (
            self.destination_reused_link_count
            + self.incoming_link_count
            + self.missing_related_record_count
            + self.ambiguous_destination_link_count
        )
        if self.source_evidence_available and resolved_links != self.source_link_count:
            raise ValueError("Destination relationship link totals are inconsistent")
        if not self.source_evidence_available and any(
            (
                self.source_owner_count,
                self.source_link_count,
                self.source_blank_owner_count,
                self.destination_reused_link_count,
                self.incoming_link_count,
                self.missing_related_record_count,
                self.ambiguous_destination_link_count,
            )
        ):
            raise ValueError("Unavailable relationship evidence must not contain counts")

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.source_evidence_available:
            reasons.append("SOURCE_RELATION_EVIDENCE_MISSING")
        if self.required and self.source_blank_owner_count:
            reasons.append("SOURCE_REQUIRED_RELATION_BLANK")
        if self.missing_related_record_count:
            reasons.append("SOURCE_RELATED_RECORD_MISSING")
        if self.ambiguous_destination_link_count:
            reasons.append("DESTINATION_RELATED_KEY_DUPLICATE")
        return tuple(reasons)

    @property
    def ready(self) -> bool:
        return not self.blocking_reasons


@dataclass(frozen=True, slots=True)
class DestinationMatchPlan:
    """Current Stage 5 decision bound to source, destination, and access."""

    workspace_id: str
    source_selection_hash: str
    source_schema_hash: str
    destination_target_hash: str
    destination_credential_binding_hash: str
    destination_read_principal_hash: str
    destination_read_permission_hash: str
    destination_read_context_hash: str
    destination_schema_snapshot_hash: str
    destination_record_snapshot_hash: str
    model_matches: tuple[DestinationModelMatch, ...]
    recorded_at: datetime
    recorded_by: str
    relationship_matches: tuple[DestinationRelationshipMatch, ...] = ()
    create_field_decisions: tuple[DestinationCreateFieldDecision, ...] = ()
    create_field_evidence_id: str | None = None
    create_field_evidence_hash: str | None = None
    protected_create_field_artifact_hash: str | None = None
    contract_version: int = DESTINATION_MATCH_CONTRACT_VERSION
    create_field_evidence: DestinationCreateFieldEvidence | None = dataclass_field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.contract_version not in _SUPPORTED_DESTINATION_MATCH_CONTRACT_VERSIONS:
            raise ValueError("Destination matching contract version is unsupported")
        if not self.workspace_id.strip() or not self.recorded_by.strip():
            raise ValueError("Destination matching provenance is incomplete")
        hashes = (
            self.source_selection_hash,
            self.source_schema_hash,
            self.destination_target_hash,
            self.destination_credential_binding_hash,
            self.destination_read_principal_hash,
            self.destination_read_permission_hash,
            self.destination_read_context_hash,
            self.destination_schema_snapshot_hash,
            self.destination_record_snapshot_hash,
        )
        if any(_HASH.fullmatch(value) is None for value in hashes):
            raise ValueError("Destination matching contains an invalid evidence hash")
        if self.recorded_at.tzinfo is None:
            raise ValueError("Destination matching time must be timezone-aware")
        if self.model_matches != tuple(
            sorted(self.model_matches, key=lambda item: item.model)
        ):
            raise ValueError("Destination model matches must be model ordered")
        if len({item.model for item in self.model_matches}) != len(
            self.model_matches
        ) or len({item.dataset_id for item in self.model_matches}) != len(
            self.model_matches
        ):
            raise ValueError("Destination model matches must be unique")
        if self.relationship_matches != tuple(
            sorted(
                self.relationship_matches,
                key=lambda item: (item.model, item.field_name),
            )
        ):
            raise ValueError("Destination relationship matches must be ordered")
        if len(
            {(item.model, item.field_name) for item in self.relationship_matches}
        ) != len(self.relationship_matches):
            raise ValueError("Destination relationship matches must be unique")
        if self.create_field_decisions != tuple(
            sorted(self.create_field_decisions, key=lambda item: item.key)
        ):
            raise ValueError("Destination create-field decisions must be ordered")
        if len({item.key for item in self.create_field_decisions}) != len(
            self.create_field_decisions
        ):
            raise ValueError("Destination create-field decisions must be unique")
        references = (
            self.create_field_evidence_id,
            self.create_field_evidence_hash,
            self.protected_create_field_artifact_hash,
        )
        if self.contract_version < 6 and (
            self.create_field_decisions or any(value is not None for value in references)
        ):
            raise ValueError(
                "Legacy destination matching cannot contain create-field decisions"
            )
        if any(value is not None for value in references):
            if references[0] is None or references[1] is None:
                raise ValueError("Destination create-field evidence reference is missing")
            try:
                UUID(references[0])
            except (TypeError, ValueError, AttributeError) as error:
                raise ValueError(
                    "Destination create-field evidence reference is invalid"
                ) from error
            if _HASH.fullmatch(references[1]) is None or (
                references[2] is not None and _HASH.fullmatch(references[2]) is None
            ):
                raise ValueError("Destination create-field evidence hash is invalid")
        elif self.create_field_decisions or self.create_field_evidence is not None:
            raise ValueError("Destination create-field evidence reference is missing")
        if self.create_field_evidence is not None:
            evidence = self.create_field_evidence
            if (
                evidence.evidence_id != self.create_field_evidence_id
                or evidence.content_hash != self.create_field_evidence_hash
                or evidence.workspace_id != self.workspace_id
                or evidence.source_selection_hash != self.source_selection_hash
                or evidence.source_schema_hash != self.source_schema_hash
                or evidence.destination_target_hash != self.destination_target_hash
                or evidence.destination_read_principal_hash
                != self.destination_read_principal_hash
                or evidence.destination_read_context_hash
                != self.destination_read_context_hash
                or evidence.destination_schema_snapshot_hash
                != self.destination_schema_snapshot_hash
                or not {item.key for item in self.create_field_decisions}.issubset(
                    {item.key for item in evidence.values}
                )
            ):
                raise ValueError("Destination create-field evidence does not match the plan")

    @property
    def ready(self) -> bool:
        return (
            self.contract_version == DESTINATION_MATCH_CONTRACT_VERSION
            and bool(self.model_matches)
            and all(item.ready for item in self.model_matches)
            and all(item.ready for item in self.relationship_matches)
        )

    @property
    def pending_create_field_decisions(
        self,
    ) -> tuple[DestinationCreateFieldDecision, ...]:
        return tuple(
            item
            for item in self.create_field_decisions
            if item.decision_kind == "review" and not item.reviewed
        )

    @property
    def create_field_defaults_complete(self) -> bool:
        return not self.pending_create_field_decisions and not any(
            item.destination_create_key_count and item.unresolved_create_fields
            for item in self.model_matches
        )

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "source_selection_hash": self.source_selection_hash,
            "source_schema_hash": self.source_schema_hash,
            "destination_target_hash": self.destination_target_hash,
            "destination_credential_binding_hash": (
                self.destination_credential_binding_hash
            ),
            "destination_read_principal_hash": (
                self.destination_read_principal_hash
            ),
            "destination_read_permission_hash": (
                self.destination_read_permission_hash
            ),
            "destination_read_context_hash": self.destination_read_context_hash,
            "destination_schema_snapshot_hash": (
                self.destination_schema_snapshot_hash
            ),
            "destination_record_snapshot_hash": (
                self.destination_record_snapshot_hash
            ),
            "model_matches": [
                {
                    name: value
                    for name, value in asdict(item).items()
                    if (self.contract_version >= 3 or name != "destination_key_binding_hash")
                    and (
                        self.contract_version >= 4
                        or name not in {"source_column_keys", "key_fields"}
                    )
                    and (
                        self.contract_version >= 5
                        or name not in {
                            "unresolved_create_fields", "requires_workflow_handler"
                        }
                    )
                }
                for item in self.model_matches
            ],
            "recorded_at": self.recorded_at.isoformat(),
            "recorded_by": self.recorded_by,
        }
        if self.contract_version >= 2:
            payload["relationship_matches"] = [
                {
                    name: value for name, value in asdict(item).items()
                    if self.contract_version >= 4 or name != "related_key_fields"
                }
                for item in self.relationship_matches
            ]
        if self.contract_version >= 6:
            payload["create_field_decisions"] = [
                asdict(item) for item in self.create_field_decisions
            ]
            payload["create_field_evidence_id"] = self.create_field_evidence_id
            payload["create_field_evidence_hash"] = self.create_field_evidence_hash
            payload["protected_create_field_artifact_hash"] = (
                self.protected_create_field_artifact_hash
            )
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "DestinationMatchPlan":
        try:
            payload = json.loads(value)
            contract_version = int(payload.get("contract_version"))
            if contract_version not in _SUPPORTED_DESTINATION_MATCH_CONTRACT_VERSIONS:
                raise ValueError("unsupported contract")
            result = cls(
                workspace_id=str(payload["workspace_id"]),
                source_selection_hash=str(payload["source_selection_hash"]),
                source_schema_hash=str(payload["source_schema_hash"]),
                destination_target_hash=str(payload["destination_target_hash"]),
                destination_credential_binding_hash=str(
                    payload["destination_credential_binding_hash"]
                ),
                destination_read_principal_hash=str(
                    payload["destination_read_principal_hash"]
                ),
                destination_read_permission_hash=str(
                    payload["destination_read_permission_hash"]
                ),
                destination_read_context_hash=str(
                    payload["destination_read_context_hash"]
                ),
                destination_schema_snapshot_hash=str(
                    payload["destination_schema_snapshot_hash"]
                ),
                destination_record_snapshot_hash=str(
                    payload["destination_record_snapshot_hash"]
                ),
                model_matches=tuple(
                    DestinationModelMatch(
                        dataset_id=str(item["dataset_id"]),
                        dataset_name=str(item["dataset_name"]),
                        model=str(item["model"]),
                        model_label=str(item["model_label"]),
                        source_column_key=str(item["source_column_key"]),
                        key_field=str(item["key_field"]),
                        key_field_label=str(item["key_field_label"]),
                        source_row_count=int(item["source_row_count"]),
                        source_distinct_key_count=int(
                            item["source_distinct_key_count"]
                        ),
                        source_blank_row_count=int(item["source_blank_row_count"]),
                        source_duplicate_key_count=int(
                            item["source_duplicate_key_count"]
                        ),
                        destination_existing_key_count=int(
                            item["destination_existing_key_count"]
                        ),
                        destination_duplicate_key_count=int(
                            item["destination_duplicate_key_count"]
                        ),
                        destination_create_key_count=int(
                            item["destination_create_key_count"]
                        ),
                        destination_key_binding_hash=str(
                            item.get(
                                "destination_key_binding_hash",
                                payload["destination_record_snapshot_hash"],
                            )
                        ),
                        compatible_fields=tuple(item["compatible_fields"]),
                        missing_fields=tuple(item["missing_fields"]),
                        incompatible_fields=tuple(item["incompatible_fields"]),
                        destination_limit_reached=bool(
                            item["destination_limit_reached"]
                        ),
                        source_column_keys=tuple(item.get("source_column_keys", ())),
                        key_fields=tuple(item.get("key_fields", ())),
                        unresolved_create_fields=tuple(
                            item.get("unresolved_create_fields", ())
                        ),
                        requires_workflow_handler=bool(
                            item.get("requires_workflow_handler", False)
                        ),
                    )
                    for item in payload["model_matches"]
                ),
                recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
                recorded_by=str(payload["recorded_by"]),
                relationship_matches=tuple(
                    DestinationRelationshipMatch(
                        dataset_id=str(item["dataset_id"]),
                        dataset_name=str(item["dataset_name"]),
                        model=str(item["model"]),
                        model_label=str(item["model_label"]),
                        field_name=str(item["field_name"]),
                        field_label=str(item["field_label"]),
                        kind=str(item["kind"]),
                        related_dataset_id=str(item["related_dataset_id"]),
                        related_dataset_name=str(item["related_dataset_name"]),
                        related_model=str(item["related_model"]),
                        related_model_label=str(item["related_model_label"]),
                        related_key_field=str(item["related_key_field"]),
                        operation=str(item["operation"]),
                        inverse_field=(
                            str(item["inverse_field"])
                            if item.get("inverse_field") is not None
                            else None
                        ),
                        source_owner_count=int(item["source_owner_count"]),
                        source_link_count=int(item["source_link_count"]),
                        source_blank_owner_count=int(
                            item["source_blank_owner_count"]
                        ),
                        destination_reused_link_count=int(
                            item["destination_reused_link_count"]
                        ),
                        incoming_link_count=int(item["incoming_link_count"]),
                        missing_related_record_count=int(
                            item["missing_related_record_count"]
                        ),
                        ambiguous_destination_link_count=int(
                            item["ambiguous_destination_link_count"]
                        ),
                        source_evidence_available=bool(
                            item["source_evidence_available"]
                        ),
                        required=bool(item["required"]),
                        related_key_fields=tuple(item.get("related_key_fields", ())),
                    )
                    for item in payload.get("relationship_matches", ())
                ),
                create_field_decisions=tuple(
                    DestinationCreateFieldDecision(
                        dataset_id=str(item["dataset_id"]),
                        model=str(item["model"]),
                        model_label=str(item["model_label"]),
                        field_name=str(item["field_name"]),
                        field_label=str(item["field_label"]),
                        field_type=str(item["field_type"]),
                        provider_kind=str(item["provider_kind"]),
                        decision_kind=str(item["decision_kind"]),
                        reason=str(item["reason"]),
                        field_contract_hash=str(item["field_contract_hash"]),
                        value_hash=str(item["value_hash"]),
                        reviewed=bool(item["reviewed"]),
                        source_field_name=(
                            str(item["source_field_name"])
                            if item.get("source_field_name") is not None
                            else None
                        ),
                        related_model=(
                            str(item["related_model"])
                            if item.get("related_model") is not None
                            else None
                        ),
                        related_identity_fields=tuple(
                            item.get("related_identity_fields", ())
                        ),
                        source_dataset_id=(
                            str(item["source_dataset_id"])
                            if item.get("source_dataset_id") is not None
                            else None
                        ),
                        source_reference_requires_create=(
                            bool(item["source_reference_requires_create"])
                            if item.get("source_reference_requires_create")
                            is not None
                            else None
                        ),
                    )
                    for item in payload.get("create_field_decisions", ())
                ),
                create_field_evidence_id=(
                    str(payload["create_field_evidence_id"])
                    if payload.get("create_field_evidence_id") is not None
                    else None
                ),
                create_field_evidence_hash=(
                    str(payload["create_field_evidence_hash"])
                    if payload.get("create_field_evidence_hash") is not None
                    else None
                ),
                protected_create_field_artifact_hash=(
                    str(payload["protected_create_field_artifact_hash"])
                    if payload.get("protected_create_field_artifact_hash") is not None
                    else None
                ),
                contract_version=contract_version,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored destination matching plan is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored destination matching plan hash is invalid")
        return result


def confirm_destination_create_field_defaults(
    plan: DestinationMatchPlan,
    evidence: DestinationCreateFieldEvidence,
    selected_keys: set[tuple[str, str]],
) -> DestinationMatchPlan:
    """Confirm every pending exact default without copying its value into the plan."""

    if plan.create_field_evidence is not None:
        raise ValueError("Confirm stored destination create-field evidence")
    if (
        plan.create_field_evidence_id != evidence.evidence_id
        or plan.create_field_evidence_hash != evidence.content_hash
        or evidence.workspace_id != plan.workspace_id
        or evidence.source_selection_hash != plan.source_selection_hash
        or evidence.source_schema_hash != plan.source_schema_hash
        or evidence.destination_target_hash != plan.destination_target_hash
        or evidence.destination_read_principal_hash
        != plan.destination_read_principal_hash
        or evidence.destination_read_context_hash != plan.destination_read_context_hash
        or evidence.destination_schema_snapshot_hash
        != plan.destination_schema_snapshot_hash
    ):
        raise ValueError("Destination create-field evidence no longer matches")
    pending = {item.key for item in plan.pending_create_field_decisions}
    if not pending or selected_keys != pending:
        raise ValueError("Confirm every current Odoo default shown for review")
    evidence_by_key = {item.key: item for item in evidence.values}
    for decision in plan.create_field_decisions:
        protected = evidence_by_key.get(decision.key)
        if protected is None or (
            protected.field_contract_hash != decision.field_contract_hash
            or protected.value_hash != decision.value_hash
        ):
            raise ValueError("Destination create-field evidence changed")
    decisions = tuple(
        replace(item, reviewed=True) if item.key in pending else item
        for item in plan.create_field_decisions
    )
    resolved_by_model: dict[str, set[str]] = {}
    for item in decisions:
        if item.reviewed:
            resolved_by_model.setdefault(item.model, set()).add(item.field_name)
    models = tuple(
        replace(
            item,
            unresolved_create_fields=tuple(
                field_name
                for field_name in item.unresolved_create_fields
                if field_name not in resolved_by_model.get(item.model, set())
            ),
        )
        for item in plan.model_matches
    )
    return replace(
        plan,
        model_matches=models,
        create_field_decisions=decisions,
    )


def choose_destination_create_field_provider(
    plan: DestinationMatchPlan,
    evidence: DestinationCreateFieldEvidence,
    *,
    model: str,
    field_name: str,
    provider_kind: str,
    fixed_value: bool | int | float | str | None = None,
    source_field_name: str | None = None,
    reference_choice_hash: str | None = None,
    incoming_reference_choice_hash: str | None = None,
) -> DestinationMatchPlan:
    """Choose one reviewed create-only provider and rotate protected evidence."""

    _require_create_field_evidence(plan, evidence)
    key = (model, field_name)
    current = next((item for item in evidence.values if item.key == key), None)
    match = next((item for item in plan.model_matches if item.model == model), None)
    if (
        current is None
        or match is None
        or not match.destination_create_key_count
        or field_name not in match.unresolved_create_fields
    ):
        raise ValueError("Choose a current required field for new records")
    candidate: DestinationCreateReferenceCandidate | None = None
    incoming_candidate: DestinationCreateIncomingReferenceCandidate | None = None
    if provider_kind == "fixed_value":
        if not _valid_fixed_create_value(current, fixed_value):
            raise ValueError("Enter a valid value for the current destination field")
        source_field_name = None
        protected_value = fixed_value
        display_value = _fixed_display_value(current, fixed_value)
        reason = "Use this reviewed value only when Impodo creates a new record."
    elif provider_kind == "source_field":
        candidate_names = {name for name, _label in current.source_candidates}
        if source_field_name not in candidate_names:
            raise ValueError("Choose a compatible captured source field")
        protected_value = None
        display_value = next(
            label for name, label in current.source_candidates if name == source_field_name
        )
        reason = "Copy this captured source field only when Impodo creates a new record."
    elif provider_kind == "existing_reference":
        candidate = next(
            (
                item for item in current.reference_candidates
                if item.choice_hash == reference_choice_hash
            ),
            None,
        )
        if current.field_type != "many2one" or candidate is None:
            raise ValueError("Choose a current destination record by its business key")
        source_field_name = None
        protected_value = candidate.odoo_id
        display_value = candidate.display_value
        reason = (
            "Use this reviewed destination record only when Impodo creates a new record."
        )
    elif provider_kind == "incoming_reference":
        incoming_candidate = next(
            (
                item for item in current.incoming_reference_candidates
                if item.choice_hash == incoming_reference_choice_hash
            ),
            None,
        )
        if current.field_type != "many2one" or incoming_candidate is None:
            raise ValueError("Choose one current source record by its business key")
        source_field_name = None
        protected_value = None
        display_value = (
            f"{incoming_candidate.source_dataset_name}: "
            f"{incoming_candidate.display_value}"
        )
        reason = (
            "Use this reviewed source record only when Impodo creates a new record."
        )
    else:
        raise ValueError("Choose a supported create-only provider")
    value_hash = content_hash(
        {
            "model": model,
            "field": field_name,
            "type": current.field_type,
            "provider": provider_kind,
            "value": protected_value,
            "source_field": source_field_name,
            "reference_choice": (
                candidate.choice_hash
                if provider_kind == "existing_reference"
                else None
            ),
            "incoming_reference_choice": (
                incoming_candidate.choice_hash
                if provider_kind == "incoming_reference"
                else None
            ),
        }
    )
    decision = DestinationCreateFieldDecision(
        dataset_id=current.dataset_id,
        model=model,
        model_label=match.model_label,
        field_name=field_name,
        field_label=current.field_label or field_name,
        field_type=current.field_type,
        provider_kind=provider_kind,
        decision_kind="review",
        reason=reason,
        field_contract_hash=current.field_contract_hash,
        value_hash=value_hash,
        reviewed=True,
        source_field_name=source_field_name,
        related_model=(
            (
                candidate.related_model
                if provider_kind == "existing_reference"
                else incoming_candidate.related_model
            )
            if provider_kind in {"existing_reference", "incoming_reference"}
            else None
        ),
        related_identity_fields=(
            (
                candidate.identity_fields
                if provider_kind == "existing_reference"
                else incoming_candidate.identity_fields
            )
            if provider_kind in {"existing_reference", "incoming_reference"}
            else ()
        ),
        source_dataset_id=(
            incoming_candidate.source_dataset_id
            if provider_kind == "incoming_reference"
            else None
        ),
        source_reference_requires_create=(
            incoming_candidate.requires_create
            if provider_kind == "incoming_reference"
            else None
        ),
    )
    protected = replace(
        current,
        value=protected_value,
        display_value=display_value,
        value_hash=value_hash,
        provider_kind=provider_kind,
        source_field_name=source_field_name,
        incoming_reference_choice_hash=(
            incoming_candidate.choice_hash
            if provider_kind == "incoming_reference"
            else None
        ),
    )
    decisions_by_key = {item.key: item for item in plan.create_field_decisions}
    decisions_by_key[key] = decision
    values_by_key = {item.key: item for item in evidence.values}
    values_by_key[key] = protected
    rotated = replace(
        evidence,
        evidence_id=str(uuid4()),
        values=tuple(sorted(values_by_key.values(), key=lambda item: item.key)),
        recorded_at=datetime.now(UTC),
    )
    models = tuple(
        replace(
            item,
            unresolved_create_fields=tuple(
                name for name in item.unresolved_create_fields
                if item.model != model or name != field_name
            ),
        )
        for item in plan.model_matches
    )
    return replace(
        plan,
        model_matches=models,
        create_field_decisions=tuple(
            sorted(decisions_by_key.values(), key=lambda item: item.key)
        ),
        create_field_evidence_id=rotated.evidence_id,
        create_field_evidence_hash=rotated.content_hash,
        protected_create_field_artifact_hash=None,
        create_field_evidence=rotated,
    )


def _require_create_field_evidence(
    plan: DestinationMatchPlan,
    evidence: DestinationCreateFieldEvidence,
) -> None:
    if plan.create_field_evidence is not None or (
        plan.create_field_evidence_id != evidence.evidence_id
        or plan.create_field_evidence_hash != evidence.content_hash
        or evidence.workspace_id != plan.workspace_id
        or evidence.source_selection_hash != plan.source_selection_hash
        or evidence.source_schema_hash != plan.source_schema_hash
        or evidence.destination_target_hash != plan.destination_target_hash
        or evidence.destination_read_principal_hash
        != plan.destination_read_principal_hash
        or evidence.destination_read_context_hash != plan.destination_read_context_hash
        or evidence.destination_schema_snapshot_hash
        != plan.destination_schema_snapshot_hash
    ):
        raise ValueError("Destination create-field evidence no longer matches")


def _valid_fixed_create_value(
    evidence: DestinationCreateFieldEvidenceValue,
    value: bool | int | float | str | None,
) -> bool:
    field_type = evidence.field_type
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type in {"float", "monetary"}:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isfinite(value)
        )
    if field_type == "selection":
        return isinstance(value, str) and value in {
            key for key, _label in evidence.selection
        }
    if field_type in {"char", "text", "html", "date", "datetime"}:
        return isinstance(value, str) and bool(value.strip())
    return False


def _fixed_display_value(
    evidence: DestinationCreateFieldEvidenceValue,
    value: bool | int | float | str | None,
) -> str:
    if evidence.field_type == "boolean":
        return "Yes" if value else "No"
    if evidence.field_type == "selection":
        label = next(
            label for key, label in evidence.selection if key == str(value)
        )
        return f"{label} ({value})"
    return str(value)


def carry_destination_create_field_reviews(
    fresh: DestinationMatchPlan,
    approved: DestinationMatchPlan,
    approved_evidence: DestinationCreateFieldEvidence | None = None,
) -> DestinationMatchPlan:
    """Carry reviews only when fresh field contracts and exact values still match."""

    if (
        fresh.workspace_id != approved.workspace_id
        or fresh.source_selection_hash != approved.source_selection_hash
        or fresh.source_schema_hash != approved.source_schema_hash
        or fresh.destination_target_hash != approved.destination_target_hash
        or fresh.destination_read_principal_hash
        != approved.destination_read_principal_hash
        or fresh.destination_read_context_hash != approved.destination_read_context_hash
    ):
        return fresh
    approved_by_key = {item.key: item for item in approved.create_field_decisions}
    fresh_by_key = {item.key: item for item in fresh.create_field_decisions}
    fresh_evidence = fresh.create_field_evidence
    fresh_values = (
        {item.key: item for item in fresh_evidence.values}
        if fresh_evidence is not None
        else {}
    )
    approved_values = (
        {item.key: item for item in approved_evidence.values}
        if approved_evidence is not None
        else {}
    )
    for key, prior in approved_by_key.items():
        if not prior.reviewed:
            continue
        current = fresh_by_key.get(key)
        if current is not None:
            if (
                prior.provider_kind == current.provider_kind
                and prior.field_contract_hash == current.field_contract_hash
                and prior.value_hash == current.value_hash
            ):
                fresh_by_key[key] = replace(current, reviewed=True)
            continue
        if prior.provider_kind not in {
            "fixed_value", "source_field", "existing_reference",
            "incoming_reference",
        }:
            continue
        requirement = fresh_values.get(key)
        protected = approved_values.get(key)
        if (
            requirement is None
            or protected is None
            or requirement.field_contract_hash != prior.field_contract_hash
            or protected.field_contract_hash != prior.field_contract_hash
            or protected.value_hash != prior.value_hash
            or protected.provider_kind != prior.provider_kind
            or (
                prior.provider_kind == "existing_reference"
                and not any(
                    candidate.odoo_id == protected.value
                    and candidate.target_binding_hash
                    == next(
                        (
                            approved_candidate.target_binding_hash
                            for approved_candidate in protected.reference_candidates
                            if approved_candidate.odoo_id == protected.value
                        ),
                        "",
                    )
                    for candidate in requirement.reference_candidates
                )
            )
            or (
                prior.provider_kind == "incoming_reference"
                and (
                    protected.incoming_reference_choice_hash is None
                    or not any(
                        candidate.choice_hash
                        == protected.incoming_reference_choice_hash
                        and candidate.source_dataset_id
                        == prior.source_dataset_id
                        and candidate.requires_create
                        == prior.source_reference_requires_create
                        for candidate in requirement.incoming_reference_candidates
                    )
                )
            )
        ):
            continue
        fresh_by_key[key] = prior
        fresh_values[key] = replace(
            protected,
            dataset_id=requirement.dataset_id,
            source_candidates=requirement.source_candidates,
            selection=requirement.selection,
            reference_candidates=requirement.reference_candidates,
            incoming_reference_candidates=(
                requirement.incoming_reference_candidates
            ),
        )
    carried = tuple(sorted(fresh_by_key.values(), key=lambda item: item.key))
    resolved_by_model: dict[str, set[str]] = {}
    for item in carried:
        if item.reviewed:
            resolved_by_model.setdefault(item.model, set()).add(item.field_name)
    models = tuple(
        replace(
            item,
            unresolved_create_fields=tuple(
                field_name
                for field_name in item.unresolved_create_fields
                if field_name not in resolved_by_model.get(item.model, set())
            ),
        )
        for item in fresh.model_matches
    )
    if fresh_evidence is not None and fresh_values:
        fresh_evidence = replace(
            fresh_evidence,
            values=tuple(sorted(fresh_values.values(), key=lambda item: item.key)),
        )
    return replace(
        fresh,
        model_matches=models,
        create_field_decisions=carried,
        create_field_evidence_hash=(
            fresh_evidence.content_hash if fresh_evidence is not None else None
        ),
        create_field_evidence=fresh_evidence,
    )
