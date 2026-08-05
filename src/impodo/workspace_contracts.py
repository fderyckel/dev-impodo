"""Governed source, Odoo schema, and mapping working-state contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json

from .mapping_semantics import MappingDefinition
from .models import UniqueConstraintMetadata
from .workspace_serialization import canonical_json


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
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "SourceConfiguration":
        payload = json.loads(value)
        return cls(
            file_id=str(payload["file_id"]),
            source_sha256=str(payload["source_sha256"]),
            catalog_hash=str(payload["catalog_hash"]),
            encoding=payload.get("encoding"),
            delimiter=payload.get("delimiter"),
            selected_table_keys=tuple(payload["selected_table_keys"]),
            warnings_acknowledged=bool(payload["warnings_acknowledged"]),
            confirmed_at=datetime.fromisoformat(payload["confirmed_at"]),
            confirmed_by=str(payload["confirmed_by"]),
        )


@dataclass(frozen=True, slots=True)
class SourceDatasetColumn:
    ordinal: int
    source_name: str
    stable_key: str
    candidate_type: str


@dataclass(frozen=True, slots=True)
class SourceDataset:
    dataset_id: str
    name: str
    file_id: str
    table_key: str
    source_sha256: str
    catalog_hash: str
    encoding: str | None
    delimiter: str | None
    header_row: int
    row_count: int
    columns: tuple[SourceDatasetColumn, ...]


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Frozen, versioned source datasets consumed by governed mapping."""

    selection_id: str
    version: int
    project_id: str
    created_at: datetime
    created_by: str
    datasets: tuple[SourceDataset, ...]
    content_hash: str

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "SourceSelection":
        payload = json.loads(value)
        datasets = tuple(
            SourceDataset(
                dataset_id=item["dataset_id"],
                name=item["name"],
                file_id=item["file_id"],
                table_key=item["table_key"],
                source_sha256=item["source_sha256"],
                catalog_hash=item["catalog_hash"],
                encoding=item.get("encoding"),
                delimiter=item.get("delimiter"),
                header_row=int(item["header_row"]),
                row_count=int(item["row_count"]),
                columns=tuple(
                    SourceDatasetColumn(
                        ordinal=int(column["ordinal"]),
                        source_name=column["source_name"],
                        stable_key=column["stable_key"],
                        candidate_type=column["candidate_type"],
                    )
                    for column in item["columns"]
                ),
            )
            for item in payload["datasets"]
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


@dataclass(frozen=True, slots=True)
class SchemaField:
    name: str
    label: str
    type: str
    required: bool
    readonly: bool
    relation: str | None
    relation_field: str | None
    selection: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SchemaModel:
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
    target_hash: str
    captured_at: datetime
    captured_by: str
    connection_mode: str
    database: str
    odoo_version: str
    models: tuple[OdooModelSummary, ...]
    content_hash: str

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "OdooModelCatalog":
        payload = json.loads(value)
        return cls(
            project_id=payload["project_id"],
            target_hash=payload["target_hash"],
            captured_at=datetime.fromisoformat(payload["captured_at"]),
            captured_by=payload["captured_by"],
            connection_mode=payload["connection_mode"],
            database=payload["database"],
            odoo_version=payload["odoo_version"],
            models=tuple(
                OdooModelSummary(
                    name=model["name"],
                    label=model["label"],
                    modules=tuple(model.get("modules", ())),
                    state=model.get("state", "base"),
                )
                for model in payload["models"]
            ),
            content_hash=payload["content_hash"],
        )


class SchemaOrigin(StrEnum):
    """How the current schema catalog was obtained."""

    LIVE_API = "LIVE_API"
    LOCAL_MANUAL = "LOCAL_MANUAL"


@dataclass(frozen=True, slots=True)
class OdooSchemaCatalog:
    """Read-only, permitted-model Odoo schema captured for mapping."""

    project_id: str
    target_hash: str
    captured_at: datetime
    captured_by: str
    connection_mode: str
    database: str
    odoo_version: str
    models: tuple[SchemaModel, ...]
    content_hash: str
    origin: SchemaOrigin = SchemaOrigin.LIVE_API

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "OdooSchemaCatalog":
        payload = json.loads(value)
        return cls(
            project_id=payload["project_id"],
            target_hash=payload["target_hash"],
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
                            relation=field.get("relation"),
                            relation_field=field.get("relation_field"),
                            selection=tuple(
                                tuple(item)
                                for item in field.get("selection", ())
                            ),
                        )
                        for field in model["fields"]
                    ),
                    unique_constraints=tuple(
                        UniqueConstraintMetadata(
                            name=str(item["name"]),
                            definition=str(item["definition"]),
                        )
                        for item in model.get("unique_constraints", ())
                    ),
                )
                for model in payload["models"]
            ),
            content_hash=payload["content_hash"],
            origin=SchemaOrigin(payload.get("origin", SchemaOrigin.LIVE_API)),
        )


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
        return self.definition.content_hash

    def to_json(self) -> str:
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
