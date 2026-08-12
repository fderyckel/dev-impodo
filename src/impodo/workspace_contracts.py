"""Define portable workspace evidence shared by migration Stages B–D.

Layer: domain contracts at the package root.

The types form the handoff from confirmed source structure to frozen datasets,
then to target-bound Odoo schema catalogs and recoverable mapping work. They
are immutable, JSON-serializable, and contain no source rows, credentials, or
numeric Odoo IDs.

See ``docs/architecture/python-code-map.md``,
``docs/contracts/02-workspace.md``, and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json

from .domain.mapping.contracts import MappingDefinition
from .domain.source_binding import (
    SourceBinding,
    SourceOriginKind,
    source_binding_from_dict,
)
from .models import UniqueConstraintMetadata
from .domain.serialization import canonical_json


@dataclass(frozen=True, slots=True)
class SourceConfiguration:
    """Confirmed parsing and table choices for one source file catalog."""

    file_id: str
    source_sha256: str
    catalog_hash: str
    encoding: str | None
    delimiter: str | None
    selected_table_keys: tuple[str, ...]
    warnings_acknowledged: bool
    confirmed_at: datetime
    confirmed_by: str

    def to_json(self) -> str:
        """Serialize the complete hash-bound confirmation deterministically."""

        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "SourceConfiguration":
        """Restore one stored confirmation without reinterpreting its catalog."""

        payload = json.loads(value)
        return cls(
            file_id=str(payload["file_id"]),
            source_sha256=str(payload["source_sha256"]),
            catalog_hash=str(payload["catalog_hash"]),
            encoding=payload["encoding"],
            delimiter=payload["delimiter"],
            selected_table_keys=tuple(payload["selected_table_keys"]),
            warnings_acknowledged=bool(payload["warnings_acknowledged"]),
            confirmed_at=datetime.fromisoformat(payload["confirmed_at"]),
            confirmed_by=str(payload["confirmed_by"]),
        )


@dataclass(frozen=True, slots=True)
class SourceDatasetColumn:
    """Identify one frozen source column independently of its display name.

    ``ordinal`` plus ``stable_key`` are the mapping identity; ``source_name``
    remains human evidence and ``candidate_type`` is inspection guidance, not a
    guaranteed runtime type.
    """

    ordinal: int
    source_name: str
    stable_key: str
    candidate_type: str


@dataclass(frozen=True, slots=True)
class SourceDataset:
    """Bind one dataset and its columns to exact discriminated source evidence."""

    dataset_id: str
    name: str
    source: SourceBinding
    row_count: int
    columns: tuple[SourceDatasetColumn, ...]

    @property
    def origin(self) -> SourceOriginKind:
        return self.source.origin

    @property
    def source_evidence_hash(self) -> str:
        return self.source.source_evidence_hash

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "source": self.source.to_dict(),
            "row_count": self.row_count,
            "columns": [asdict(column) for column in self.columns],
        }


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Freeze the complete Stage B dataset set consumed by later stages.

    A new version replaces the current pointer but does not mutate historical
    mappings or runs. ``content_hash`` binds dataset identities, source/catalog
    hashes, parsing choices, row counts, and column contracts.
    """

    selection_id: str
    version: int
    project_id: str
    created_at: datetime
    created_by: str
    datasets: tuple[SourceDataset, ...]
    content_hash: str

    @property
    def origins(self) -> frozenset[SourceOriginKind]:
        return frozenset(dataset.origin for dataset in self.datasets)

    def to_json(self) -> str:
        """Serialize the full frozen selection as deterministic portable JSON."""

        return canonical_json(
            {
                "selection_id": self.selection_id,
                "version": self.version,
                "project_id": self.project_id,
                "created_at": self.created_at.isoformat(),
                "created_by": self.created_by,
                "datasets": [dataset.to_dict() for dataset in self.datasets],
                "content_hash": self.content_hash,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> "SourceSelection":
        """Restore a frozen selection without reopening source artifacts."""

        payload = json.loads(value)
        _require_exact_fields(
            payload,
            {
                "selection_id",
                "version",
                "project_id",
                "created_at",
                "created_by",
                "datasets",
                "content_hash",
            },
            "source selection",
        )
        datasets = tuple(
            _source_dataset_from_dict(item) for item in payload["datasets"]
        )
        return cls(
            selection_id=payload["selection_id"],
            version=int(payload["version"]),
            project_id=payload["project_id"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            created_by=payload["created_by"],
            datasets=datasets,
            content_hash=payload["content_hash"],
        )


def _source_dataset_from_dict(value: object) -> SourceDataset:
    _require_exact_fields(
        value,
        {"dataset_id", "name", "source", "row_count", "columns"},
        "source dataset",
    )
    assert isinstance(value, dict)
    return SourceDataset(
        dataset_id=value["dataset_id"],
        name=value["name"],
        source=source_binding_from_dict(value["source"]),
        row_count=int(value["row_count"]),
        columns=tuple(_source_column_from_dict(item) for item in value["columns"]),
    )


def _source_column_from_dict(value: object) -> SourceDatasetColumn:
    _require_exact_fields(
        value,
        {"ordinal", "source_name", "stable_key", "candidate_type"},
        "source column",
    )
    assert isinstance(value, dict)
    return SourceDatasetColumn(
        ordinal=int(value["ordinal"]),
        source_name=value["source_name"],
        stable_key=value["stable_key"],
        candidate_type=value["candidate_type"],
    )


def _require_exact_fields(
    value: object,
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"Stored {label} does not match the current contract")


@dataclass(frozen=True, slots=True)
class SchemaField:
    """Describe one captured Odoo field needed for mapping and validation."""

    name: str
    label: str
    type: str
    required: bool
    readonly: bool
    relation: str | None
    relation_field: str | None
    selection: tuple[tuple[str, str], ...]
    stored: bool | None = None
    computed: bool | None = None
    has_inverse: bool | None = None
    related: bool | None = None
    translated: bool | None = None
    company_dependent: bool | None = None
    searchable: bool | None = None
    sortable: bool | None = None
    exportable: bool | None = None
    digits: tuple[int, int] | None = None
    currency_field: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaModel:
    """Describe one permitted Odoo model and its captured field surface."""

    name: str
    label: str
    fields: tuple[SchemaField, ...]
    unique_constraints: tuple[UniqueConstraintMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class OdooModelSummary:
    """One concrete, persistent model advertised by the connected Odoo."""

    name: str
    label: str
    modules: tuple[str, ...]
    state: str


@dataclass(frozen=True, slots=True)
class OdooModelCatalog:
    """Lightweight model choices discovered from one exact Odoo target."""

    project_id: str
    connection_target_hash: str
    policy_hash: str
    captured_at: datetime
    captured_by: str
    connection_mode: str
    database: str
    odoo_version: str
    models: tuple[OdooModelSummary, ...]
    content_hash: str
    read_credential_binding_hash: str
    read_principal_hash: str
    read_permission_hash: str
    read_context_hash: str

    def to_json(self) -> str:
        """Serialize the target-bound persistent-model choices."""

        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "OdooModelCatalog":
        """Restore model choices without contacting the target again."""

        payload = json.loads(value)
        return cls(
            project_id=payload["project_id"],
            connection_target_hash=payload["connection_target_hash"],
            policy_hash=payload["policy_hash"],
            captured_at=datetime.fromisoformat(payload["captured_at"]),
            captured_by=payload["captured_by"],
            connection_mode=payload["connection_mode"],
            database=payload["database"],
            odoo_version=payload["odoo_version"],
            models=tuple(
                OdooModelSummary(
                    name=model["name"],
                    label=model["label"],
                    modules=tuple(model["modules"]),
                    state=model["state"],
                )
                for model in payload["models"]
            ),
            content_hash=payload["content_hash"],
            read_credential_binding_hash=payload["read_credential_binding_hash"],
            read_principal_hash=payload["read_principal_hash"],
            read_permission_hash=payload["read_permission_hash"],
            read_context_hash=payload["read_context_hash"],
        )


class SchemaOrigin(StrEnum):
    """How the current schema catalog was obtained."""

    LIVE_API = "LIVE_API"
    LOCAL_MANUAL = "LOCAL_MANUAL"


@dataclass(frozen=True, slots=True)
class OdooSchemaCatalog:
    """Hold the exact permitted-model Odoo schema captured for mapping.

    ``connection_target_hash`` binds the configured endpoint/database identity;
    ``content_hash`` is the exact schema-scope hash. ``origin`` distinguishes
    authenticated/live evidence from an explicitly unverified local draft that
    cannot support submission.
    """

    project_id: str
    policy_hash: str
    captured_at: datetime
    captured_by: str
    connection_mode: str
    database: str
    odoo_version: str
    models: tuple[SchemaModel, ...]
    content_hash: str
    origin: SchemaOrigin
    read_credential_binding_hash: str
    read_principal_hash: str
    read_permission_hash: str
    read_context_hash: str
    connection_target_hash: str

    def to_json(self) -> str:
        """Serialize the complete captured schema and provenance deterministically."""

        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "OdooSchemaCatalog":
        """Restore captured metadata without making another Odoo request."""

        payload = json.loads(value)
        return cls(
            project_id=payload["project_id"],
            policy_hash=payload["policy_hash"],
            captured_at=datetime.fromisoformat(payload["captured_at"]),
            captured_by=payload["captured_by"],
            connection_mode=payload["connection_mode"],
            database=payload["database"],
            odoo_version=payload["odoo_version"],
            models=tuple(
                SchemaModel(
                    name=model["name"],
                    label=model["label"],
                    fields=tuple(
                        SchemaField(
                            name=field["name"],
                            label=field["label"],
                            type=field["type"],
                            required=bool(field["required"]),
                            readonly=bool(field["readonly"]),
                            relation=field["relation"],
                            relation_field=field["relation_field"],
                            selection=tuple(
                                tuple(item)
                                for item in field["selection"]
                            ),
                            stored=_optional_bool(field["stored"]),
                            computed=_optional_bool(field["computed"]),
                            has_inverse=_optional_bool(field["has_inverse"]),
                            related=_optional_bool(field["related"]),
                            translated=_optional_bool(field["translated"]),
                            company_dependent=_optional_bool(
                                field["company_dependent"]
                            ),
                            searchable=_optional_bool(field["searchable"]),
                            sortable=_optional_bool(field["sortable"]),
                            exportable=_optional_bool(field["exportable"]),
                            digits=(
                                tuple(int(item) for item in field["digits"])
                                if field["digits"] is not None
                                else None
                            ),
                            currency_field=(
                                str(field["currency_field"])
                                if field["currency_field"] is not None
                                else None
                            ),
                        )
                        for field in model["fields"]
                    ),
                    unique_constraints=tuple(
                        UniqueConstraintMetadata(
                            name=str(item["name"]),
                            definition=str(item["definition"]),
                        )
                        for item in model["unique_constraints"]
                    ),
                )
                for model in payload["models"]
            ),
            content_hash=payload["content_hash"],
            origin=SchemaOrigin(payload["origin"]),
            read_credential_binding_hash=payload["read_credential_binding_hash"],
            read_principal_hash=payload["read_principal_hash"],
            read_permission_hash=payload["read_permission_hash"],
            read_context_hash=payload["read_context_hash"],
            connection_target_hash=payload["connection_target_hash"],
        )


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("Stored schema boolean metadata is invalid")
    return value
    return value


@dataclass(frozen=True, slots=True)
class MappingWorkingDraft:
    """Recoverable, potentially incomplete browser mapping state."""

    mapping_id: str
    version: int
    project_id: str
    base_mapping_version: int | None
    definition: MappingDefinition
    updated_at: datetime
    updated_by: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Working-draft version must be positive")
        if self.definition.mapping_id != self.mapping_id:
            raise ValueError("Working draft and definition IDs do not match")

    @property
    def content_hash(self) -> str:
        """Return the definition hash; editor metadata is not semantic content."""

        return self.definition.content_hash

    def to_json(self) -> str:
        """Serialize recoverable editor state with its semantic content hash."""

        return canonical_json(
            {
                "mapping_id": self.mapping_id,
                "version": self.version,
                "project_id": self.project_id,
                "base_mapping_version": self.base_mapping_version,
                "definition": self.definition.to_dict(),
                "content_hash": self.content_hash,
                "updated_at": self.updated_at.isoformat(),
                "updated_by": self.updated_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> "MappingWorkingDraft":
        """Restore editor state and reject a tampered definition hash."""

        payload = json.loads(value)
        definition = MappingDefinition.from_dict(payload["definition"])
        if payload.get("content_hash") != definition.content_hash:
            raise ValueError("Working-draft content hash is invalid")
        return cls(
            mapping_id=str(payload["mapping_id"]),
            version=int(payload["version"]),
            project_id=str(payload["project_id"]),
            base_mapping_version=(
                int(payload["base_mapping_version"])
                if payload.get("base_mapping_version") is not None
                else None
            ),
            definition=definition,
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            updated_by=str(payload["updated_by"]),
        )
