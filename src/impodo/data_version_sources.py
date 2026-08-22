"""Define Project-owned source packages and read-only workspace projections.

Phase M2 moves source-file identity, inspection catalogues, confirmed parsing,
logical dataset selection, and immutable snapshot references under one
DataVersion. A MigrationWorkspace stores only a bounded selection of dataset
references. It never copies the DataVersion database or acquires authority to
change the accepted package.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path, PurePath
from typing import BinaryIO, Mapping, Protocol
from uuid import uuid4

from .access import Actor, AuthorizationPolicy, Capability
from .artifacts import ArtifactStore, ArtifactStoreError
from .domain.serialization import canonical_json, content_hash
from .domain.source_binding import (
    FileSourceBinding,
    OdooSourceBinding,
    SourceBinding,
)
from .migration_foundation import (
    FaultInjector,
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from .source import SourceLoadError
from .source_worker import validate_source_file_isolated
from .workspace_contracts import SourceDataset, SourceDatasetColumn


class SourcePackageOrigin(StrEnum):
    FILE = "FILE"
    ODOO = "ODOO"


class SourcePackageState(StrEnum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"


MAX_DATA_VERSION_SOURCE_BYTES = 100 * 1024 * 1024
DATA_VERSION_SOURCE_CHUNK_BYTES = 1024 * 1024
DATA_VERSION_SOURCE_EXTENSIONS = frozenset({".csv", ".xlsx"})


class DataVersionSourceIntakeError(MigrationFoundationError):
    """Report unsafe source intake or artifact compensation failure."""


@dataclass(frozen=True, slots=True)
class SourcePackageFile:
    """Reference one immutable source artifact owned by a DataVersion."""

    file_id: str
    display_name: str
    storage_key: str
    size_bytes: int
    sha256: str
    received_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.file_id, "file_id")
        required_text(self.display_name, "display_name", maximum=500)
        required_text(self.storage_key, "storage_key", maximum=1_000)
        if self.size_bytes < 0:
            raise MigrationFoundationError("size_bytes is invalid")
        require_hash(self.sha256, "sha256")
        require_aware(self.received_at, "received_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "display_name": self.display_name,
            "file_id": self.file_id,
            "received_at": self.received_at.isoformat(),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "storage_key": self.storage_key,
        }


@dataclass(frozen=True, slots=True)
class SourcePackageCatalog:
    """Bind one inspection catalogue to the exact source file bytes."""

    file_id: str
    source_sha256: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        require_uuid(self.file_id, "file_id")
        require_hash(self.source_sha256, "source_sha256")
        _portable_mapping(self.payload, "catalog payload")

    @property
    def content_hash(self) -> str:
        return content_hash(self.payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "file_id": self.file_id,
            "payload": dict(self.payload),
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class SourcePackageConfiguration:
    """Record the accepted table and parsing choices for one catalogue."""

    file_id: str
    catalog_hash: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        require_uuid(self.file_id, "file_id")
        require_hash(self.catalog_hash, "catalog_hash")
        _portable_mapping(self.payload, "configuration payload")

    @property
    def content_hash(self) -> str:
        return content_hash(self.payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_hash": self.catalog_hash,
            "content_hash": self.content_hash,
            "file_id": self.file_id,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class SourcePackageDataset:
    """Bind one logical dataset to an immutable snapshot artifact."""

    dataset_id: str
    display_name: str
    source_file_ids: tuple[str, ...]
    source: SourceBinding
    row_count: int
    columns: tuple[SourceDatasetColumn, ...]
    schema_hash: str
    snapshot_hash: str
    snapshot_storage_key: str
    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        required_text(self.dataset_id, "dataset_id", maximum=300)
        required_text(self.display_name, "display_name", maximum=500)
        if len(set(self.source_file_ids)) != len(self.source_file_ids):
            raise MigrationFoundationError("Dataset source files are duplicated")
        for file_id in self.source_file_ids:
            require_uuid(file_id, "source_file_id")
        object.__setattr__(
            self,
            "source_file_ids",
            tuple(sorted(self.source_file_ids)),
        )
        if not self.columns:
            raise MigrationFoundationError(
                "A source dataset requires at least one mapped column"
            )
        object.__setattr__(
            self,
            "columns",
            tuple(sorted(self.columns, key=lambda item: item.ordinal)),
        )
        if any(item.ordinal < 1 for item in self.columns) or len(
            {item.ordinal for item in self.columns}
        ) != len(self.columns):
            raise MigrationFoundationError(
                "Source dataset column ordinals are invalid"
            )
        if len({item.stable_key for item in self.columns}) != len(
            self.columns
        ):
            raise MigrationFoundationError(
                "Source dataset columns contain duplicate identities"
            )
        for item in self.columns:
            required_text(item.source_name, "source column name", maximum=500)
            required_text(item.stable_key, "source column key", maximum=500)
            required_text(
                item.candidate_type,
                "source column candidate type",
                maximum=100,
            )
        if self.row_count < 0:
            raise MigrationFoundationError("row_count is invalid")
        require_hash(self.schema_hash, "schema_hash")
        if self.schema_hash != source_column_contract_hash(self.columns):
            raise MigrationFoundationError(
                "Source dataset schema hash does not match its columns"
            )
        require_hash(self.snapshot_hash, "snapshot_hash")
        required_text(
            self.snapshot_storage_key,
            "snapshot_storage_key",
            maximum=1_000,
        )
        _portable_mapping(self.manifest, "snapshot manifest")

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "display_name": self.display_name,
            "manifest": dict(self.manifest),
            "row_count": self.row_count,
            "schema_hash": self.schema_hash,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_storage_key": self.snapshot_storage_key,
            "source": self.source.to_dict(),
            "source_file_ids": list(self.source_file_ids),
            "columns": [
                {
                    "candidate_type": item.candidate_type,
                    "ordinal": item.ordinal,
                    "source_name": item.source_name,
                    "stable_key": item.stable_key,
                }
                for item in self.columns
            ],
        }

    def to_mapping_dataset(self) -> SourceDataset:
        """Return the exact dataset contract consumed by mapping services."""

        return SourceDataset(
            dataset_id=self.dataset_id,
            name=self.display_name,
            source=self.source,
            row_count=self.row_count,
            columns=self.columns,
        )


@dataclass(frozen=True, slots=True)
class DataVersionSourcePackage:
    """Own the complete source package assembled for one DataVersion."""

    data_version_id: str
    project_id: str
    revision: int
    origin: SourcePackageOrigin
    state: SourcePackageState
    files: tuple[SourcePackageFile, ...]
    catalogs: tuple[SourcePackageCatalog, ...]
    configurations: tuple[SourcePackageConfiguration, ...]
    datasets: tuple[SourcePackageDataset, ...]
    updated_at: datetime
    frozen_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.data_version_id, "data_version_id")
        require_uuid(self.project_id, "project_id")
        require_revision(self.revision, "package_revision")
        object.__setattr__(self, "origin", SourcePackageOrigin(self.origin))
        object.__setattr__(self, "state", SourcePackageState(self.state))
        object.__setattr__(
            self,
            "files",
            tuple(sorted(self.files, key=lambda item: item.file_id)),
        )
        object.__setattr__(
            self,
            "catalogs",
            tuple(sorted(self.catalogs, key=lambda item: item.file_id)),
        )
        object.__setattr__(
            self,
            "configurations",
            tuple(sorted(self.configurations, key=lambda item: item.file_id)),
        )
        object.__setattr__(
            self,
            "datasets",
            tuple(sorted(self.datasets, key=lambda item: item.dataset_id)),
        )
        require_aware(self.updated_at, "updated_at")
        if self.frozen_at is not None:
            require_aware(self.frozen_at, "frozen_at")
        self._validate_membership()

    @property
    def content_hash(self) -> str:
        return content_hash(
            {
                "catalogs": [item.to_dict() for item in self.catalogs],
                "configurations": [
                    item.to_dict() for item in self.configurations
                ],
                "data_version_id": self.data_version_id,
                "datasets": [item.to_dict() for item in self.datasets],
                "files": [item.to_dict() for item in self.files],
                "origin": self.origin.value,
                "project_id": self.project_id,
            }
        )

    def dataset(self, dataset_id: str) -> SourcePackageDataset:
        for dataset in self.datasets:
            if dataset.dataset_id == dataset_id:
                return dataset
        raise MigrationFoundationError("Source package dataset not found")

    def _validate_membership(self) -> None:
        files = _unique(self.files, "file_id", "Source files")
        catalogs = _unique(self.catalogs, "file_id", "Source catalogs")
        configurations = _unique(
            self.configurations,
            "file_id",
            "Source configurations",
        )
        _unique(self.datasets, "dataset_id", "Source datasets")
        file_ids = set(files)
        if self.origin is SourcePackageOrigin.FILE:
            if not set(catalogs).issubset(file_ids):
                raise MigrationFoundationError(
                    "A source catalogue references a file outside its DataVersion"
                )
        elif files or catalogs or configurations:
            raise MigrationFoundationError(
                "An Odoo source package cannot contain file evidence"
            )
        if not set(configurations).issubset(catalogs):
            raise MigrationFoundationError(
                "A source confirmation requires an inspection catalogue"
            )
        for file_id, catalog in catalogs.items():
            if catalog.source_sha256 != files[file_id].sha256:
                raise MigrationFoundationError(
                    "Source catalogue does not match its file"
                )
        for file_id, configuration in configurations.items():
            if configuration.catalog_hash != catalogs[file_id].content_hash:
                raise MigrationFoundationError(
                    "Source confirmation does not match its catalogue"
                )
        for dataset in self.datasets:
            if not set(dataset.source_file_ids).issubset(file_ids):
                raise MigrationFoundationError(
                    "Source dataset references a file outside its DataVersion"
                )
            if (
                self.origin is SourcePackageOrigin.FILE
                and not dataset.source_file_ids
            ):
                raise MigrationFoundationError(
                    "A file dataset requires source-file evidence"
                )
            if (
                isinstance(dataset.source, FileSourceBinding)
                and dataset.source_file_ids != (dataset.source.file_id,)
            ):
                raise MigrationFoundationError(
                    "File dataset evidence does not match its source binding"
                )
            if isinstance(dataset.source, FileSourceBinding):
                binding = dataset.source
                file = files.get(binding.file_id)
                catalog = catalogs.get(binding.file_id)
                configuration = configurations.get(binding.file_id)
                if (
                    file is None
                    or catalog is None
                    or configuration is None
                    or binding.source_sha256 != file.sha256
                    or binding.catalog_hash != catalog.content_hash
                ):
                    raise MigrationFoundationError(
                        "File dataset binding does not match package evidence"
                    )
            if (
                self.origin is SourcePackageOrigin.FILE
                and isinstance(dataset.source, OdooSourceBinding)
            ):
                raise MigrationFoundationError(
                    "A file package cannot contain an Odoo source binding"
                )
            if (
                self.origin is SourcePackageOrigin.ODOO
                and dataset.source_file_ids
            ):
                raise MigrationFoundationError(
                    "An Odoo dataset cannot reference a source file"
                )
            if (
                self.origin is SourcePackageOrigin.ODOO
                and isinstance(dataset.source, FileSourceBinding)
            ):
                raise MigrationFoundationError(
                    "An Odoo package cannot contain a file source binding"
                )
        if self.state is SourcePackageState.FROZEN:
            if self.frozen_at is None:
                raise MigrationFoundationError(
                    "A frozen source package requires a freeze time"
                )
            self.require_acceptance_ready()
        elif self.frozen_at is not None:
            raise MigrationFoundationError(
                "A draft source package cannot have a freeze time"
            )

    def require_acceptance_ready(self) -> None:
        """Reject an incomplete draft before DataVersion acceptance."""

        if not self.datasets:
            raise MigrationFoundationError(
                "A source package requires at least one frozen dataset"
            )
        if self.origin is SourcePackageOrigin.FILE:
            file_ids = {item.file_id for item in self.files}
            if not file_ids:
                raise MigrationFoundationError(
                    "A file source package requires at least one file"
                )
            if (
                {item.file_id for item in self.catalogs} != file_ids
                or {item.file_id for item in self.configurations} != file_ids
            ):
                raise MigrationFoundationError(
                    "Every source file requires one catalogue and confirmation"
                )


@dataclass(frozen=True, slots=True)
class WorkspaceSourceProjection:
    """Select immutable DataVersion datasets for one MigrationWorkspace."""

    projection_id: str
    workspace_id: str
    project_id: str
    data_version_id: str
    package_hash: str
    datasets: tuple[SourcePackageDataset, ...]
    created_at: datetime
    created_by: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.projection_id, "projection_id"),
            (self.workspace_id, "workspace_id"),
            (self.project_id, "project_id"),
            (self.data_version_id, "data_version_id"),
        ):
            require_uuid(value, name)
        require_hash(self.package_hash, "package_hash")
        if not self.datasets:
            raise MigrationFoundationError(
                "A workspace source projection requires at least one dataset"
            )
        object.__setattr__(
            self,
            "datasets",
            tuple(sorted(self.datasets, key=lambda item: item.dataset_id)),
        )
        _unique(self.datasets, "dataset_id", "Projected datasets")
        require_aware(self.created_at, "created_at")
        required_text(self.created_by, "created_by", maximum=500)


class DataVersionSourceRepository(Protocol):
    def data_version_project_id(self, data_version_id: str) -> str: ...

    def get_source_package(
        self,
        data_version_id: str,
    ) -> DataVersionSourcePackage | None: ...

    def replace_draft_source_package(
        self,
        package: DataVersionSourcePackage,
        *,
        expected_package_revision: int | None,
        actor: Actor,
    ) -> DataVersionSourcePackage: ...

    def freeze_source_package(
        self,
        data_version_id: str,
        *,
        expected_data_version_revision: int,
        expected_package_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersionSourcePackage: ...


class WorkspaceSourceProjectionRepository(Protocol):
    def workspace_project_id(self, workspace_id: str) -> str: ...

    def create_workspace_source_projection(
        self,
        workspace_id: str,
        *,
        dataset_ids: tuple[str, ...],
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> WorkspaceSourceProjection: ...

    def get_workspace_source_projection(
        self,
        workspace_id: str,
    ) -> WorkspaceSourceProjection | None: ...


class DataVersionSourceIntakeService:
    """Store bounded file bytes inside one draft DataVersion boundary."""

    def __init__(
        self,
        repository: DataVersionSourceRepository,
        authorization: AuthorizationPolicy,
        artifacts: ArtifactStore,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.artifacts = artifacts

    def accept(
        self,
        data_version_id: str,
        *,
        actor: Actor,
        expected_package_revision: int | None,
        display_name: str,
        stream: BinaryIO,
    ) -> DataVersionSourcePackage:
        """Validate, store, and register one immutable CSV or XLSX file."""

        data_version_id = require_uuid(data_version_id, "data_version_id")
        project_id = self._authorize(data_version_id, actor)
        current = self.repository.get_source_package(data_version_id)
        if current is not None and (
            current.state is not SourcePackageState.DRAFT
            or current.origin is not SourcePackageOrigin.FILE
        ):
            raise DataVersionSourceIntakeError(
                "Source files require a draft file DataVersion"
            )
        safe_name = _safe_source_display_name(display_name)
        suffix = Path(safe_name).suffix.casefold()
        file_id = str(uuid4())
        try:
            stored = self.artifacts.store_source(
                data_version_id,
                artifact_id=file_id,
                suffix=suffix,
                stream=stream,
                maximum_bytes=MAX_DATA_VERSION_SOURCE_BYTES,
                chunk_bytes=DATA_VERSION_SOURCE_CHUNK_BYTES,
                validator=validate_source_file_isolated,
            )
            now = datetime.now(timezone.utc)
            source_file = SourcePackageFile(
                file_id=file_id,
                display_name=safe_name,
                storage_key=stored.storage_key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                received_at=now,
            )
            candidate = DataVersionSourcePackage(
                data_version_id=data_version_id,
                project_id=project_id,
                revision=(current.revision + 1 if current else 1),
                origin=SourcePackageOrigin.FILE,
                state=SourcePackageState.DRAFT,
                files=(current.files if current else ()) + (source_file,),
                catalogs=current.catalogs if current else (),
                configurations=current.configurations if current else (),
                datasets=current.datasets if current else (),
                updated_at=now,
            )
            try:
                return self.repository.replace_draft_source_package(
                    candidate,
                    expected_package_revision=expected_package_revision,
                    actor=actor,
                )
            except Exception:
                self.artifacts.delete_source(
                    data_version_id,
                    stored.storage_key,
                )
                raise
        except (ArtifactStoreError, SourceLoadError) as error:
            raise DataVersionSourceIntakeError(str(error)) from error

    def remove(
        self,
        data_version_id: str,
        file_id: str,
        *,
        actor: Actor,
        expected_package_revision: int,
    ) -> DataVersionSourcePackage:
        """Remove one draft file and its dependent draft source metadata."""

        data_version_id = require_uuid(data_version_id, "data_version_id")
        file_id = require_uuid(file_id, "file_id")
        self._authorize(data_version_id, actor)
        current = self.repository.get_source_package(data_version_id)
        if (
            current is None
            or current.state is not SourcePackageState.DRAFT
            or current.origin is not SourcePackageOrigin.FILE
        ):
            raise DataVersionSourceIntakeError(
                "Source files require a draft file DataVersion"
            )
        try:
            removed = next(item for item in current.files if item.file_id == file_id)
        except StopIteration as error:
            raise DataVersionSourceIntakeError("Source file not found") from error
        now = datetime.now(timezone.utc)
        candidate = replace(
            current,
            revision=current.revision + 1,
            files=tuple(item for item in current.files if item.file_id != file_id),
            catalogs=tuple(
                item for item in current.catalogs if item.file_id != file_id
            ),
            configurations=tuple(
                item for item in current.configurations if item.file_id != file_id
            ),
            datasets=tuple(
                item
                for item in current.datasets
                if file_id not in item.source_file_ids
            ),
            updated_at=now,
        )
        saved = self.repository.replace_draft_source_package(
            candidate,
            expected_package_revision=expected_package_revision,
            actor=actor,
        )
        try:
            self.artifacts.delete_source(
                data_version_id,
                removed.storage_key,
            )
        except ArtifactStoreError as error:
            raise DataVersionSourceIntakeError(
                "The source reference was removed, but its stored bytes could "
                "not be deleted"
            ) from error
        return saved

    def _authorize(self, data_version_id: str, actor: Actor) -> str:
        self.authorization.require(actor, Capability.DATA_VERSION_EDIT)
        project_id = self.repository.data_version_project_id(data_version_id)
        self.authorization.require(
            actor,
            Capability.DATA_VERSION_EDIT,
            project_id=project_id,
        )
        return project_id


class DataVersionSourcePackageService:
    """Authorize assembly and acceptance of one complete source package."""

    def __init__(
        self,
        repository: DataVersionSourceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def replace_draft(
        self,
        package: DataVersionSourcePackage,
        *,
        actor: Actor,
        expected_package_revision: int | None,
    ) -> DataVersionSourcePackage:
        self.authorization.require(actor, Capability.DATA_VERSION_EDIT)
        project_id = self.repository.data_version_project_id(
            package.data_version_id
        )
        if package.project_id != project_id:
            raise MigrationFoundationError(
                "Source package does not belong to this DataVersion"
            )
        self.authorization.require(
            actor,
            Capability.DATA_VERSION_EDIT,
            project_id=project_id,
        )
        if package.state is not SourcePackageState.DRAFT:
            raise MigrationFoundationError(
                "Only a draft source package can be replaced"
            )
        return self.repository.replace_draft_source_package(
            package,
            expected_package_revision=expected_package_revision,
            actor=actor,
        )

    def record_catalog(
        self,
        data_version_id: str,
        catalog: SourcePackageCatalog,
        *,
        actor: Actor,
        expected_package_revision: int,
    ) -> DataVersionSourcePackage:
        """Record one inspection result against exact draft file bytes."""

        current = self._draft(data_version_id, actor=actor)
        catalogs = {
            item.file_id: item for item in current.catalogs
        }
        catalogs[catalog.file_id] = catalog
        candidate = replace(
            current,
            revision=current.revision + 1,
            catalogs=tuple(catalogs.values()),
            configurations=tuple(
                item
                for item in current.configurations
                if item.file_id != catalog.file_id
            ),
            datasets=tuple(
                item
                for item in current.datasets
                if catalog.file_id not in item.source_file_ids
            ),
            updated_at=datetime.now(timezone.utc),
        )
        return self.replace_draft(
            candidate,
            actor=actor,
            expected_package_revision=expected_package_revision,
        )

    def confirm_configuration(
        self,
        data_version_id: str,
        configuration: SourcePackageConfiguration,
        *,
        actor: Actor,
        expected_package_revision: int,
    ) -> DataVersionSourcePackage:
        """Record accepted parsing and table choices for one catalogue."""

        current = self._draft(data_version_id, actor=actor)
        configurations = {
            item.file_id: item for item in current.configurations
        }
        configurations[configuration.file_id] = configuration
        candidate = replace(
            current,
            revision=current.revision + 1,
            configurations=tuple(configurations.values()),
            datasets=tuple(
                item
                for item in current.datasets
                if configuration.file_id not in item.source_file_ids
            ),
            updated_at=datetime.now(timezone.utc),
        )
        return self.replace_draft(
            candidate,
            actor=actor,
            expected_package_revision=expected_package_revision,
        )

    def replace_datasets(
        self,
        data_version_id: str,
        datasets: tuple[SourcePackageDataset, ...],
        *,
        actor: Actor,
        expected_package_revision: int,
    ) -> DataVersionSourcePackage:
        """Publish the current logical datasets and immutable snapshots."""

        current = self._draft(data_version_id, actor=actor)
        candidate = replace(
            current,
            revision=current.revision + 1,
            datasets=datasets,
            updated_at=datetime.now(timezone.utc),
        )
        return self.replace_draft(
            candidate,
            actor=actor,
            expected_package_revision=expected_package_revision,
        )

    def freeze(
        self,
        data_version_id: str,
        *,
        actor: Actor,
        expected_data_version_revision: int,
        expected_package_revision: int,
        operation_id: str,
        fault: FaultInjector | None = None,
    ) -> DataVersionSourcePackage:
        self.authorization.require(actor, Capability.DATA_VERSION_EDIT)
        current = self.repository.get_source_package(
            require_uuid(data_version_id, "data_version_id")
        )
        if current is None:
            raise MigrationFoundationError("Source package is not assembled")
        current.require_acceptance_ready()
        self.authorization.require(
            actor,
            Capability.DATA_VERSION_EDIT,
            project_id=current.project_id,
        )
        request_hash = content_hash(
            {
                "data_version_id": current.data_version_id,
                "package_hash": current.content_hash,
                "package_revision": expected_package_revision,
            }
        )
        return self.repository.freeze_source_package(
            current.data_version_id,
            expected_data_version_revision=require_revision(
                expected_data_version_revision,
                "expected_data_version_revision",
            ),
            expected_package_revision=require_revision(
                expected_package_revision,
                "expected_package_revision",
            ),
            operation_id=require_uuid(operation_id, "operation_id"),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

    def _draft(
        self,
        data_version_id: str,
        *,
        actor: Actor,
    ) -> DataVersionSourcePackage:
        self.authorization.require(actor, Capability.DATA_VERSION_EDIT)
        data_version_id = require_uuid(data_version_id, "data_version_id")
        project_id = self.repository.data_version_project_id(data_version_id)
        self.authorization.require(
            actor,
            Capability.DATA_VERSION_EDIT,
            project_id=project_id,
        )
        current = self.repository.get_source_package(
            data_version_id
        )
        if current is None or current.state is not SourcePackageState.DRAFT:
            raise MigrationFoundationError(
                "A draft source package is required"
            )
        return current


class WorkspaceSourceProjectionService:
    """Authorize one immutable, bounded DataVersion view for a workspace."""

    def __init__(
        self,
        repository: WorkspaceSourceProjectionRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def materialize(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        dataset_ids: tuple[str, ...],
        expected_workspace_revision: int,
        operation_id: str,
        fault: FaultInjector | None = None,
    ) -> WorkspaceSourceProjection:
        self.authorization.require(actor, Capability.MIGRATION_WORKSPACE_EDIT)
        workspace_id = require_uuid(workspace_id, "workspace_id")
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_EDIT,
            project_id=self.repository.workspace_project_id(workspace_id),
        )
        cleaned = tuple(
            sorted(
                required_text(item, "dataset_id", maximum=300)
                for item in dataset_ids
            )
        )
        if not cleaned or len(set(cleaned)) != len(cleaned):
            raise MigrationFoundationError(
                "Choose one or more distinct source datasets"
            )
        request_hash = content_hash(
            {
                "dataset_ids": list(cleaned),
                "workspace_id": workspace_id,
            }
        )
        return self.repository.create_workspace_source_projection(
            workspace_id,
            dataset_ids=cleaned,
            expected_workspace_revision=require_revision(
                expected_workspace_revision,
                "expected_workspace_revision",
            ),
            operation_id=require_uuid(operation_id, "operation_id"),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )


def _portable_mapping(value: Mapping[str, object], label: str) -> None:
    try:
        json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise MigrationFoundationError(f"{label} is not portable") from error


def _safe_source_display_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 255:
        raise DataVersionSourceIntakeError(
            "Source filename is missing or too long"
        )
    if PurePath(name).name != name or "/" in name or "\\" in name:
        raise DataVersionSourceIntakeError(
            "Source filename must not contain a path"
        )
    if any(ord(character) < 32 for character in name):
        raise DataVersionSourceIntakeError(
            "Source filename contains control characters"
        )
    if Path(name).suffix.casefold() not in DATA_VERSION_SOURCE_EXTENSIONS:
        raise DataVersionSourceIntakeError(
            "Only CSV and XLSX files are accepted"
        )
    return name


def source_column_contract_hash(
    columns: tuple[SourceDatasetColumn, ...],
) -> str:
    """Hash one ordered logical column contract for source and mapping use."""

    ordered = tuple(sorted(columns, key=lambda item: item.ordinal))
    return content_hash(
        [
            {
                "candidate_type": item.candidate_type,
                "ordinal": item.ordinal,
                "source_name": item.source_name,
                "stable_key": item.stable_key,
            }
            for item in ordered
        ]
    )


def _unique(
    values: tuple[object, ...],
    attribute: str,
    label: str,
) -> dict[str, object]:
    indexed = {str(getattr(item, attribute)): item for item in values}
    if len(indexed) != len(values):
        raise MigrationFoundationError(f"{label} contain duplicate identities")
    return indexed
