"""Versioned scope and reference-data contracts for advanced coverage.

Migration stages: A-D. Layer: domain contracts. These objects make coverage
applicability and exact reference content explicit before canonical
preparation starts. They contain no executable rules, credentials, target
record IDs, or runtime storage concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Mapping
from uuid import UUID

from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import (
    assert_no_numeric_odoo_ids,
    portable_value,
    restore_portable_value,
)
from .serialization import canonical_json, content_hash


COVERAGE_SCOPE_CONTRACT_VERSION = 2
REFERENCE_DATA_CONTRACT_VERSION = 2
REFERENCE_BUNDLE_CONTRACT_VERSION = 2
MAX_REFERENCE_DATASETS = 50
MAX_REFERENCE_ROWS_PER_DATASET = 10_000
MAX_REFERENCE_KEY_PARTS = 5
MAX_REFERENCE_VALUE_FIELDS = 20
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_TECHNICAL_NAME = re.compile(r"[a-z_][a-z0-9_.]{0,127}")


class CoverageFamily(StrEnum):
    """Case-family identifiers from the authoritative coverage ledger."""

    TC_01 = "TC-01"
    TC_02 = "TC-02"
    TC_03 = "TC-03"
    TC_04 = "TC-04"
    TC_05 = "TC-05"
    TC_06 = "TC-06"
    TC_07 = "TC-07"
    TC_08 = "TC-08"
    TC_09 = "TC-09"
    TC_10 = "TC-10"
    TC_11 = "TC-11"
    TC_12 = "TC-12"
    TC_13 = "TC-13"
    TC_14 = "TC-14"
    TC_15 = "TC-15"
    TC_16 = "TC-16"
    TC_17 = "TC-17"
    TC_18 = "TC-18"
    TC_19 = "TC-19"
    TC_20 = "TC-20"
    TC_21 = "TC-21"
    TC_22 = "TC-22"
    TC_23 = "TC-23"
    TC_24 = "TC-24"


class CoverageApplicability(StrEnum):
    APPLICABLE = "APPLICABLE"
    INAPPLICABLE = "INAPPLICABLE"


class ReferenceValueKind(StrEnum):
    DISPLAY_VALUE = "DISPLAY_VALUE"
    BUSINESS_KEY = "BUSINESS_KEY"
    ODOO_SELECTION_KEY = "ODOO_SELECTION_KEY"


@dataclass(frozen=True, slots=True)
class CoverageDeclaration:
    """One reviewed applicability choice for one ledger family."""

    family: CoverageFamily
    applicability: CoverageApplicability
    rationale: str
    datasets: tuple[str, ...] = ()
    owner_role: str = "Data manager"

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", CoverageFamily(self.family))
        object.__setattr__(
            self,
            "applicability",
            CoverageApplicability(self.applicability),
        )
        rationale = _required_text(self.rationale, "coverage rationale", 1_000)
        owner_role = _required_text(self.owner_role, "coverage owner", 120)
        datasets = tuple(sorted({_dataset_name(item) for item in self.datasets}))
        if self.applicability is CoverageApplicability.APPLICABLE and not datasets:
            raise ValueError("Applicable coverage requires at least one dataset")
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "owner_role", owner_role)
        object.__setattr__(self, "datasets", datasets)

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "applicability": self.applicability.value,
            "rationale": self.rationale,
            "datasets": list(self.datasets),
            "owner_role": self.owner_role,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CoverageDeclaration":
        return cls(
            family=CoverageFamily(str(payload["family"])),
            applicability=CoverageApplicability(str(payload["applicability"])),
            rationale=str(payload["rationale"]),
            datasets=tuple(str(item) for item in payload.get("datasets", ())),
            owner_role=str(payload.get("owner_role", "Data manager")),
        )


@dataclass(frozen=True, slots=True)
class CoverageScopeRevision:
    """Complete approved coverage applicability for one workspace revision."""

    scope_id: str
    workspace_id: str
    version: int
    parent_version: int | None
    source_selection_hash: str
    declarations: tuple[CoverageDeclaration, ...]
    approved_by: ActorIdentity
    approved_at: datetime
    contract_version: int = COVERAGE_SCOPE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _uuid(self.scope_id, "coverage scope ID")
        _required_text(self.workspace_id, "workspace ID", 200)
        _hash(self.source_selection_hash, "source selection hash")
        if self.contract_version != COVERAGE_SCOPE_CONTRACT_VERSION:
            raise ValueError("Coverage-scope contract version is unsupported")
        if self.version < 1:
            raise ValueError("Coverage-scope version must be positive")
        if self.parent_version is not None and self.parent_version >= self.version:
            raise ValueError("Coverage-scope parent version is invalid")
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("Coverage-scope approval time must be timezone-aware")
        expected = tuple(sorted(self.declarations, key=lambda item: item.family.value))
        if self.declarations != expected:
            raise ValueError("Coverage declarations must use ledger order")
        declared = {item.family for item in self.declarations}
        if len(declared) != len(self.declarations):
            raise ValueError("Coverage families must be declared exactly once")
        missing = set(CoverageFamily) - declared
        if missing:
            labels = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"Coverage scope is incomplete: {labels}")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False, include_approval=False))

    def declaration(self, family: CoverageFamily) -> CoverageDeclaration:
        requested = CoverageFamily(family)
        return next(item for item in self.declarations if item.family is requested)

    def to_portable_dict(
        self,
        *,
        include_hash: bool = True,
        include_approval: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "scope_id": self.scope_id,
            "workspace_id": self.workspace_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "source_selection_hash": self.source_selection_hash,
            "declarations": [item.to_portable_dict() for item in self.declarations],
        }
        if include_approval:
            payload["approved_by"] = {
                "issuer": self.approved_by.issuer,
                "subject_id": self.approved_by.subject_id,
                "display_name": self.approved_by.display_name,
            }
            payload["approved_at"] = self.approved_at.isoformat()
        if include_hash:
            payload["content_hash"] = self.content_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    def to_json(self) -> str:
        return canonical_json(self.to_portable_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CoverageScopeRevision":
        actor = dict(payload["approved_by"])
        result = cls(
            contract_version=int(payload["contract_version"]),
            scope_id=str(payload["scope_id"]),
            workspace_id=str(payload["workspace_id"]),
            version=int(payload["version"]),
            parent_version=(
                int(payload["parent_version"])
                if payload.get("parent_version") is not None
                else None
            ),
            source_selection_hash=str(payload["source_selection_hash"]),
            declarations=tuple(
                CoverageDeclaration.from_dict(item)
                for item in payload.get("declarations", ())
            ),
            approved_by=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
            approved_at=datetime.fromisoformat(str(payload["approved_at"])),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Coverage-scope content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class ReferenceEntry:
    """One typed exact-key reference entry."""

    key: tuple[Any, ...]
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not 1 <= len(self.key) <= MAX_REFERENCE_KEY_PARTS:
            raise ValueError("Reference keys require one to five parts")
        if not self.values or len(self.values) > MAX_REFERENCE_VALUE_FIELDS:
            raise ValueError("Reference entries require bounded output values")
        fields = tuple(self.values)
        if len(set(fields)) != len(fields) or any(
            not _TECHNICAL_NAME.fullmatch(str(field)) for field in fields
        ):
            raise ValueError("Reference output field names are invalid")
        portable = self.to_portable_dict()
        assert_no_numeric_odoo_ids(portable)

    @property
    def key_hash(self) -> str:
        return content_hash(portable_value(self.key))

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "key": portable_value(self.key),
            "values": portable_value(dict(sorted(self.values.items()))),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReferenceEntry":
        key = restore_portable_value(payload["key"])
        values = restore_portable_value(payload["values"])
        if not isinstance(key, tuple) or not isinstance(values, dict):
            raise ValueError("Reference entry portable values are invalid")
        return cls(key=key, values=values)


@dataclass(frozen=True, slots=True)
class ReferenceDataSet:
    """Immutable exact lookup data with ownership and semantic versioning."""

    reference_id: str
    version: int
    name: str
    key_fields: tuple[str, ...]
    value_kinds: Mapping[str, ReferenceValueKind]
    entries: tuple[ReferenceEntry, ...]
    owner: str
    classification: str
    effective_label: str
    contract_version: int = REFERENCE_DATA_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _uuid(self.reference_id, "reference dataset ID")
        if self.contract_version != REFERENCE_DATA_CONTRACT_VERSION:
            raise ValueError("Reference-data contract version is unsupported")
        if self.version < 1:
            raise ValueError("Reference-data version must be positive")
        _required_text(self.name, "reference-data name", 160)
        _required_text(self.owner, "reference-data owner", 160)
        _required_text(self.classification, "reference-data classification", 80)
        _required_text(self.effective_label, "reference-data effective label", 160)
        if not 1 <= len(self.key_fields) <= MAX_REFERENCE_KEY_PARTS:
            raise ValueError("Reference data requires one to five key fields")
        if len(set(self.key_fields)) != len(self.key_fields) or any(
            not _TECHNICAL_NAME.fullmatch(item) for item in self.key_fields
        ):
            raise ValueError("Reference-data key fields are invalid")
        if not self.value_kinds or len(self.value_kinds) > MAX_REFERENCE_VALUE_FIELDS:
            raise ValueError("Reference data requires bounded output fields")
        normalized_kinds = {
            str(field): ReferenceValueKind(kind)
            for field, kind in self.value_kinds.items()
        }
        if any(not _TECHNICAL_NAME.fullmatch(field) for field in normalized_kinds):
            raise ValueError("Reference-data output fields are invalid")
        object.__setattr__(self, "value_kinds", normalized_kinds)
        if not self.entries or len(self.entries) > MAX_REFERENCE_ROWS_PER_DATASET:
            raise ValueError("Reference-data row count is outside the supported bound")
        if any(len(item.key) != len(self.key_fields) for item in self.entries):
            raise ValueError("Reference entry key shape does not match its dataset")
        if any(set(item.values) != set(normalized_kinds) for item in self.entries):
            raise ValueError("Reference entry output shape does not match its dataset")
        ordered = tuple(sorted(self.entries, key=lambda item: item.key_hash))
        if self.entries != ordered:
            raise ValueError("Reference entries must use deterministic key order")
        hashes = [item.key_hash for item in self.entries]
        if len(set(hashes)) != len(hashes):
            raise ValueError("Reference keys must be unique")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    def lookup(self, key: tuple[Any, ...]) -> ReferenceEntry | None:
        key_hash = content_hash(portable_value(key))
        return next((item for item in self.entries if item.key_hash == key_hash), None)

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "reference_id": self.reference_id,
            "version": self.version,
            "name": self.name,
            "key_fields": list(self.key_fields),
            "value_kinds": {
                field: kind.value for field, kind in sorted(self.value_kinds.items())
            },
            "entries": [item.to_portable_dict() for item in self.entries],
            "owner": self.owner,
            "classification": self.classification,
            "effective_label": self.effective_label,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReferenceDataSet":
        result = cls(
            contract_version=int(payload["contract_version"]),
            reference_id=str(payload["reference_id"]),
            version=int(payload["version"]),
            name=str(payload["name"]),
            key_fields=tuple(str(item) for item in payload.get("key_fields", ())),
            value_kinds={
                str(field): ReferenceValueKind(str(kind))
                for field, kind in dict(payload.get("value_kinds", {})).items()
            },
            entries=tuple(
                ReferenceEntry.from_dict(item) for item in payload.get("entries", ())
            ),
            owner=str(payload["owner"]),
            classification=str(payload["classification"]),
            effective_label=str(payload["effective_label"]),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Reference-data content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class ReferenceBundle:
    """Deterministically bind all exact reference inputs for one preparation."""

    workspace_id: str
    datasets: tuple[ReferenceDataSet, ...]
    contract_version: int = REFERENCE_BUNDLE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _required_text(self.workspace_id, "workspace ID", 200)
        if self.contract_version != REFERENCE_BUNDLE_CONTRACT_VERSION:
            raise ValueError("Reference-bundle contract version is unsupported")
        if len(self.datasets) > MAX_REFERENCE_DATASETS:
            raise ValueError("Reference bundle exceeds the dataset limit")
        expected = tuple(
            sorted(self.datasets, key=lambda item: (item.reference_id, item.version))
        )
        if self.datasets != expected:
            raise ValueError("Reference datasets must use deterministic order")
        ids = [item.reference_id for item in self.datasets]
        if len(set(ids)) != len(ids):
            raise ValueError("A reference bundle cannot contain two versions of one list")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "datasets": [item.to_portable_dict() for item in self.datasets],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReferenceBundle":
        result = cls(
            contract_version=int(payload["contract_version"]),
            workspace_id=str(payload["workspace_id"]),
            datasets=tuple(
                ReferenceDataSet.from_dict(item)
                for item in payload.get("datasets", ())
            ),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Reference-bundle content hash is invalid")
        return result


def validate_odoo_selection_reference_outputs(
    bundle: ReferenceBundle,
    mapping_definition: Any,
    schema_catalog: Any,
) -> None:
    """Check governed selection outputs against one frozen Odoo schema.

    This verifies technical-key membership only. It does not contact Odoo and
    does not claim that the later rehearsal target still has the same schema.
    """

    if (
        bundle.workspace_id != schema_catalog.workspace_id
        or mapping_definition.schema_hash != schema_catalog.content_hash
    ):
        raise ValueError("Reference selection validation bindings are stale")
    references = {item.reference_id: item for item in bundle.datasets}
    models = {item.name: item for item in schema_catalog.models}
    for dataset_mapping in mapping_definition.datasets:
        model = models.get(dataset_mapping.target_model)
        fields = {item.name: item for item in (model.fields if model else ())}
        for scalar in dataset_mapping.fields:
            lookup = scalar.reference_lookup
            if lookup is None:
                continue
            reference = references.get(lookup.reference_id)
            if reference is None or reference.content_hash != lookup.reference_content_hash:
                raise ValueError("A mapping reference list is missing or has changed")
            kind = reference.value_kinds.get(lookup.value_field)
            if kind is not ReferenceValueKind.ODOO_SELECTION_KEY:
                continue
            target = fields.get(scalar.target_field)
            if target is None or target.type != "selection" or not target.selection:
                raise ValueError("An Odoo selection reference targets a non-selection field")
            permitted = {str(item[0]) for item in target.selection}
            invalid = tuple(
                entry.values[lookup.value_field]
                for entry in reference.entries
                if str(entry.values[lookup.value_field]) not in permitted
            )
            if invalid:
                raise ValueError(
                    "An approved reference list contains an unknown Odoo selection key"
                )


def _required_text(value: str, label: str, maximum: int) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} is required")
    if len(clean) > maximum:
        raise ValueError(f"{label} is too long")
    return clean


def _dataset_name(value: str) -> str:
    clean = _required_text(value, "coverage dataset", 200)
    if any(character in clean for character in ("\x00", "\r", "\n")):
        raise ValueError("Coverage dataset is invalid")
    return clean


def _hash(value: str, label: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a SHA-256 hash")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{label} is invalid") from error
