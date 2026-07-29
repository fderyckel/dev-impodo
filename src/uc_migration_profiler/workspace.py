"""Governed source selection, Odoo schema, and mapping-draft contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable, Mapping, Protocol
from uuid import uuid4

from .access import Actor, AuthorizationPolicy, Capability
from .connectors import MetadataSnapshot
from .inspection import SourceFileCatalog, SourceInspectionError
from .projects import MigrationProject, ProjectError, ProjectStatus


_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class WorkspaceError(ProjectError):
    """Raised when a Phase B/Phase 2 workspace transition is invalid."""


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
        return _canonical_json(asdict(self))

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
    """Frozen, versioned source datasets consumed by mapping drafts."""

    selection_id: str
    version: int
    project_id: str
    created_at: datetime
    created_by: str
    datasets: tuple[SourceDataset, ...]
    content_hash: str

    def to_json(self) -> str:
        return _canonical_json(asdict(self))

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
    selection: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SchemaModel:
    name: str
    label: str
    fields: tuple[SchemaField, ...]


@dataclass(frozen=True, slots=True)
class OdooSchemaCatalog:
    """Read-only, permitted-model Odoo schema captured for mapping."""

    project_id: str
    target_hash: str
    captured_at: datetime
    captured_by: str
    environment: str
    database: str
    odoo_version: str
    models: tuple[SchemaModel, ...]
    content_hash: str

    def to_json(self) -> str:
        return _canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "OdooSchemaCatalog":
        payload = json.loads(value)
        return cls(
            project_id=payload["project_id"],
            target_hash=payload["target_hash"],
            captured_at=datetime.fromisoformat(payload["captured_at"]),
            captured_by=payload["captured_by"],
            environment=payload["environment"],
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
                            selection=tuple(
                                tuple(item) for item in field.get("selection", ())
                            ),
                        )
                        for field in model["fields"]
                    ),
                )
                for model in payload["models"]
            ),
            content_hash=payload["content_hash"],
        )


class MappingStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"


@dataclass(frozen=True, slots=True)
class FieldMapping:
    dataset_name: str
    source_column: str
    target_model: str
    target_field: str


@dataclass(frozen=True, slots=True)
class MappingDraft:
    mapping_id: str
    version: int
    status: MappingStatus
    source_selection_hash: str
    schema_hash: str
    updated_at: datetime
    updated_by: str
    entries: tuple[FieldMapping, ...]
    content_hash: str

    def to_json(self) -> str:
        return _canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> "MappingDraft":
        payload = json.loads(value)
        return cls(
            mapping_id=payload["mapping_id"],
            version=int(payload["version"]),
            status=MappingStatus(payload["status"]),
            source_selection_hash=payload["source_selection_hash"],
            schema_hash=payload["schema_hash"],
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            updated_by=payload["updated_by"],
            entries=tuple(
                FieldMapping(**item) for item in payload["entries"]
            ),
            content_hash=payload["content_hash"],
        )


class WorkspaceRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...
    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...
    def get_source_configurations(
        self, project_id: str
    ) -> tuple[SourceConfiguration, ...]: ...
    def save_source_configuration(
        self,
        project_id: str,
        configuration: SourceConfiguration,
        *,
        actor: Actor,
    ) -> None: ...
    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...
    def save_source_selection(
        self,
        project_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None: ...
    def get_odoo_schema_catalog(
        self, project_id: str
    ) -> OdooSchemaCatalog | None: ...
    def save_odoo_schema_catalog(
        self,
        project_id: str,
        catalog: OdooSchemaCatalog,
        *,
        actor: Actor,
    ) -> None: ...
    def get_mapping_draft(self, project_id: str) -> MappingDraft | None: ...
    def save_mapping_draft(
        self,
        project_id: str,
        draft: MappingDraft,
        *,
        actor: Actor,
    ) -> None: ...


class SourceWorkspaceService:
    def __init__(
        self,
        repository: WorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def confirm_source(
        self,
        project_id: str,
        file_id: str,
        *,
        selected_table_keys: Iterable[str],
        warnings_acknowledged: bool,
        actor: Actor,
    ) -> SourceConfiguration:
        self.authorization.require(
            actor, Capability.SOURCE_CONFIGURE, project_id=project_id
        )
        catalog = _catalog(self.repository, project_id, file_id)
        selected = tuple(dict.fromkeys(selected_table_keys))
        available = {table.table_key: table for table in catalog.tables}
        if not selected or any(key not in available for key in selected):
            raise WorkspaceError("Select at least one available source table")
        selected_tables = tuple(available[key] for key in selected)
        blocking = [
            warning
            for table in selected_tables
            for warning in table.warnings
            if "empty candidate header" in warning.casefold()
            or "duplicate candidate header" in warning.casefold()
        ]
        if blocking:
            raise WorkspaceError(blocking[0])
        warnings = [
            *catalog.warnings,
            *(warning for table in selected_tables for warning in table.warnings),
        ]
        if warnings and not warnings_acknowledged:
            raise WorkspaceError("Acknowledge source warnings before confirmation")
        configuration = SourceConfiguration(
            file_id=file_id,
            source_sha256=catalog.source_sha256,
            catalog_hash=catalog.content_hash,
            encoding=catalog.encoding,
            delimiter=catalog.delimiter,
            selected_table_keys=selected,
            warnings_acknowledged=warnings_acknowledged,
            confirmed_at=datetime.now(timezone.utc),
            confirmed_by=actor.identity.display_name,
        )
        self.repository.save_source_configuration(
            project_id, configuration, actor=actor
        )
        return configuration

    def freeze_selection(
        self,
        project_id: str,
        *,
        dataset_names: Mapping[tuple[str, str], str],
        actor: Actor,
    ) -> SourceSelection:
        self.authorization.require(
            actor, Capability.SOURCE_SELECT, project_id=project_id
        )
        project = self.repository.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            raise WorkspaceError("Register the project before selecting datasets")
        catalogs = {
            catalog.file_id: catalog
            for catalog in self.repository.get_source_catalogs(project_id)
        }
        configurations = self.repository.get_source_configurations(project_id)
        if len(configurations) != len(project.source_files):
            raise WorkspaceError("Confirm every source file before freezing datasets")
        datasets: list[SourceDataset] = []
        used_names: set[str] = set()
        for configuration in configurations:
            catalog = catalogs.get(configuration.file_id)
            if catalog is None or catalog.content_hash != configuration.catalog_hash:
                raise WorkspaceError("Source confirmation is stale; confirm it again")
            tables = {table.table_key: table for table in catalog.tables}
            for table_key in configuration.selected_table_keys:
                table = tables[table_key]
                name = dataset_names.get((catalog.file_id, table_key), "").strip()
                if not _DATASET_NAME.fullmatch(name):
                    raise WorkspaceError(
                        "Dataset names must use lowercase letters, digits, "
                        "and underscores"
                    )
                if name in used_names:
                    raise WorkspaceError("Dataset names must be unique")
                used_names.add(name)
                datasets.append(
                    SourceDataset(
                        dataset_id=_dataset_key(catalog.file_id, table.table_key),
                        name=name,
                        file_id=catalog.file_id,
                        table_key=table.table_key,
                        source_sha256=catalog.source_sha256,
                        catalog_hash=catalog.content_hash,
                        encoding=catalog.encoding,
                        delimiter=catalog.delimiter,
                        header_row=table.header_row or 1,
                        row_count=table.row_count,
                        columns=tuple(
                            SourceDatasetColumn(
                                ordinal=column.ordinal,
                                source_name=column.name,
                                stable_key=_column_key(column.ordinal, column.name),
                                candidate_type=column.candidate_type,
                            )
                            for column in table.columns
                        ),
                    )
                )
        if not datasets:
            raise WorkspaceError("Select at least one source dataset")
        previous = self.repository.get_source_selection(project_id)
        version = previous.version + 1 if previous else 1
        content = {
            "project_id": project_id,
            "version": version,
            "datasets": [asdict(item) for item in datasets],
        }
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=version,
            project_id=project_id,
            created_at=datetime.now(timezone.utc),
            created_by=actor.identity.display_name,
            datasets=tuple(datasets),
            content_hash=_content_hash(content),
        )
        self.repository.save_source_selection(project_id, selection, actor=actor)
        return selection


class SchemaWorkspaceService:
    def __init__(
        self,
        repository: WorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def capture(
        self,
        project_id: str,
        snapshot: MetadataSnapshot,
        *,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        self.authorization.require(
            actor, Capability.SCHEMA_DISCOVER, project_id=project_id
        )
        project = self.repository.get(project_id)
        if self.repository.get_source_selection(project_id) is None:
            raise WorkspaceError("Freeze source datasets before capturing Odoo schema")
        if not snapshot.complete:
            raise WorkspaceError("Odoo schema response is incomplete")
        permitted = set(project.intended_models)
        if not permitted:
            raise WorkspaceError(
                "Add at least one permitted technical Odoo model to the project"
            )
        if set(snapshot.models) != permitted:
            raise WorkspaceError("Odoo schema response does not match permitted models")
        if snapshot.fingerprint.environment != project.target_environment.value:
            raise WorkspaceError("Odoo schema environment does not match the project")
        if snapshot.fingerprint.database != project.odoo_database:
            raise WorkspaceError("Odoo schema database does not match the project")
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo schema capture requires Odoo 19")
        models = tuple(
            SchemaModel(
                name=name,
                label=model.description or name,
                fields=tuple(
                    SchemaField(
                        name=field_name,
                        label=field.label or field_name,
                        type=field.type,
                        required=field.required,
                        readonly=field.readonly,
                        relation=field.relation,
                        selection=field.selection,
                    )
                    for field_name, field in sorted(model.fields.items())
                ),
            )
            for name, model in sorted(snapshot.models.items())
        )
        if any(not model.fields for model in models):
            raise WorkspaceError("Odoo returned an empty permitted-model schema")
        target_hash = _content_hash(
            {
                "mode": (
                    project.odoo_connection_mode.value
                    if project.odoo_connection_mode
                    else None
                ),
                "environment": (
                    project.target_environment.value
                    if project.target_environment
                    else None
                ),
                "url": project.odoo_base_url,
                "database": project.odoo_database,
                "models": sorted(permitted),
            }
        )
        content = {
            "target_hash": target_hash,
            "fingerprint": snapshot.fingerprint.portable_dict(),
            "models": [asdict(model) for model in models],
        }
        catalog = OdooSchemaCatalog(
            project_id=project_id,
            target_hash=target_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            environment=snapshot.fingerprint.environment,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            models=models,
            content_hash=_content_hash(content),
        )
        self.repository.save_odoo_schema_catalog(project_id, catalog, actor=actor)
        return catalog


class MappingWorkspaceService:
    def __init__(
        self,
        repository: WorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def save(
        self,
        project_id: str,
        *,
        proposals: Iterable[FieldMapping],
        submit: bool,
        actor: Actor,
    ) -> MappingDraft:
        capability = (
            Capability.MAPPING_SUBMIT if submit else Capability.MAPPING_EDIT
        )
        self.authorization.require(actor, capability, project_id=project_id)
        selection = self.repository.get_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        if selection is None or schema is None:
            raise WorkspaceError("Freeze datasets and capture Odoo schema first")
        source_columns = {
            (dataset.name, column.source_name)
            for dataset in selection.datasets
            for column in dataset.columns
        }
        target_fields = {
            (model.name, field.name): field
            for model in schema.models
            for field in model.fields
        }
        entries = tuple(
            sorted(
                (
                    proposal
                    for proposal in proposals
                    if proposal.target_field.strip()
                ),
                key=lambda item: (
                    item.dataset_name,
                    item.source_column,
                    item.target_model,
                    item.target_field,
                ),
            )
        )
        source_pairs = [
            (entry.dataset_name, entry.source_column) for entry in entries
        ]
        target_pairs = [
            (entry.target_model, entry.target_field) for entry in entries
        ]
        if len(source_pairs) != len(set(source_pairs)):
            raise WorkspaceError("A source column can only be mapped once")
        if len(target_pairs) != len(set(target_pairs)):
            raise WorkspaceError("An Odoo target field can only be mapped once")
        for entry in entries:
            if (entry.dataset_name, entry.source_column) not in source_columns:
                raise WorkspaceError("Mapping references an unknown source column")
            target = target_fields.get((entry.target_model, entry.target_field))
            if target is None:
                raise WorkspaceError("Mapping references an unknown target field")
            if target.readonly:
                raise WorkspaceError(
                    f"Target field {entry.target_model}."
                    f"{entry.target_field} is readonly"
                )
        if submit and not entries:
            raise WorkspaceError("Map at least one field before submitting")
        previous = self.repository.get_mapping_draft(project_id)
        version = previous.version + 1 if previous else 1
        mapping_id = previous.mapping_id if previous else str(uuid4())
        status = MappingStatus.SUBMITTED if submit else MappingStatus.DRAFT
        content = {
            "mapping_id": mapping_id,
            "version": version,
            "status": status.value,
            "source_selection_hash": selection.content_hash,
            "schema_hash": schema.content_hash,
            "entries": [asdict(item) for item in entries],
        }
        draft = MappingDraft(
            mapping_id=mapping_id,
            version=version,
            status=status,
            source_selection_hash=selection.content_hash,
            schema_hash=schema.content_hash,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
            entries=entries,
            content_hash=_content_hash(content),
        )
        self.repository.save_mapping_draft(project_id, draft, actor=actor)
        return draft


def _catalog(
    repository: WorkspaceRepository,
    project_id: str,
    file_id: str,
) -> SourceFileCatalog:
    try:
        return next(
            catalog
            for catalog in repository.get_source_catalogs(project_id)
            if catalog.file_id == file_id
        )
    except StopIteration as error:
        raise SourceInspectionError(
            "Inspect the source file before configuring it"
        ) from error


def _column_key(ordinal: int, name: str) -> str:
    digest = sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"column:{ordinal}:{digest}"


def _dataset_key(file_id: str, table_key: str) -> str:
    digest = sha256(f"{file_id}\0{table_key}".encode("utf-8")).hexdigest()
    return f"dataset:{digest[:24]}"


def _content_hash(payload: object) -> str:
    return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    def default(value: object):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, StrEnum):
            return value.value
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    return json.dumps(
        payload,
        default=default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
