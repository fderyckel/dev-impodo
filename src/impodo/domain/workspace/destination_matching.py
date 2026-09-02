"""Destination-aware matching evidence for Odoo-to-Odoo transfers.

The current plan stores portable technical names, aggregate counts, and
one-way snapshot hashes. Raw source values and destination numeric identifiers
remain inside their protected read boundaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Any

from impodo.domain.serialization import canonical_json, content_hash


DESTINATION_MATCH_CONTRACT_VERSION = 2
_SUPPORTED_DESTINATION_MATCH_CONTRACT_VERSIONS = frozenset({1, 2})
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


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
    compatible_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]
    incompatible_fields: tuple[str, ...]
    destination_limit_reached: bool

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
        field_groups = (
            self.compatible_fields,
            self.missing_fields,
            self.incompatible_fields,
        )
        if any(group != tuple(sorted(set(group))) for group in field_groups):
            raise ValueError("Destination field results must be sorted and unique")
        if len(set().union(*map(set, field_groups))) != sum(
            len(group) for group in field_groups
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
        if self.missing_fields:
            reasons.append("DESTINATION_FIELDS_MISSING")
        if self.incompatible_fields:
            reasons.append("DESTINATION_FIELDS_INCOMPATIBLE")
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
    contract_version: int = DESTINATION_MATCH_CONTRACT_VERSION

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

    @property
    def ready(self) -> bool:
        return (
            self.contract_version == DESTINATION_MATCH_CONTRACT_VERSION
            and bool(self.model_matches)
            and all(item.ready for item in self.model_matches)
            and all(item.ready for item in self.relationship_matches)
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
            "model_matches": [asdict(item) for item in self.model_matches],
            "recorded_at": self.recorded_at.isoformat(),
            "recorded_by": self.recorded_by,
        }
        if self.contract_version >= 2:
            payload["relationship_matches"] = [
                asdict(item) for item in self.relationship_matches
            ]
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
                        compatible_fields=tuple(item["compatible_fields"]),
                        missing_fields=tuple(item["missing_fields"]),
                        incompatible_fields=tuple(item["incompatible_fields"]),
                        destination_limit_reached=bool(
                            item["destination_limit_reached"]
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
                    )
                    for item in payload.get("relationship_matches", ())
                ),
                contract_version=contract_version,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Stored destination matching plan is invalid") from error
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Stored destination matching plan hash is invalid")
        return result
