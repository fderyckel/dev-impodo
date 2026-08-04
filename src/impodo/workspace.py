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
from .connectors import MetadataSnapshot, RecordSnapshot
from .inspection import SourceFileCatalog, SourceInspectionError
from .mapping_semantics import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingCompiler,
    MappingRevision,
    MappingSemanticValidator,
    MappingSubmission,
    MappingTargetMode,
    MappingValidationResult,
    MappingValidationStatus,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    SchemaGovernance,
    mapping_issue_fingerprint,
)
from .models import target_identity_hash
from .projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectError,
    ProjectStatus,
)


_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TECHNICAL_MODEL = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")


class WorkspaceError(ProjectError):
    """Raised when a source-discovery or mapping workspace transition is invalid."""


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
    relation_field: str | None
    selection: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SchemaModel:
    name: str
    label: str
    fields: tuple[SchemaField, ...]


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
        return _canonical_json(asdict(self))

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
        return _canonical_json(asdict(self))

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
                                tuple(item) for item in field.get("selection", ())
                            ),
                        )
                        for field in model["fields"]
                    ),
                )
                for model in payload["models"]
            ),
            content_hash=payload["content_hash"],
            origin=SchemaOrigin(payload.get("origin", SchemaOrigin.LIVE_API)),
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
        return _canonical_json(
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
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...
    def save_source_selection(
        self,
        project_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None: ...
    def get_odoo_model_catalog(
        self, project_id: str
    ) -> OdooModelCatalog | None: ...
    def save_odoo_model_catalog(
        self,
        project_id: str,
        catalog: OdooModelCatalog,
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
    def get_schema_governance(
        self, project_id: str
    ) -> SchemaGovernance | None: ...
    def save_schema_governance(
        self,
        project_id: str,
        governance: SchemaGovernance,
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
    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None: ...
    def save_mapping_working_draft(
        self,
        project_id: str,
        draft: MappingWorkingDraft,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None: ...
    def get_mapping_revision(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingRevision | None: ...
    def list_mapping_revisions(
        self, project_id: str
    ) -> tuple[MappingRevision, ...]: ...
    def save_mapping_revision(
        self,
        project_id: str,
        revision: MappingRevision,
        *,
        validation: MappingValidationResult,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None: ...
    def get_mapping_validation(
        self,
        project_id: str,
        version: int,
    ) -> MappingValidationResult | None: ...
    def save_mapping_validation(
        self,
        project_id: str,
        version: int,
        validation: MappingValidationResult,
        *,
        actor: Actor,
    ) -> None: ...
    def get_mapping_submission(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingSubmission | None: ...
    def save_mapping_submission(
        self,
        project_id: str,
        submission: MappingSubmission,
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

    def discover_models(
        self,
        project_id: str,
        snapshot: RecordSnapshot,
        *,
        actor: Actor,
    ) -> OdooModelCatalog:
        """Store concrete model choices returned by the connected Odoo."""

        self.authorization.require(
            actor, Capability.SCHEMA_DISCOVER, project_id=project_id
        )
        project = self.repository.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before discovering Odoo models"
            )
        if project.odoo_connection_mode is None:
            raise WorkspaceError("Configure the Odoo target before discovering models")
        if not snapshot.complete:
            raise WorkspaceError("Odoo model discovery response is incomplete")
        if snapshot.fingerprint.target_hash != _target_identity_hash(project):
            raise WorkspaceError("Odoo model target does not match the project")
        if snapshot.fingerprint.connection_mode != project.odoo_connection_mode.value:
            raise WorkspaceError("Odoo model connection mode does not match the project")
        if snapshot.fingerprint.database != project.odoo_database:
            raise WorkspaceError("Odoo model database does not match the project")
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo model discovery requires Odoo 19")
        if set(snapshot.records) != {"ir.model"}:
            raise WorkspaceError("Odoo model discovery returned an unexpected model")

        models: list[OdooModelSummary] = []
        seen: set[str] = set()
        for record in snapshot.records["ir.model"]:
            values = record.values
            if bool(values.get("abstract")) or bool(values.get("transient")):
                continue
            name = str(values.get("model") or "").strip()
            label = str(values.get("name") or name).strip()
            if not _TECHNICAL_MODEL.fullmatch(name):
                raise WorkspaceError("Odoo model discovery returned an invalid name")
            if not label:
                raise WorkspaceError(f"Odoo model {name} has no label")
            if name in seen:
                raise WorkspaceError(f"Odoo model {name} was returned more than once")
            seen.add(name)
            models.append(
                OdooModelSummary(
                    name=name,
                    label=label,
                    modules=_split_module_names(values.get("modules")),
                    state=str(values.get("state") or "base"),
                )
            )
        if not models:
            raise WorkspaceError(
                "No persistent Odoo models are visible through this connected "
                "read-only metadata boundary"
            )
        ordered = tuple(
            sorted(models, key=lambda item: (item.label.casefold(), item.name))
        )
        target_hash = _target_identity_hash(project)
        content = {
            "target_hash": target_hash,
            "fingerprint": snapshot.fingerprint.portable_dict(),
            "models": [asdict(model) for model in ordered],
        }
        catalog = OdooModelCatalog(
            project_id=project_id,
            target_hash=target_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            connection_mode=snapshot.fingerprint.connection_mode,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            models=ordered,
            content_hash=_content_hash(content),
        )
        self.repository.save_odoo_model_catalog(
            project_id,
            catalog,
            actor=actor,
        )
        return catalog

    def capture(
        self,
        project_id: str,
        snapshot: MetadataSnapshot,
        *,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Capture a verified catalog through the connected read-only reader."""

        project, permitted = self._capture_context(project_id, actor=actor)
        if not snapshot.complete:
            raise WorkspaceError("Odoo schema response is incomplete")
        if set(snapshot.models) != permitted:
            raise WorkspaceError("Odoo schema response does not match permitted models")
        if snapshot.fingerprint.target_hash != _target_identity_hash(project):
            raise WorkspaceError("Odoo schema target does not match the project")
        if snapshot.fingerprint.connection_mode != project.odoo_connection_mode.value:
            raise WorkspaceError("Odoo schema connection mode does not match the project")
        if snapshot.fingerprint.database != project.odoo_database:
            raise WorkspaceError("Odoo schema database does not match the project")
        if not snapshot.fingerprint.odoo_version.startswith("19."):
            raise WorkspaceError("Odoo schema capture requires Odoo 19")
        discovered = self.repository.get_odoo_model_catalog(project_id)
        discovered_labels = (
            {model.name: model.label for model in discovered.models}
            if discovered and discovered.target_hash == _target_identity_hash(project)
            else {}
        )
        missing_discovered = permitted - set(discovered_labels)
        if discovered and missing_discovered:
            missing = sorted(missing_discovered)[0]
            raise WorkspaceError(
                f"{missing} is no longer in the refreshed Odoo model catalogue; "
                "save the permitted model scope again"
            )
        models = tuple(
            SchemaModel(
                name=name,
                label=discovered_labels.get(name) or model.description or name,
                fields=tuple(
                    SchemaField(
                        name=field_name,
                        label=field.label or field_name,
                        type=field.type,
                        required=field.required,
                        readonly=field.readonly,
                        relation=field.relation,
                        relation_field=field.relation_field,
                        selection=field.selection,
                    )
                    for field_name, field in sorted(model.fields.items())
                ),
            )
            for name, model in sorted(snapshot.models.items())
        )
        self._validate_schema_models(models, permitted)
        return self._store_catalog(
            project,
            models=models,
            connection_mode=snapshot.fingerprint.connection_mode,
            database=snapshot.fingerprint.database,
            odoo_version=snapshot.fingerprint.odoo_version,
            fingerprint=snapshot.fingerprint.portable_dict(),
            origin=SchemaOrigin.LIVE_API,
            actor=actor,
        )

    def capture_local_manual(
        self,
        project_id: str,
        models: Iterable[SchemaModel],
        *,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Store an explicitly unverified schema draft for local work.

        This deliberately does not contact Odoo or accept any alternate
        credential. A later authenticated capture replaces the draft and
        invalidates its dependent governance and mapping evidence.
        """

        project, permitted = self._capture_context(project_id, actor=actor)
        if project.odoo_connection_mode is not OdooConnectionMode.LOCAL:
            raise WorkspaceError(
                "A manual schema draft is available only for Local Odoo"
            )
        declared_models = tuple(sorted(models, key=lambda item: item.name))
        self._validate_schema_models(declared_models, permitted)
        return self._store_catalog(
            project,
            models=declared_models,
            connection_mode=project.odoo_connection_mode.value,
            database=project.odoo_database,
            odoo_version="unverified local draft (expected Odoo 19)",
            fingerprint={
                "target_hash": _target_identity_hash(project),
                "connection_mode": project.odoo_connection_mode.value,
                "database": project.odoo_database,
                "odoo_version": "unverified local draft (expected Odoo 19)",
                "snapshot_timestamp": "not captured",
                "module_versions": {},
            },
            origin=SchemaOrigin.LOCAL_MANUAL,
            actor=actor,
        )

    def _capture_context(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[MigrationProject, set[str]]:
        self.authorization.require(
            actor, Capability.SCHEMA_DISCOVER, project_id=project_id
        )
        project = self.repository.get(project_id)
        if self.repository.get_source_selection(project_id) is None:
            raise WorkspaceError("Freeze source datasets before capturing Odoo schema")
        permitted = set(project.intended_models)
        if not permitted:
            raise WorkspaceError(
                "Add at least one permitted technical Odoo model to the project"
            )
        if project.odoo_connection_mode is None:
            raise WorkspaceError("Configure the Odoo target before capturing schema")
        return project, permitted

    @staticmethod
    def _validate_schema_models(
        models: tuple[SchemaModel, ...],
        permitted: set[str],
    ) -> None:
        if {model.name for model in models} != permitted:
            raise WorkspaceError("Schema models do not match the permitted scope")
        if len(models) != len(permitted):
            raise WorkspaceError("Schema models must be unique")
        if any(not model.label or not model.fields for model in models):
            raise WorkspaceError("Each permitted model must have a label and field")
        for model in models:
            names = [field.name for field in model.fields]
            if len(names) != len(set(names)):
                raise WorkspaceError(
                    f"Schema fields for {model.name} must be unique"
                )
            if any(
                not field.name or not field.label or not field.type
                for field in model.fields
            ):
                raise WorkspaceError(
                    f"Schema fields for {model.name} need a name, label, and type"
                )

    def _store_catalog(
        self,
        project: MigrationProject,
        *,
        models: tuple[SchemaModel, ...],
        connection_mode: str,
        database: str,
        odoo_version: str,
        fingerprint: Mapping[str, object],
        origin: SchemaOrigin,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        target_hash = _content_hash(
            {
                "mode": (
                    project.odoo_connection_mode.value
                    if project.odoo_connection_mode
                    else None
                ),
                "url": project.odoo_base_url,
                "database": project.odoo_database,
                "models": sorted(model.name for model in models),
            }
        )
        content = {
            "target_hash": target_hash,
            "fingerprint": fingerprint,
            "origin": origin.value,
            "models": [asdict(model) for model in models],
        }
        catalog = OdooSchemaCatalog(
            project_id=project.project_id,
            target_hash=target_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=actor.identity.display_name,
            connection_mode=connection_mode,
            database=database,
            odoo_version=odoo_version,
            models=models,
            content_hash=_content_hash(content),
            origin=origin,
        )
        self.repository.save_odoo_schema_catalog(
            project.project_id,
            catalog,
            actor=actor,
        )
        return catalog

    def govern(
        self,
        project_id: str,
        *,
        business_keys: Iterable[BusinessKeyDefinition],
        actor: Actor,
    ) -> SchemaGovernance:
        """Confirm explicit natural keys for the current captured schema."""

        self.authorization.require(
            actor, Capability.SCHEMA_GOVERN, project_id=project_id
        )
        schema = self.repository.get_odoo_schema_catalog(project_id)
        if schema is None:
            raise WorkspaceError("Capture the Odoo schema before confirming keys")
        models = {model.name: model for model in schema.models}
        normalized = tuple(
            sorted(
                business_keys,
                key=lambda item: (
                    item.model,
                    item.key_fields,
                    item.scope_fields,
                    item.key_id,
                ),
            )
        )
        if not normalized:
            raise WorkspaceError("Confirm at least one governed business key")
        seen_ids: set[str] = set()
        seen_shapes: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
        for definition in normalized:
            if definition.key_id in seen_ids:
                raise WorkspaceError("Business-key IDs must be unique")
            seen_ids.add(definition.key_id)
            shape = (
                definition.model,
                definition.key_fields,
                definition.scope_fields,
            )
            if shape in seen_shapes:
                raise WorkspaceError("Business-key definitions must be unique")
            seen_shapes.add(shape)
            model = models.get(definition.model)
            if model is None:
                raise WorkspaceError(
                    f"Business-key model {definition.model} is not captured"
                )
            available = {field.name for field in model.fields}
            missing = [
                item
                for item in (*definition.key_fields, *definition.scope_fields)
                if item not in available
            ]
            if missing:
                raise WorkspaceError(
                    f"Business-key field {definition.model}.{missing[0]} "
                    "is not captured"
                )
        previous = self.repository.get_schema_governance(project_id)
        governance = SchemaGovernance(
            governance_id=(
                previous.governance_id if previous else str(uuid4())
            ),
            version=previous.version + 1 if previous else 1,
            project_id=project_id,
            catalog_hash=schema.content_hash,
            permitted_models=tuple(sorted(models)),
            business_keys=normalized,
            recorded_at=datetime.now(timezone.utc),
            recorded_by=actor.identity.display_name,
        )
        self.repository.save_schema_governance(
            project_id, governance, actor=actor
        )
        return governance


class MappingWorkspaceService:
    def __init__(
        self,
        repository: WorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.compiler = MappingCompiler()
        self.validator = MappingSemanticValidator()

    def save_working_draft(
        self,
        project_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_version: int | None,
        actor: Actor,
    ) -> MappingWorkingDraft:
        """Persist incomplete browser work without semantic validation."""

        self.authorization.require(
            actor, Capability.MAPPING_EDIT, project_id=project_id
        )
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        governance = self.repository.get_schema_governance(project_id)
        if selection is None or schema is None:
            raise WorkspaceError("Freeze datasets and capture Odoo schema first")
        current = self.repository.get_mapping_revision(project_id)
        existing = self.repository.get_mapping_working_draft(project_id)
        actual_version = existing.version if existing else None
        if expected_version != actual_version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )
        if existing is not None:
            mapping_id = existing.mapping_id
        elif current is not None:
            mapping_id = current.mapping_id
        else:
            mapping_id = str(uuid4())
        definition = MappingDefinition(
            mapping_id=mapping_id,
            source_selection_hash=selection.content_hash,
            schema_hash=(
                governance.content_hash
                if governance is not None
                else schema.content_hash
            ),
            datasets=tuple(datasets),
        )
        draft = MappingWorkingDraft(
            mapping_id=mapping_id,
            version=(actual_version or 0) + 1,
            project_id=project_id,
            base_mapping_version=current.version if current else None,
            definition=definition,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.repository.save_mapping_working_draft(
            project_id,
            draft,
            expected_version=expected_version,
            actor=actor,
        )
        return draft

    def save_definition(
        self,
        project_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_parent_version: int | None,
        submit: bool,
        warning_acknowledgements: Iterable[str] = (),
        actor: Actor,
    ) -> tuple[
        MappingRevision,
        MappingValidationResult,
        MappingSubmission | None,
    ]:
        """Save and validate one immutable dataset-centric mapping revision."""

        capability = (
            Capability.MAPPING_SUBMIT if submit else Capability.MAPPING_EDIT
        )
        self.authorization.require(actor, capability, project_id=project_id)
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        governance = self.repository.get_schema_governance(project_id)
        if selection is None or schema is None:
            raise WorkspaceError("Freeze datasets and capture Odoo schema first")
        if submit and schema.origin is SchemaOrigin.LOCAL_MANUAL:
            raise WorkspaceError(
                "Capture the live Odoo schema before submitting a mapping; "
                "the current local schema is unverified"
            )
        current = self.repository.get_mapping_revision(project_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The mapping was modified by another request; reload it"
            )
        working_draft = self.repository.get_mapping_working_draft(project_id)
        expected_schema_hash = (
            governance.content_hash
            if governance is not None
            else schema.content_hash
        )
        compatible_working_draft = (
            working_draft
            if working_draft is not None
            and working_draft.definition.source_selection_hash
            == selection.content_hash
            and working_draft.definition.schema_hash == expected_schema_hash
            else None
        )
        mapping_id = (
            current.mapping_id
            if current is not None
            else (
                compatible_working_draft.mapping_id
                if compatible_working_draft is not None
                else str(uuid4())
            )
        )
        definition = self.compiler.compile(
            MappingDefinition(
                mapping_id=mapping_id,
                source_selection_hash=selection.content_hash,
                schema_hash=(
                    governance.content_hash
                    if governance is not None
                    else schema.content_hash
                ),
                datasets=tuple(datasets),
            )
        ).definition
        validation = self.validator.validate(
            definition,
            selection,
            schema,
            governance,
        )
        warning_fingerprints = {
            mapping_issue_fingerprint(item)
            for item in validation.issues
            if item.severity == "warning"
        }
        acknowledgements = frozenset(warning_acknowledgements)
        historical_versions = self.repository.list_mapping_revisions(project_id)
        revision = MappingRevision(
            mapping_id=mapping_id,
            version=(
                max((item.version for item in historical_versions), default=0)
                + 1
            ),
            parent_version=actual_parent,
            definition=definition,
            created_at=datetime.now(timezone.utc),
            created_by=actor.identity.display_name,
        )
        self.repository.save_mapping_revision(
            project_id,
            revision,
            validation=validation,
            expected_parent_version=expected_parent_version,
            actor=actor,
        )
        if submit and validation.status is MappingValidationStatus.INVALID:
            first = next(
                item
                for item in validation.issues
                if item.severity == "error"
            )
            raise WorkspaceError(
                f"Mapping cannot be submitted: {first.message}"
            )
        if submit:
            missing = warning_fingerprints.difference(acknowledgements)
            if missing:
                raise WorkspaceError(
                    "Acknowledge every current validation warning before "
                    "submitting"
                )
        submission = None
        if submit:
            submission = MappingSubmission(
                submission_id=str(uuid4()),
                mapping_id=mapping_id,
                version=revision.version,
                mapping_content_hash=definition.content_hash,
                validation_hash=validation.validation_hash,
                warning_acknowledgements=tuple(
                    sorted(warning_fingerprints)
                ),
                submitted_at=datetime.now(timezone.utc),
                submitted_by=actor.identity.display_name,
            )
            self.repository.save_mapping_submission(
                project_id, submission, actor=actor
            )
        return revision, validation, submission

    def validate_current(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> MappingValidationResult:
        """Revalidate the current exact revision against current evidence."""

        self.authorization.require(
            actor, Capability.MAPPING_EDIT, project_id=project_id
        )
        revision = self.repository.get_mapping_revision(project_id)
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        governance = self.repository.get_schema_governance(project_id)
        if revision is None or selection is None or schema is None:
            raise WorkspaceError("Save a mapping revision before validating")
        validation = self.validator.validate(
            revision.definition,
            selection,
            schema,
            governance,
        )
        self.repository.save_mapping_validation(
            project_id,
            revision.version,
            validation,
            actor=actor,
        )
        return validation

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
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        if selection is None or schema is None:
            raise WorkspaceError("Freeze datasets and capture Odoo schema first")
        if submit and schema.origin is SchemaOrigin.LOCAL_MANUAL:
            raise WorkspaceError(
                "Capture the live Odoo schema before submitting a mapping; "
                "the current local schema is unverified"
            )
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


def _target_identity_hash(project: MigrationProject) -> str:
    return target_identity_hash(
        connection_mode=(
            project.odoo_connection_mode.value
            if project.odoo_connection_mode
            else ""
        ),
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )


def _split_module_names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value]
    else:
        candidates = [str(value)]
    return tuple(
        sorted(
            {
                candidate.strip()
                for candidate in candidates
                if candidate.strip()
            }
        )
    )


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
