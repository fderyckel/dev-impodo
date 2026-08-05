"""Application service for governed source confirmation and selection."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Iterable, Mapping, Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..inspection import SourceFileCatalog, SourceInspectionError
from ..projects import MigrationProject, ProjectStatus
from ..workspace_contracts import (
    SourceConfiguration,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from ..workspace_errors import WorkspaceError
from ..domain.serialization import content_hash


_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class SourceWorkspaceRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...

    def get_source_catalogs(
        self,
        project_id: str,
    ) -> tuple[SourceFileCatalog, ...]: ...

    def get_source_configurations(
        self,
        project_id: str,
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


class SourceWorkspaceService:
    def __init__(
        self,
        repository: SourceWorkspaceRepository,
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
            actor,
            Capability.SOURCE_CONFIGURE,
            project_id=project_id,
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
            *(
                warning
                for table in selected_tables
                for warning in table.warnings
            ),
        ]
        if warnings and not warnings_acknowledged:
            raise WorkspaceError(
                "Acknowledge source warnings before confirmation"
            )
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
            project_id,
            configuration,
            actor=actor,
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
            actor,
            Capability.SOURCE_SELECT,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before selecting datasets"
            )
        catalogs = {
            catalog.file_id: catalog
            for catalog in self.repository.get_source_catalogs(project_id)
        }
        configurations = self.repository.get_source_configurations(project_id)
        if len(configurations) != len(project.source_files):
            raise WorkspaceError(
                "Confirm every source file before freezing datasets"
            )
        datasets: list[SourceDataset] = []
        used_names: set[str] = set()
        for configuration in configurations:
            catalog = catalogs.get(configuration.file_id)
            if (
                catalog is None
                or catalog.content_hash != configuration.catalog_hash
            ):
                raise WorkspaceError(
                    "Source confirmation is stale; confirm it again"
                )
            tables = {table.table_key: table for table in catalog.tables}
            for table_key in configuration.selected_table_keys:
                table = tables[table_key]
                name = dataset_names.get(
                    (catalog.file_id, table_key),
                    "",
                ).strip()
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
                        dataset_id=_dataset_key(
                            catalog.file_id,
                            table.table_key,
                        ),
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
                                stable_key=_column_key(
                                    column.ordinal,
                                    column.name,
                                ),
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
            content_hash=content_hash(content),
        )
        self.repository.save_source_selection(
            project_id,
            selection,
            actor=actor,
        )
        return selection


def _catalog(
    repository: SourceWorkspaceRepository,
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
