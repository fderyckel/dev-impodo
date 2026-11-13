"""Resolve reviewed reference aliases without changing frozen source identities.

Raw choices and resolved populations are protected, transient evidence. Only
``TransferIdentityAliasBinding`` belongs in a public destination match plan.
This helper grants no read, write, company, or version compatibility authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from impodo.domain.mapping.contracts import (
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.serialization import content_hash

TRANSFER_IDENTITY_ALIAS_CONTRACT_VERSION = 1
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NO_WRITE_HANDLINGS = frozenset({"reference_only", "reuse_only"})
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


@dataclass(frozen=True, slots=True)
class TransferIdentityAliasResolution:
    """Carry protected source and effective destination identities separately."""

    source_identities: tuple[PortableIdentity, ...]
    destination_identities: tuple[PortableIdentity, ...]
    aliases: tuple[ValueMapping, ...]
    binding: TransferIdentityAliasBinding


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
            raise ValueError("Aliased source identities must be nonblank portable values")
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
        if not isinstance(item, Mapping) or set(item) != {"source_value", "target_value"}:
            raise ValueError("Protected transfer identity alias has an invalid shape")
        source: object = item["source_value"]
        target: object = item["target_value"]
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("Protected transfer identity aliases require portable values")
        aliases.append(ValueMapping(source, target))
    return normalize_transfer_identity_aliases(aliases)
