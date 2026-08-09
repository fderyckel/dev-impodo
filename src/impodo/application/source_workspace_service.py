"""Confirm Stage B source structure and freeze mapping-ready datasets.

Layer: application service.

After isolated inspection, ``SourceWorkspaceService.confirm_source`` binds
chosen tables and warning acknowledgement to an exact catalog.
``freeze_selection`` then creates stable dataset/column identities consumed by
schema, derived-entity, mapping, staging, and preflight workflows. It reads no
source bytes and performs no Odoo access.

See ``docs/architecture/python-code-map.md``,
``docs/contracts/02-workspace.md``, and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import re
from threading import RLock
from typing import Iterable, Mapping, Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..artifacts import ArtifactStore, ArtifactStoreError
from ..domain.source_snapshot import (
    SOURCE_READER_CONTRACT_VERSION,
    SourceSnapshot,
    source_snapshot_logical_hash,
)
from ..inspection import (
    SourceFileCatalog,
    SourceInspectionError,
    SourceTableCatalog,
)
from ..projects import MigrationProject, ProjectStatus
from ..source import SourceLoadError
from ..source_snapshot_io import (
    SourceSnapshotPublisher,
    source_snapshot_schema,
    validate_snapshot_for_dataset,
)
from ..workspace_contracts import (
    SourceConfiguration,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from ..workspace_errors import WorkspaceError
from ..domain.serialization import content_hash


_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class ProjectReader(Protocol):
    """Read the project lifecycle needed before dataset freezing."""

    def get(self, project_id: str) -> MigrationProject:
        """Return the project whose registration gates dataset freezing."""
        ...


class SourceWorkspaceRepository(Protocol):
    """Persist catalogs, confirmations, and the current frozen selection."""

    def get_source_catalogs(
        self,
        project_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        """Return the current hash-bound catalog for each registered file."""
        ...

    def get_source_configurations(
        self,
        project_id: str,
    ) -> tuple[SourceConfiguration, ...]:
        """Return confirmed table/parsing choices in registered-file order."""
        ...

    def save_source_configuration(
        self,
        project_id: str,
        configuration: SourceConfiguration,
        *,
        actor: Actor,
    ) -> None:
        """Persist one exact catalog confirmation and retire its dependents."""
        ...

    def get_source_selection(self, project_id: str) -> SourceSelection | None:
        """Return the current complete frozen selection, if one exists."""
        ...

    def save_source_selection(
        self,
        project_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None:
        """Publish one selection version and invalidate derived/mapping evidence."""
        ...

    def publish_source_selection_with_snapshots(
        self,
        project_id: str,
        selection: SourceSelection,
        snapshots: Iterable[SourceSnapshot],
        *,
        actor: Actor,
    ) -> None:
        """Atomically publish one selection and all of its snapshot pointers."""
        ...

    def find_source_snapshot(
        self,
        project_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> SourceSnapshot | None:
        """Return a registered matching logical snapshot, when available."""
        ...

    def source_snapshot_storage_keys(self, project_id: str) -> frozenset[str]:
        """Return every immutable snapshot path referenced by DuckDB."""
        ...


class SourceWorkspaceService:
    """Own Stage B confirmation and versioned dataset-freeze rules.

    Confirmation is per registered file and bound to its exact inspection
    catalog. Freezing requires every file to be confirmed, assigns stable
    logical keys, and publishes one complete ``SourceSelection`` version.
    """

    def __init__(
        self,
        projects: ProjectReader,
        sources: SourceWorkspaceRepository,
        authorization: AuthorizationPolicy,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.projects = projects
        self.sources = sources
        self.authorization = authorization
        self.artifacts = artifacts
        self.snapshot_publisher = (
            SourceSnapshotPublisher(artifacts) if artifacts is not None else None
        )
        self._snapshot_lock = RLock()

    def confirm_source(
        self,
        project_id: str,
        file_id: str,
        *,
        selected_table_keys: Iterable[str],
        warnings_acknowledged: bool,
        actor: Actor,
    ) -> SourceConfiguration:
        """Confirm selected tables and acknowledged warnings for one catalog.

        Blocking header problems cannot be acknowledged. Any repository update
        invalidates the older frozen selection and dependent active evidence.
        """

        self.authorization.require(
            actor,
            Capability.SOURCE_CONFIGURE,
            project_id=project_id,
        )
        catalog = _catalog(self.sources, project_id, file_id)
        selected = tuple(dict.fromkeys(selected_table_keys))
        available = {table.table_key: table for table in catalog.tables}
        if not selected or any(key not in available for key in selected):
            raise WorkspaceError("Select at least one available source table")
        selected_tables = tuple(available[key] for key in selected)
        selected_keys = set(selected)
        overlapping = next(
            (
                table
                for table in selected_tables
                if table.kind == "NAMED_TABLE"
                and f"sheet:{table.worksheet_name}" in selected_keys
            ),
            None,
        )
        if overlapping is not None:
            raise WorkspaceError(
                f"Choose either worksheet {overlapping.worksheet_name!r} or its "
                "Excel tables, not both"
            )
        unsafe_cell_problem = next(
            (
                problem
                for table in selected_tables
                if (problem := _unsafe_cell_problem(catalog, table)) is not None
            ),
            None,
        )
        if unsafe_cell_problem is not None:
            raise WorkspaceError(unsafe_cell_problem)
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
        self.sources.save_source_configuration(
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
        """Serialize snapshot publication and pointer advancement per process."""

        with self._snapshot_lock:
            return self._freeze_selection_locked(
                project_id,
                dataset_names=dataset_names,
                actor=actor,
            )

    def _freeze_selection_locked(
        self,
        project_id: str,
        *,
        dataset_names: Mapping[tuple[str, str], str],
        actor: Actor,
    ) -> SourceSelection:
        """Freeze all confirmed tables as one versioned logical selection.

        Dataset names must be unique stable identifiers. Every dataset retains
        the source/catalog hashes and stable ordinal-based column keys required
        to detect later replacement or reinterpretation.
        """

        self.authorization.require(
            actor,
            Capability.SOURCE_SELECT,
            project_id=project_id,
        )
        project = self.projects.get(project_id)
        if project.status is not ProjectStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before selecting datasets"
            )
        catalogs = {
            catalog.file_id: catalog
            for catalog in self.sources.get_source_catalogs(project_id)
        }
        configurations = self.sources.get_source_configurations(project_id)
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
        previous = self.sources.get_source_selection(project_id)
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
        if self.snapshot_publisher is None or self.artifacts is None:
            # Pure service tests keep the filesystem boundary absent. Production
            # composition always supplies it and therefore always publishes
            # snapshots before advancing the frozen-selection pointer.
            self.sources.save_source_selection(
                project_id,
                selection,
                actor=actor,
            )
            return selection

        source_files = {item.file_id: item for item in project.source_files}
        snapshots: list[SourceSnapshot] = []
        try:
            for dataset in selection.datasets:
                catalog = catalogs.get(dataset.file_id)
                source_file = source_files.get(dataset.file_id)
                if catalog is None or source_file is None:
                    raise WorkspaceError("Frozen source evidence is incomplete")
                schema = source_snapshot_schema(dataset)
                logical_hash = source_snapshot_logical_hash(
                    project_id=project_id,
                    dataset_id=dataset.dataset_id,
                    dataset_name=dataset.name,
                    file_id=dataset.file_id,
                    table_key=dataset.table_key,
                    source_sha256=(
                        "sha256:"
                        + dataset.source_sha256.removeprefix("sha256:")
                    ),
                    catalog_hash=dataset.catalog_hash,
                    physical_selection_hash=selection.content_hash,
                    reader_contract_version=SOURCE_READER_CONTRACT_VERSION,
                    schema_hash=schema.content_hash,
                    row_count=dataset.row_count,
                )
                existing = self.sources.find_source_snapshot(
                    project_id,
                    dataset.dataset_id,
                    logical_hash,
                )
                if existing is not None:
                    validate_snapshot_for_dataset(selection, dataset, existing)
                    with self.artifacts.materialize_source_snapshot(
                        project_id,
                        existing.parquet_storage_key,
                        expected_sha256=existing.parquet_sha256,
                    ):
                        pass
                    snapshots.append(existing)
                    continue
                snapshots.append(
                    self.snapshot_publisher.publish(
                        project,
                        selection,
                        dataset,
                        catalog,
                        source_file,
                    ).snapshot
                )
            self.sources.publish_source_selection_with_snapshots(
                project_id,
                selection,
                snapshots,
                actor=actor,
            )
        except WorkspaceError:
            self._cleanup_snapshot_orphans(project_id)
            raise
        except (ArtifactStoreError, SourceLoadError, OSError) as error:
            self._cleanup_snapshot_orphans(project_id)
            raise WorkspaceError(
                "Impodo could not create the immutable source snapshot: "
                f"{error}"
            ) from error
        except Exception as error:
            self._cleanup_snapshot_orphans(project_id)
            raise WorkspaceError(
                "Impodo could not publish the immutable source snapshot"
            ) from error
        self._cleanup_snapshot_orphans(project_id)
        return selection

    def _cleanup_snapshot_orphans(self, project_id: str) -> None:
        assert self.artifacts is not None
        referenced = self.sources.source_snapshot_storage_keys(project_id)
        self.artifacts.cleanup_source_snapshots(project_id, referenced)


def _unsafe_cell_problem(
    catalog: SourceFileCatalog,
    table: SourceTableCatalog,
) -> str | None:
    """Explain non-passive spreadsheet cells before they can be confirmed."""

    if table.formula_cell_count:
        kind = "Excel formula"
        count = table.formula_cell_count
        cell = table.first_formula_cell
        column = table.first_formula_column
    elif table.error_cell_count:
        kind = "Excel error"
        count = table.error_cell_count
        cell = table.first_error_cell
        column = table.first_error_column
    else:
        return None

    sheet = table.worksheet_name
    detail = f' in "{column}"' if column else ""
    if cell and sheet:
        detail += f" at {sheet}!{cell}"
    noun = "cell" if count == 1 else "cells"
    return (
        f'{kind} found{detail} in {catalog.display_name}. '
        f"This table contains {count} unsupported {noun}. Remove the formulas "
        "or errors, or replace them with fixed values before including this table."
    )


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
    """Derive a stable mapping key from column position plus its source name."""

    digest = sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"column:{ordinal}:{digest}"


def _dataset_key(file_id: str, table_key: str) -> str:
    """Derive a stable dataset identity from registered file and table keys."""

    digest = sha256(f"{file_id}\0{table_key}".encode("utf-8")).hexdigest()
    return f"dataset:{digest[:24]}"
