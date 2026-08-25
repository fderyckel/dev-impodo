"""Portable, target-bound snapshots used by Many2one value matching.

Supporting lookups are deliberately separate from the project's primary Odoo
schema.  They contain only governed business-key values and labels: never
credentials, numeric Odoo identifiers, or complete target records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from uuid import uuid4

from .domain.serialization import canonical_json, content_hash
from .reference_keys import (
    REFERENCE_POLICY_HASH,
    StandardReferenceFieldContract,
)


_TECHNICAL_NAME = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
SUPPORTING_LOOKUP_CONTRACT_VERSION = 4


@dataclass(frozen=True, slots=True)
class SupportingLookupChoice:
    """One portable Odoo business-key value displayed to the operator."""

    value: str
    label: str

    def __post_init__(self) -> None:
        value = self.value.strip()
        label = self.label.strip()
        if not value or not label:
            raise ValueError("Supporting lookup values and labels must not be blank")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "label", label)


def supporting_lookup_key(
    *,
    relation_model: str,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
    display_field: str,
    reference_policy_hash: str = REFERENCE_POLICY_HASH,
    contract_version: int = SUPPORTING_LOOKUP_CONTRACT_VERSION,
) -> str:
    """Identify one lookup by its governed, target-independent semantics."""

    if contract_version != SUPPORTING_LOOKUP_CONTRACT_VERSION:
        raise ValueError("Supporting lookup contract version is unsupported")

    _validate_technical_name(relation_model, "related model")
    for field_name in (*key_fields, *scope_fields, display_field):
        _validate_technical_name(field_name, "related field")
    if not key_fields:
        raise ValueError("A supporting lookup requires at least one key field")
    payload = {
        "contract": f"supporting-lookup-key-v{contract_version}",
        "relation_model": relation_model,
        "key_fields": key_fields,
        "scope_fields": scope_fields,
        "display_field": display_field,
    }
    payload["reference_policy_hash"] = reference_policy_hash
    return content_hash(payload)


@dataclass(frozen=True, slots=True)
class SupportingLookupSnapshot:
    """Immutable portable choices plus exact target/read provenance."""

    snapshot_id: str
    workspace_id: str
    lookup_key: str
    relation_model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    display_field: str
    field_contracts: tuple[StandardReferenceFieldContract, ...]
    target_hash: str
    read_credential_binding_hash: str
    read_principal_hash: str
    read_permission_hash: str
    read_context_hash: str
    captured_at: datetime
    captured_by: str
    choices: tuple[SupportingLookupChoice, ...]
    ambiguous_values: tuple[str, ...]
    content_hash: str
    reference_policy_hash: str = REFERENCE_POLICY_HASH
    contract_version: int = SUPPORTING_LOOKUP_CONTRACT_VERSION

    @classmethod
    def capture(
        cls,
        *,
        workspace_id: str,
        relation_model: str,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
        display_field: str,
        field_contracts: tuple[StandardReferenceFieldContract, ...],
        target_hash: str,
        read_credential_binding_hash: str,
        read_principal_hash: str,
        read_permission_hash: str,
        read_context_hash: str,
        captured_at: datetime,
        captured_by: str,
        choices: tuple[SupportingLookupChoice, ...],
        ambiguous_values: tuple[str, ...],
    ) -> "SupportingLookupSnapshot":
        """Build and hash one normalized snapshot."""

        normalized_workspace_id = workspace_id.strip()
        normalized_actor = captured_by.strip()
        if not normalized_workspace_id or not normalized_actor:
            raise ValueError("Supporting lookup project and actor are required")
        lookup_key = supporting_lookup_key(
            relation_model=relation_model,
            key_fields=key_fields,
            scope_fields=scope_fields,
            display_field=display_field,
            reference_policy_hash=REFERENCE_POLICY_HASH,
        )
        normalized_choices = tuple(
            sorted(choices, key=lambda item: (item.label.casefold(), item.value))
        )
        if len({item.value for item in normalized_choices}) != len(
            normalized_choices
        ):
            raise ValueError("Supporting lookup choice values must be unique")
        normalized_ambiguous = tuple(
            sorted(
                {item.strip() for item in ambiguous_values if item.strip()},
                key=str.casefold,
            )
        )
        requested_fields = set((*key_fields, *scope_fields, display_field))
        contracts_by_name = {item.name: item for item in field_contracts}
        if set(contracts_by_name) != requested_fields:
            raise ValueError(
                "Supporting lookup field contracts must match its requested fields"
            )
        normalized_contracts = tuple(
            contracts_by_name[name] for name in sorted(contracts_by_name)
        )
        for contract in normalized_contracts:
            _validate_technical_name(contract.name, "related field")
            if contract.relation_model is not None:
                _validate_technical_name(contract.relation_model, "related model")
        semantic = {
            "contract": "supporting-lookup-snapshot-v4",
            "workspace_id": normalized_workspace_id,
            "lookup_key": lookup_key,
            "relation_model": relation_model,
            "key_fields": key_fields,
            "scope_fields": scope_fields,
            "display_field": display_field,
            "field_contracts": [
                {
                    "name": item.name,
                    "field_type": item.field_type,
                    "required": item.required,
                    "readonly": item.readonly,
                    "relation_model": item.relation_model,
                }
                for item in normalized_contracts
            ],
            "target_hash": target_hash,
            "read_credential_binding_hash": read_credential_binding_hash,
            "read_principal_hash": read_principal_hash,
            "read_permission_hash": read_permission_hash,
            "read_context_hash": read_context_hash,
            "reference_policy_hash": REFERENCE_POLICY_HASH,
            "choices": [
                {"value": item.value, "label": item.label}
                for item in normalized_choices
            ],
            "ambiguous_values": normalized_ambiguous,
        }
        return cls(
            snapshot_id=str(uuid4()),
            workspace_id=normalized_workspace_id,
            lookup_key=lookup_key,
            relation_model=relation_model,
            key_fields=key_fields,
            scope_fields=scope_fields,
            display_field=display_field,
            field_contracts=normalized_contracts,
            target_hash=target_hash,
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash=read_principal_hash,
            read_permission_hash=read_permission_hash,
            read_context_hash=read_context_hash,
            captured_at=captured_at,
            captured_by=normalized_actor,
            choices=normalized_choices,
            ambiguous_values=normalized_ambiguous,
            content_hash=content_hash(semantic),
            reference_policy_hash=REFERENCE_POLICY_HASH,
            contract_version=SUPPORTING_LOOKUP_CONTRACT_VERSION,
        )

    def to_json(self) -> str:
        """Serialize complete immutable evidence deterministically."""

        return canonical_json(
            {
                "snapshot_id": self.snapshot_id,
                "contract_version": self.contract_version,
                "workspace_id": self.workspace_id,
                "lookup_key": self.lookup_key,
                "relation_model": self.relation_model,
                "key_fields": self.key_fields,
                "scope_fields": self.scope_fields,
                "display_field": self.display_field,
                "field_contracts": [
                    {
                        "name": item.name,
                        "field_type": item.field_type,
                        "required": item.required,
                        "readonly": item.readonly,
                        "relation_model": item.relation_model,
                    }
                    for item in self.field_contracts
                ],
                "target_hash": self.target_hash,
                "read_credential_binding_hash": self.read_credential_binding_hash,
                "read_principal_hash": self.read_principal_hash,
                "read_permission_hash": self.read_permission_hash,
                "read_context_hash": self.read_context_hash,
                "reference_policy_hash": self.reference_policy_hash,
                "captured_at": self.captured_at,
                "captured_by": self.captured_by,
                "choices": [
                    {"value": item.value, "label": item.label}
                    for item in self.choices
                ],
                "ambiguous_values": self.ambiguous_values,
                "content_hash": self.content_hash,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> "SupportingLookupSnapshot":
        """Restore stored evidence and reject semantic tampering."""

        payload = json.loads(value)
        expected_fields = {
            "snapshot_id", "contract_version", "workspace_id", "lookup_key",
            "relation_model", "key_fields", "scope_fields", "display_field",
            "field_contracts",
            "target_hash", "read_credential_binding_hash", "read_principal_hash",
            "read_permission_hash", "read_context_hash", "reference_policy_hash",
            "captured_at", "captured_by", "choices", "ambiguous_values",
            "content_hash",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ValueError("Supporting lookup evidence shape is unsupported")
        contract_version = int(payload["contract_version"])
        if contract_version != SUPPORTING_LOOKUP_CONTRACT_VERSION:
            raise ValueError("Supporting lookup contract version is unsupported")
        reference_policy_hash = str(payload["reference_policy_hash"])
        restored = cls(
            snapshot_id=str(payload["snapshot_id"]),
            workspace_id=str(payload["workspace_id"]),
            lookup_key=str(payload["lookup_key"]),
            relation_model=str(payload["relation_model"]),
            key_fields=tuple(str(item) for item in payload["key_fields"]),
            scope_fields=tuple(str(item) for item in payload["scope_fields"]),
            display_field=str(payload["display_field"]),
            field_contracts=tuple(
                StandardReferenceFieldContract(
                    name=str(item["name"]),
                    field_type=str(item["field_type"]),
                    required=bool(item["required"]),
                    readonly=bool(item["readonly"]),
                    relation_model=(
                        str(item["relation_model"])
                        if item["relation_model"] is not None
                        else None
                    ),
                )
                for item in payload["field_contracts"]
            ),
            target_hash=str(payload["target_hash"]),
            read_credential_binding_hash=str(
                payload["read_credential_binding_hash"]
            ),
            read_principal_hash=str(payload["read_principal_hash"]),
            read_permission_hash=str(payload["read_permission_hash"]),
            read_context_hash=str(payload["read_context_hash"]),
            captured_at=datetime.fromisoformat(str(payload["captured_at"])),
            captured_by=str(payload["captured_by"]),
            choices=tuple(
                SupportingLookupChoice(
                    value=str(item["value"]),
                    label=str(item["label"]),
                )
                for item in payload["choices"]
            ),
            ambiguous_values=tuple(
                str(item) for item in payload["ambiguous_values"]
            ),
            content_hash=str(payload["content_hash"]),
            reference_policy_hash=reference_policy_hash,
            contract_version=contract_version,
        )
        expected_lookup_key = supporting_lookup_key(
            relation_model=restored.relation_model,
            key_fields=restored.key_fields,
            scope_fields=restored.scope_fields,
            display_field=restored.display_field,
            reference_policy_hash=restored.reference_policy_hash,
            contract_version=restored.contract_version,
        )
        semantic = {
            "contract": f"supporting-lookup-snapshot-v{restored.contract_version}",
            "workspace_id": restored.workspace_id,
            "lookup_key": restored.lookup_key,
            "relation_model": restored.relation_model,
            "key_fields": restored.key_fields,
            "scope_fields": restored.scope_fields,
            "display_field": restored.display_field,
            "field_contracts": [
                {
                    "name": item.name,
                    "field_type": item.field_type,
                    "required": item.required,
                    "readonly": item.readonly,
                    "relation_model": item.relation_model,
                }
                for item in restored.field_contracts
            ],
            "target_hash": restored.target_hash,
            "read_credential_binding_hash": restored.read_credential_binding_hash,
            "read_principal_hash": restored.read_principal_hash,
            "read_permission_hash": restored.read_permission_hash,
            "read_context_hash": restored.read_context_hash,
            "choices": [
                {"value": item.value, "label": item.label}
                for item in restored.choices
            ],
            "ambiguous_values": restored.ambiguous_values,
        }
        semantic["reference_policy_hash"] = restored.reference_policy_hash
        if restored.lookup_key != expected_lookup_key:
            raise ValueError("Supporting lookup key is invalid")
        if restored.content_hash != content_hash(semantic):
            raise ValueError("Supporting lookup content hash is invalid")
        return restored


def _validate_technical_name(value: str, label: str) -> None:
    if _TECHNICAL_NAME.fullmatch(value) is None:
        raise ValueError(f"The {label} is invalid")
