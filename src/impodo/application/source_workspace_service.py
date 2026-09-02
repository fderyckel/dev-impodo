"""Confirm Stage B source structure and freeze mapping-ready datasets.

Layer: application service.

After isolated inspection, ``SourceWorkspaceService.confirm_source`` binds
chosen tables and warning acknowledgement to an exact catalog.
``freeze_selection`` then creates stable dataset/column identities consumed by
schema, derived-entity, mapping, staging, and preflight workflows. It reads no
source bytes and performs no Odoo access.

See ``docs/architecture/python-code-map.md``,
``docs/developer/contracts/evidence-lifecycle.md``, and
``tests/integration/duckdb/test_workspace.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re
from threading import RLock
from typing import Iterable, Mapping, Protocol
from uuid import uuid4

from impodo.domain.shared.access import Actor, Capability
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore, ArtifactStoreError
from ..domain.source_snapshot import (
    SourceSnapshot,
)
from ..domain.odoo_capture import (
    MAX_ODOO_CAPTURE_ROWS,
    ODOO_CAPTURE_PAGE_SIZES,
    OdooCaptureContractError,
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
)
from ..domain.odoo_source_capture import (
    OdooSourceCaptureConfigurationError,
    is_odoo_capture_filter_field,
    is_odoo_capture_value_field,
    plan_odoo_source_capture,
)
from ..domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from ..domain.source_binding import FileSourceBinding, require_file_source
from impodo.application.data_version.inspection import (
    SourceFileCatalog,
    SourceInspectionError,
    SourceTableCatalog,
)
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus, SourceMode
from impodo.domain.preparation.source import SourceLoadError
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotPublisher,
)
from impodo.domain.workspace.contracts import (
    SourceConfiguration,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
    OdooSchemaCatalog,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.workspace.access import WorkspaceAccessService
from ..domain.serialization import content_hash


def _dataset_name_violations(name: str) -> tuple[str, ...]:
    """Explain each stable dataset-name rule that ``name`` breaks."""

    if not name:
        return ("Enter a name.",)
    violations: list[str] = []
    if len(name) > 63:
        violations.append("Use no more than 63 characters.")
    if not re.match(r"^[a-z]", name):
        violations.append("Start with a lowercase letter from a to z.")
    if re.search(r"[^a-z0-9_]", name):
        violations.append(
            "Use only lowercase letters, numbers, and underscores."
        )
    return tuple(violations)


class WorkspaceStateReader(Protocol):
    """Read the workspace lifecycle needed before dataset freezing."""

    def get(self, workspace_id: str) -> WorkspaceState:
        """Return the project whose registration gates dataset freezing."""
        ...


class SourceWorkspaceRepository(Protocol):
    """Persist catalogs, confirmations, and the current frozen selection."""

    def get_source_catalogs(
        self,
        workspace_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        """Return the current hash-bound catalog for each registered file."""
        ...

    def get_source_configurations(
        self,
        workspace_id: str,
    ) -> tuple[SourceConfiguration, ...]:
        """Return confirmed table/parsing choices in registered-file order."""
        ...

    def save_source_configuration(
        self,
        workspace_id: str,
        configuration: SourceConfiguration,
        *,
        actor: Actor,
    ) -> None:
        """Persist one exact catalog confirmation and retire its dependents."""
        ...

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        """Return the current complete frozen selection, if one exists."""
        ...

    def save_source_selection(
        self,
        workspace_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None:
        """Publish one selection version and invalidate derived/mapping evidence."""
        ...

    def publish_source_selection_with_snapshots(
        self,
        workspace_id: str,
        selection: SourceSelection,
        snapshots: Iterable[SourceSnapshot],
        *,
        actor: Actor,
    ) -> None:
        """Atomically publish one selection and all of its snapshot pointers."""
        ...

    def find_source_snapshot(
        self,
        workspace_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> SourceSnapshot | None:
        """Return a registered matching logical snapshot, when available."""
        ...

    def source_snapshot_storage_keys(self, workspace_id: str) -> frozenset[str]:
        """Return every immutable snapshot path referenced by DuckDB."""
        ...

    def get_current_odoo_capture_selection(
        self,
        workspace_id: str,
    ) -> OdooCaptureSelection | None:
        """Return the current protected Odoo capture selection."""

        ...

    def get_current_odoo_capture_selections(
        self,
        workspace_id: str,
    ) -> tuple[OdooCaptureSelection, ...]:
        """Return every current protected Odoo capture selection by model."""

        ...

    def save_odoo_capture_selection(
        self,
        workspace_id: str,
        selection: OdooCaptureSelection,
        *,
        actor: Actor,
    ) -> None:
        """Append and select one immutable bounded Odoo capture plan."""

        ...


class OdooCaptureSchemaReader(Protocol):
    """Read the exact schema evidence that an Odoo capture plan must bind."""

    def get_odoo_schema_catalog(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None: ...


class SourceWorkspaceService:
    """Own Stage B confirmation and versioned dataset-freeze rules.

    Confirmation is per registered file and bound to its exact inspection
    catalog. Freezing requires every file to be confirmed, assigns stable
    logical keys, and publishes one complete ``SourceSelection`` version.
    """

    def __init__(
        self,
        workspace_states: WorkspaceStateReader,
        sources: SourceWorkspaceRepository,
        authorization: WorkspaceAccessService,
        artifacts: DataVersionSourceArtifactStore | None = None,
        *,
        schemas: OdooCaptureSchemaReader | None = None,
    ) -> None:
        self.workspace_states = workspace_states
        self.sources = sources
        self.authorization = authorization
        self.artifacts = artifacts
        self.schemas = schemas
        self.snapshot_publisher = (
            SourceSnapshotPublisher(artifacts) if artifacts is not None else None
        )
        self._snapshot_lock = RLock()

    def define_odoo_capture_selection(
        self,
        workspace_id: str,
        *,
        dataset_name: str,
        model: str,
        field_names: Iterable[str],
        include_archived: bool,
        page_size: int | str,
        actor: Actor,
    ) -> OdooCaptureSelection:
        """Save a closed, bounded capture plan without contacting Odoo."""

        context = self.authorization.require(
            actor,
            Capability.SOURCE_SELECT,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspace_states.get(workspace_id)
        if workspace_state.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before selecting Odoo source records"
            )
        if workspace_state.source_mode is not SourceMode.ODOO:
            raise WorkspaceError(
                "Odoo capture selections are available only for Odoo-source projects"
            )
        if self.schemas is None:
            raise WorkspaceError("Odoo capture schema access is not configured")
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        if schema is None:
            raise WorkspaceError(
                "Capture the eligible Odoo fields before selecting records"
            )
        if schema.origin is not SchemaOrigin.LIVE_API:
            raise WorkspaceError(
                "Replace the unverified local schema draft with a live capture"
            )
        if not (
            schema.policy_hash == ODOO_SOURCE_POLICY_HASH
            and schema.connection_target_hash
            and schema.read_principal_hash
            and schema.read_permission_hash
            and schema.read_context_hash
        ):
            raise WorkspaceError(
                "Refresh the authenticated Odoo schema identity before selecting records"
            )
        schema_model = next(
            (item for item in schema.models if item.name == model),
            None,
        )
        if schema_model is None:
            raise WorkspaceError(
                "Choose one model from the current captured Odoo schema"
            )
        fields_by_name = {item.name: item for item in schema_model.fields}
        normalized_fields = tuple(sorted(dict.fromkeys(field_names)))
        if not normalized_fields:
            raise WorkspaceError("Choose at least one Odoo source field")
        unsupported = next(
            (
                name
                for name in normalized_fields
                if name not in fields_by_name
                or not is_odoo_capture_value_field(fields_by_name.get(name))
                or name in {"id", "write_date"}
            ),
            None,
        )
        if unsupported is not None:
            raise WorkspaceError(
                f"Odoo field {unsupported} is not eligible for bounded source capture"
            )
        try:
            parsed_page_size = int(page_size)
        except (TypeError, ValueError) as error:
            raise WorkspaceError(
                "Odoo capture batch size must be a whole number"
            ) from error
        if str(page_size).strip() != str(parsed_page_size):
            raise WorkspaceError("Odoo capture batch size must be a whole number")
        if parsed_page_size not in ODOO_CAPTURE_PAGE_SIZES:
            raise WorkspaceError(
                "Odoo capture batch size must be 10, 100, or 500 records"
            )
        current_selections = self.sources.get_current_odoo_capture_selections(
            workspace_id
        )
        current = next(
            (item for item in current_selections if item.model == model),
            None,
        )
        duplicate_dataset = next(
            (
                item
                for item in current_selections
                if item.model != model and item.dataset_name == dataset_name.strip()
            ),
            None,
        )
        if duplicate_dataset is not None:
            raise WorkspaceError(
                "Give each Odoo record type a different dataset name"
            )
        try:
            selection = OdooCaptureSelection.create(
                selection_id=(current.selection_id if current else str(uuid4())),
                version=(current.version + 1 if current else 1),
                data_version_id=context.data_version_id,
                dataset_name=dataset_name.strip(),
                model=model,
                field_names=normalized_fields,
                filter_clauses=(),
                filter_policy=(
                    (
                        OdooCaptureFilterPolicy.ACTIVE_AND_ARCHIVED_RECORDS
                        if include_archived
                        else OdooCaptureFilterPolicy.ACTIVE_RECORDS
                    )
                    if (
                        fields_by_name.get("active") is not None
                        and fields_by_name["active"].type == "boolean"
                        and is_odoo_capture_filter_field(fields_by_name["active"])
                    )
                    else OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS
                ),
                max_rows=MAX_ODOO_CAPTURE_ROWS,
                page_size=parsed_page_size,
                connection_target_hash=schema.connection_target_hash,
                schema_scope_hash=schema.content_hash,
                read_principal_hash=schema.read_principal_hash,
                read_permission_hash=schema.read_permission_hash,
                context_hash=schema.read_context_hash,
                created_at=datetime.now(timezone.utc),
                created_by=actor.identity.display_name,
            )
        except OdooCaptureContractError as error:
            raise WorkspaceError(str(error)) from error
        try:
            plan_odoo_source_capture(selection, schema)
        except OdooSourceCaptureConfigurationError as error:
            raise WorkspaceError(str(error)) from error
        self.sources.save_odoo_capture_selection(
            workspace_id,
            selection,
            actor=actor,
        )
        return selection

    def confirm_source(
        self,
        workspace_id: str,
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
            workspace_id=workspace_id,
        )
        catalog = _catalog(self.sources, workspace_id, file_id)
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
            workspace_id,
            configuration,
            actor=actor,
        )
        return configuration

    def freeze_selection(
        self,
        workspace_id: str,
        *,
        dataset_names: Mapping[tuple[str, str], str],
        actor: Actor,
    ) -> SourceSelection:
        """Serialize snapshot publication and pointer advancement per process."""

        with self._snapshot_lock:
            return self._freeze_selection_locked(
                workspace_id,
                dataset_names=dataset_names,
                actor=actor,
            )

    def _freeze_selection_locked(
        self,
        workspace_id: str,
        *,
        dataset_names: Mapping[tuple[str, str], str],
        actor: Actor,
    ) -> SourceSelection:
        """Freeze all confirmed tables as one versioned logical selection.

        Dataset names must be unique stable identifiers. Every dataset retains
        the source/catalog hashes and stable ordinal-based column keys required
        to detect later replacement or reinterpretation.
        """

        context = self.authorization.require(
            actor,
            Capability.SOURCE_SELECT,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspace_states.get(workspace_id)
        if workspace_state.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceError(
                "Register the project before selecting datasets"
            )
        catalogs = {
            catalog.file_id: catalog
            for catalog in self.sources.get_source_catalogs(workspace_id)
        }
        configurations = self.sources.get_source_configurations(workspace_id)
        if len(configurations) != len(workspace_state.source_files):
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
                violations = _dataset_name_violations(name)
                if violations:
                    raise WorkspaceError(
                        "Name shown in Impodo is not accepted: "
                        + " ".join(violations)
                    )
                if name in used_names:
                    raise WorkspaceError(
                        f'Name shown in Impodo "{name}" is already used. '
                        "Give each table a different name."
                    )
                used_names.add(name)
                datasets.append(
                    SourceDataset(
                        dataset_id=_dataset_key(
                            catalog.file_id,
                            table.table_key,
                        ),
                        name=name,
                        source=FileSourceBinding(
                            file_id=catalog.file_id,
                            table_key=table.table_key,
                            source_sha256=catalog.source_sha256,
                            catalog_hash=catalog.content_hash,
                            encoding=catalog.encoding,
                            delimiter=catalog.delimiter,
                            header_row=table.header_row or 1,
                        ),
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
        previous = self.sources.get_source_selection(workspace_id)
        version = previous.version + 1 if previous else 1
        content = {
            "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
            "data_version_id": context.data_version_id,
            "version": version,
            "datasets": [item.to_dict() for item in datasets],
        }
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=version,
            data_version_id=context.data_version_id,
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
                workspace_id,
                selection,
                actor=actor,
            )
            return selection

        source_files = {item.file_id: item for item in workspace_state.source_files}
        snapshots: list[SourceSnapshot] = []
        try:
            for dataset in selection.datasets:
                binding = require_file_source(dataset.source)
                catalog = catalogs.get(binding.file_id)
                source_file = source_files.get(binding.file_id)
                if catalog is None or source_file is None:
                    raise WorkspaceError("Frozen source evidence is incomplete")
                snapshots.append(
                    self.snapshot_publisher.publish(
                        workspace_state,
                        selection,
                        dataset,
                        catalog,
                        source_file,
                    ).snapshot
                )
            self.sources.publish_source_selection_with_snapshots(
                workspace_id,
                selection,
                snapshots,
                actor=actor,
            )
        except WorkspaceError:
            self._cleanup_snapshot_orphans(
                workspace_id,
                context.data_version_id,
            )
            raise
        except (ArtifactStoreError, SourceLoadError, OSError) as error:
            self._cleanup_snapshot_orphans(
                workspace_id,
                context.data_version_id,
            )
            raise WorkspaceError(
                "Impodo could not create the immutable source snapshot: "
                f"{error}"
            ) from error
        except Exception as error:
            self._cleanup_snapshot_orphans(
                workspace_id,
                context.data_version_id,
            )
            raise WorkspaceError(
                "Impodo could not publish the immutable source snapshot"
            ) from error
        self._cleanup_snapshot_orphans(
            workspace_id,
            context.data_version_id,
        )
        return selection

    def _cleanup_snapshot_orphans(
        self,
        workspace_id: str,
        data_version_id: str,
    ) -> None:
        assert self.artifacts is not None
        referenced = self.sources.source_snapshot_storage_keys(workspace_id)
        self.artifacts.cleanup_source_snapshots(data_version_id, referenced)


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
    workspace_id: str,
    file_id: str,
) -> SourceFileCatalog:
    try:
        return next(
            catalog
            for catalog in repository.get_source_catalogs(workspace_id)
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
