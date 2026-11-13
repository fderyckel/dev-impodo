"""Resolve reviewed reference aliases without changing frozen source identities.

Raw choices and resolved populations are protected, transient evidence. Only
``TransferIdentityAliasBinding`` belongs in a public destination match plan.
This helper grants no read, write, company, or version compatibility authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from impodo.domain.mapping.contracts import (
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.serialization import canonical_json, content_hash

TRANSFER_IDENTITY_ALIAS_CONTRACT_VERSION = 1
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NO_WRITE_HANDLINGS = frozenset({"reference_only", "reuse_only"})
_MODEL = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\Z")
type PortableIdentity = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransferIdentityAliasBinding:
    """Bind protected choices and both ordered populations using hashes only."""

    key_fields: tuple[str, ...]
    destination_handling: str
    aliases_hash: str
    source_population_hash: str
    destination_population_hash: str
    alias_count: int
    contract_version: int = TRANSFER_IDENTITY_ALIAS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not int
            or self.contract_version != TRANSFER_IDENTITY_ALIAS_CONTRACT_VERSION
            or not self.key_fields
            or len(set(self.key_fields)) != len(self.key_fields)
            or any(not field.strip() for field in self.key_fields)
            or type(self.alias_count) is not int
            or self.alias_count < 0
            or any(
                _HASH.fullmatch(value) is None
                for value in (
                    self.aliases_hash,
                    self.source_population_hash,
                    self.destination_population_hash,
                )
            )
            or (
                self.alias_count
                and (
                    len(self.key_fields) != 1
                    or self.destination_handling not in _NO_WRITE_HANDLINGS
                )
            )
        ):
            raise ValueError("Transfer identity alias binding is invalid")

    @property
    def content_hash(self) -> str:
        """Bind the exact rule meaning without exposing raw business values."""

        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        """Return only public key names, handling, counts, and one-way hashes."""

        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TransferIdentityAliasBinding:
        """Read a closed hash-only binding without accepting raw alias values."""

        if set(payload) != {
            "key_fields",
            "destination_handling",
            "aliases_hash",
            "source_population_hash",
            "destination_population_hash",
            "alias_count",
            "contract_version",
        }:
            raise ValueError("Transfer identity alias binding has an invalid shape")
        fields = payload["key_fields"]
        if not isinstance(fields, list | tuple) or any(
            not isinstance(item, str) for item in fields
        ):
            raise ValueError("Transfer identity alias key fields are invalid")
        count, version = payload["alias_count"], payload["contract_version"]
        if type(count) is not int or type(version) is not int:
            raise ValueError("Transfer identity alias counts or version are invalid")
        return cls(
            key_fields=tuple(fields),
            destination_handling=_text(payload, "destination_handling"),
            aliases_hash=_text(payload, "aliases_hash"),
            source_population_hash=_text(payload, "source_population_hash"),
            destination_population_hash=_text(payload, "destination_population_hash"),
            alias_count=count,
            contract_version=version,
        )


@dataclass(frozen=True, slots=True)
class TransferIdentityAliasResolution:
    """Carry protected source and effective destination identities separately."""

    source_identities: tuple[PortableIdentity, ...]
    destination_identities: tuple[PortableIdentity, ...]
    aliases: tuple[ValueMapping, ...]
    binding: TransferIdentityAliasBinding


@dataclass(frozen=True, slots=True)
class DestinationIdentityAliasBinding:
    """Identify one aliased dataset without retaining any business-key values."""

    dataset_id: str
    model: str
    binding: TransferIdentityAliasBinding

    def __post_init__(self) -> None:
        if (
            not self.dataset_id.strip()
            or _MODEL.fullmatch(self.model) is None
            or self.binding.alias_count < 1
        ):
            raise ValueError("Destination identity alias dataset binding is invalid")

    @property
    def key(self) -> tuple[str, str]:
        return self.model, self.dataset_id

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "model": self.model,
            "binding": self.binding.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> DestinationIdentityAliasBinding:
        if set(payload) != {"dataset_id", "model", "binding"}:
            raise ValueError(
                "Destination identity alias dataset binding has an invalid shape"
            )
        return cls(
            _text(payload, "dataset_id"),
            _text(payload, "model"),
            TransferIdentityAliasBinding.from_dict(_mapping(payload["binding"])),
        )


@dataclass(frozen=True, slots=True)
class DestinationIdentityAliasEvidenceValue:
    """Keep exact reviewed choices solely inside protected destination evidence."""

    dataset_id: str
    model: str
    aliases: tuple[ValueMapping, ...]
    binding: TransferIdentityAliasBinding

    def __post_init__(self) -> None:
        _ = self.public_binding
        if self.aliases != normalize_transfer_identity_aliases(self.aliases) or (
            self.binding.alias_count != len(self.aliases)
            or self.binding.aliases_hash
            != content_hash(transfer_identity_aliases_to_dict(self.aliases))
        ):
            raise ValueError(
                "Protected identity aliases do not match their public binding"
            )

    @property
    def public_binding(self) -> DestinationIdentityAliasBinding:
        return DestinationIdentityAliasBinding(
            self.dataset_id, self.model, self.binding
        )

    @property
    def key(self) -> tuple[str, str]:
        return self.model, self.dataset_id

    def to_dict(self) -> dict[str, object]:
        return {
            **self.public_binding.to_dict(),
            "choices": transfer_identity_aliases_to_dict(self.aliases),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> DestinationIdentityAliasEvidenceValue:
        if set(payload) != {"dataset_id", "model", "binding", "choices"}:
            raise ValueError("Protected identity alias dataset has an invalid shape")
        return cls(
            _text(payload, "dataset_id"),
            _text(payload, "model"),
            transfer_identity_aliases_from_dict(_mapping(payload["choices"])),
            TransferIdentityAliasBinding.from_dict(_mapping(payload["binding"])),
        )


@dataclass(frozen=True, slots=True)
class DestinationIdentityAliasEvidence:
    """Bind encrypted aliases to one exact source, target, access and read."""

    evidence_id: str
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
    values: tuple[DestinationIdentityAliasEvidenceValue, ...]
    recorded_at: datetime
    contract_version: int = 1

    def __post_init__(self) -> None:
        try:
            UUID(self.evidence_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(
                "Protected identity alias evidence ID is invalid"
            ) from error
        payload = asdict(self)
        hashes = (value for name, value in payload.items() if name.endswith("_hash"))
        if (
            not self.workspace_id.strip()
            or self.recorded_at.tzinfo is None
            or type(self.contract_version) is not int
            or self.contract_version != 1
            or any(
                not isinstance(value, str) or _HASH.fullmatch(value) is None
                for value in hashes
            )
            or not 1 <= len(self.values) <= 100
            or self.values != tuple(sorted(self.values, key=lambda item: item.key))
            or len({item.dataset_id for item in self.values}) != len(self.values)
            or len({item.model for item in self.values}) != len(self.values)
        ):
            raise ValueError("Protected identity alias evidence binding is invalid")

    @property
    def public_bindings(self) -> tuple[DestinationIdentityAliasBinding, ...]:
        return tuple(item.public_binding for item in self.values)

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload = asdict(self)
        payload["values"] = [item.to_dict() for item in self.values]
        payload["recorded_at"] = self.recorded_at.isoformat()
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> DestinationIdentityAliasEvidence:
        """Validate protected payload shape and logical integrity before use."""

        try:
            payload = _mapping(json.loads(value))
            values = payload["values"]
            if not isinstance(values, list):
                raise TypeError("Protected identity alias values are invalid")
            version = payload["contract_version"]
            if type(version) is not int:
                raise ValueError("Protected identity alias version is invalid")
            result = cls(
                evidence_id=_text(payload, "evidence_id"),
                workspace_id=_text(payload, "workspace_id"),
                source_selection_hash=_text(payload, "source_selection_hash"),
                source_schema_hash=_text(payload, "source_schema_hash"),
                destination_target_hash=_text(payload, "destination_target_hash"),
                destination_credential_binding_hash=_text(
                    payload, "destination_credential_binding_hash"
                ),
                destination_read_principal_hash=_text(
                    payload, "destination_read_principal_hash"
                ),
                destination_read_permission_hash=_text(
                    payload, "destination_read_permission_hash"
                ),
                destination_read_context_hash=_text(
                    payload, "destination_read_context_hash"
                ),
                destination_schema_snapshot_hash=_text(
                    payload, "destination_schema_snapshot_hash"
                ),
                destination_record_snapshot_hash=_text(
                    payload, "destination_record_snapshot_hash"
                ),
                values=tuple(
                    DestinationIdentityAliasEvidenceValue.from_dict(_mapping(item))
                    for item in values
                ),
                recorded_at=datetime.fromisoformat(_text(payload, "recorded_at")),
                contract_version=version,
            )
            if (
                set(payload) != set(result.to_dict())
                or payload.get("content_hash") != result.content_hash
            ):
                raise ValueError("Protected identity alias content hash is invalid")
            return result
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError("Stored protected identity aliases are invalid") from error


def _text(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise TypeError("Identity alias evidence requires text values")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("Identity alias evidence requires a closed object")
    return value


def aliased_identity(
    values: PortableIdentity, aliases: Sequence[ValueMapping]
) -> PortableIdentity:
    """Apply already validated single-key aliases inside a protected operation."""

    if not aliases:
        return values
    if len(values) != 1:
        raise ValueError("Identity aliases require one scalar business-key field")
    return (
        {item.source_value: item.target_value for item in aliases}.get(
            values[0], values[0]
        ),
    )


@dataclass(frozen=True, slots=True)
class DestinationIdentityAliasRule:
    """Remember portable choices that every later destination read revalidates."""

    model: str
    key_fields: tuple[str, ...]
    aliases: tuple[ValueMapping, ...]

    def __post_init__(self) -> None:
        if (
            _MODEL.fullmatch(self.model) is None
            or len(self.key_fields) != 1
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key_fields[0]) is None
            or not self.aliases
        ):
            raise ValueError(
                "Saved identity alias rule requires one scalar business key"
            )
        object.__setattr__(
            self, "aliases", normalize_transfer_identity_aliases(self.aliases)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "key_fields": self.key_fields,
            "choices": transfer_identity_aliases_to_dict(self.aliases),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DestinationIdentityAliasRule:
        if set(payload) != {"model", "key_fields", "choices"}:
            raise ValueError("Saved identity alias rule has an invalid shape")
        fields = payload["key_fields"]
        if not isinstance(fields, list | tuple) or any(
            not isinstance(item, str) for item in fields
        ):
            raise ValueError("Saved identity alias key fields are invalid")
        return cls(
            _text(payload, "model"),
            tuple(fields),
            transfer_identity_aliases_from_dict(_mapping(payload["choices"])),
        )


@dataclass(frozen=True, slots=True)
class ProtectedIdentityAliasRulesReference:
    """Persist only protected artifact identity, hashes and stable owner meaning."""

    rules_id: str
    rules_hash: str
    artifact_hash: str
    owner_key: str

    def __post_init__(self) -> None:
        try:
            UUID(self.rules_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(
                "Saved identity alias rule reference is invalid"
            ) from error
        if any(
            _HASH.fullmatch(value) is None
            for value in (self.rules_hash, self.artifact_hash, self.owner_key)
        ):
            raise ValueError("Saved identity alias rule hashes are invalid")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> ProtectedIdentityAliasRulesReference:
        if set(payload) != {"rules_id", "rules_hash", "artifact_hash", "owner_key"}:
            raise ValueError("Saved identity alias rule reference has an invalid shape")
        return cls(
            _text(payload, "rules_id"),
            _text(payload, "rules_hash"),
            _text(payload, "artifact_hash"),
            _text(payload, "owner_key"),
        )


def normalize_transfer_identity_aliases(
    aliases: Sequence[ValueMapping],
) -> tuple[ValueMapping, ...]:
    """Reuse canonical relationship choice limits, ordering and uniqueness.

    Transfer identities already trim scalar components. Apply that same meaning
    to aliases before checking duplicates; whitespace cannot conceal a conflict.
    """

    normalized = tuple(
        ValueMapping(item.source_value.strip(), item.target_value.strip())
        for item in aliases
    )
    return RelationshipResolver(
        origin=ResolverOrigin.TARGET_CATALOG, value_mappings=normalized
    ).value_mappings


def resolve_transfer_identity_aliases(
    source_identities: Sequence[PortableIdentity],
    *,
    key_fields: tuple[str, ...],
    destination_handling: str,
    aliases: Sequence[ValueMapping] = (),
) -> TransferIdentityAliasResolution:
    """Map one scalar reference identity after included-row scope is finalized.

    Callers supply canonical portable string components, never source Odoo IDs.
    Unmapped rows retain their ordinary identity. A mapped row must resolve to
    one existing accessible destination record in the caller's checked read;
    this pure helper cannot establish that remote evidence.

    Empty aliases preserve the existing identity population, including issues
    that ordinary matching must report. Explicit aliases require all included
    identities to be nonblank and unique and cannot merge distinct records.
    """

    source = tuple(tuple(identity) for identity in source_identities)
    normalized = normalize_transfer_identity_aliases(aliases)
    if not key_fields or any(len(identity) != len(key_fields) for identity in source):
        raise ValueError("Transfer identity components do not match their key fields")
    destination = source
    if normalized:
        if destination_handling not in _NO_WRITE_HANDLINGS or len(key_fields) != 1:
            raise ValueError(
                "Identity aliases require one scalar key and existing-record-only handling"
            )
        if any(
            not isinstance(part, str) or not part or part.strip() != part
            for identity in source
            for part in identity
        ):
            raise ValueError(
                "Aliased source identities must be nonblank portable values"
            )
        if len(set(source)) != len(source):
            raise ValueError("Aliased source identities must identify unique records")
        translations = {item.source_value: item.target_value for item in normalized}
        present = {identity[0] for identity in source}
        if translations.keys() - present:
            raise ValueError("An identity alias is outside the included source scope")
        destination = tuple(
            (translations.get(identity[0], identity[0]),) for identity in source
        )
        if len(set(destination)) != len(destination):
            raise ValueError("Identity aliases cannot merge distinct source records")
    binding = TransferIdentityAliasBinding(
        key_fields=key_fields,
        destination_handling=destination_handling,
        aliases_hash=content_hash(transfer_identity_aliases_to_dict(normalized)),
        source_population_hash=content_hash(source),
        destination_population_hash=content_hash(destination),
        alias_count=len(normalized),
    )
    return TransferIdentityAliasResolution(source, destination, normalized, binding)


def transfer_identity_aliases_to_dict(
    aliases: Sequence[ValueMapping],
) -> dict[str, object]:
    """Serialize raw choices for encrypted evidence, never for a public plan."""

    normalized = normalize_transfer_identity_aliases(aliases)
    return {
        "contract_version": TRANSFER_IDENTITY_ALIAS_CONTRACT_VERSION,
        "aliases": [asdict(item) for item in normalized],
    }


def transfer_identity_aliases_from_dict(
    payload: Mapping[str, object],
) -> tuple[ValueMapping, ...]:
    """Reject unknown fields and generations when opening protected choices."""

    version = payload.get("contract_version")
    raw = payload.get("aliases")
    if (
        set(payload) != {"contract_version", "aliases"}
        or type(version) is not int
        or version != TRANSFER_IDENTITY_ALIAS_CONTRACT_VERSION
        or not isinstance(raw, list | tuple)
    ):
        raise ValueError("Protected transfer identity aliases have an invalid shape")
    aliases = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "source_value",
            "target_value",
        }:
            raise ValueError("Protected transfer identity alias has an invalid shape")
        source: object = item["source_value"]
        target: object = item["target_value"]
        if not isinstance(source, str) or not isinstance(target, str):
            raise TypeError(
                "Protected transfer identity aliases require portable values"
            )
        aliases.append(ValueMapping(source, target))
    return normalize_transfer_identity_aliases(aliases)
