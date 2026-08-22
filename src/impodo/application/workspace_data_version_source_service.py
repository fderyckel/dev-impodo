"""Accept one workspace's frozen source evidence as its DataVersion package."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import UUID, uuid5

from ..access import Actor
from ..data_version_sources import (
    DataVersionSourcePackage,
    DataVersionSourcePackageService,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageDataset,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
    WorkspaceSourceProjection,
    WorkspaceSourceProjectionService,
    source_column_contract_hash,
)
from ..data_versions import DataVersionService, DataVersionState
from ..domain.odoo_provenance import OdooCaptureManifest
from ..domain.source_binding import require_file_source
from ..domain.source_snapshot import SourceSnapshot
from ..inspection import SourceFileCatalog
from ..migration_foundation import MigrationFoundationError
from ..migration_workspaces import MigrationWorkspaceService
from ..projects import ProjectService
from ..workspace_contracts import SourceConfiguration, SourceSelection


class WorkspaceDataVersionSourceService:
    """Promote current source evidence once, then project references back."""

    def __init__(
        self,
        workspace_states: ProjectService,
        workspace_sources,
        data_versions: DataVersionService,
        migration_workspaces: MigrationWorkspaceService,
        packages: DataVersionSourcePackageService,
        projections: WorkspaceSourceProjectionService,
    ) -> None:
        self.workspace_states = workspace_states
        self.workspace_sources = workspace_sources
        self.data_versions = data_versions
        self.migration_workspaces = migration_workspaces
        self.packages = packages
        self.projections = projections

    def accept_file_selection(
        self,
        workspace_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> WorkspaceSourceProjection:
        """Freeze canonical package evidence without copying source artifacts."""

        workspace = self.migration_workspaces.get(workspace_id, actor=actor)
        data_version = self.data_versions.get(workspace.data_version_id, actor=actor)
        if selection.project_id != workspace.workspace_id:
            raise MigrationFoundationError(
                "The frozen dataset selection belongs to another workspace"
            )
        current = self.packages.repository.get_source_package(
            data_version.data_version_id
        )
        if current is None:
            raise MigrationFoundationError("DataVersion source package is missing")
        if current.state is SourcePackageState.DRAFT:
            if data_version.state is not DataVersionState.DRAFT:
                raise MigrationFoundationError(
                    "Only a draft DataVersion can accept source evidence"
                )
            if current.origin is not SourcePackageOrigin.FILE:
                raise MigrationFoundationError(
                    "File evidence cannot be added to an Odoo DataVersion"
                )
            state = self.workspace_states.repository.get(workspace_id)
            catalogs = self.workspace_sources.get_source_catalogs(workspace_id)
            configurations = self.workspace_sources.get_source_configurations(
                workspace_id
            )
            snapshots = {
                item.dataset_id: item
                for item in self.workspace_sources.get_current_source_snapshots(
                    workspace_id
                )
            }
            catalog_by_file = {item.file_id: item for item in catalogs}
            try:
                datasets = tuple(
                    self._dataset(
                        item,
                        snapshots[item.dataset_id],
                        catalog_by_file,
                    )
                    for item in selection.datasets
                )
            except KeyError as error:
                raise MigrationFoundationError(
                    "A frozen dataset snapshot is missing"
                ) from error
            package = DataVersionSourcePackage(
                data_version_id=data_version.data_version_id,
                project_id=workspace.project_id,
                revision=current.revision + 1,
                origin=SourcePackageOrigin.FILE,
                state=SourcePackageState.DRAFT,
                files=tuple(self._file(item) for item in state.source_files),
                catalogs=tuple(self._catalog(item) for item in catalogs),
                configurations=tuple(
                    self._configuration(item) for item in configurations
                ),
                datasets=datasets,
                updated_at=datetime.now(timezone.utc),
            )
            current = self.packages.replace_draft(
                package,
                actor=actor,
                expected_package_revision=current.revision,
            )
            current = self.packages.freeze(
                data_version.data_version_id,
                actor=actor,
                expected_data_version_revision=data_version.optimistic_revision,
                expected_package_revision=current.revision,
                operation_id=self._operation(
                    selection.selection_id,
                    "accept-data-version-package",
                ),
            )
        return self._materialize_projection(
            workspace_id,
            selection,
            current,
            actor=actor,
        )

    def accept_odoo_capture(
        self,
        workspace_id: str,
        selection: SourceSelection,
        snapshot: SourceSnapshot,
        manifest: OdooCaptureManifest,
        *,
        actor: Actor,
    ) -> WorkspaceSourceProjection:
        """Freeze one complete Odoo capture and expose only its references."""

        workspace = self.migration_workspaces.get(workspace_id, actor=actor)
        data_version = self.data_versions.get(workspace.data_version_id, actor=actor)
        if (
            selection.project_id != workspace.workspace_id
            or snapshot.project_id != workspace.workspace_id
            or manifest.project_id != workspace.workspace_id
            or len(selection.datasets) != 1
        ):
            raise MigrationFoundationError(
                "The Odoo capture belongs to another workspace"
            )
        dataset = selection.datasets[0]
        if (
            dataset.dataset_id != snapshot.dataset_id
            or dataset.name != snapshot.dataset_name
            or dataset.source != snapshot.source
            or dataset.row_count != snapshot.row_count
            or tuple(item.stable_key for item in dataset.columns)
            != tuple(item.stable_key for item in snapshot.schema.columns)
            or manifest.dataset_id != dataset.dataset_id
            or manifest.dataset_name != dataset.name
            or manifest.row_count != dataset.row_count
            or manifest.data_logical_hash != snapshot.data_logical_hash
            or manifest.data_sha256 != snapshot.parquet_sha256
            or manifest.data_storage_key != snapshot.parquet_storage_key
        ):
            raise MigrationFoundationError(
                "The Odoo capture does not match its frozen dataset snapshot"
            )
        current = self.packages.repository.get_source_package(
            data_version.data_version_id
        )
        if current is None:
            raise MigrationFoundationError("DataVersion source package is missing")
        if current.state is SourcePackageState.DRAFT:
            if data_version.state is not DataVersionState.DRAFT:
                raise MigrationFoundationError(
                    "Only a draft DataVersion can accept source evidence"
                )
            if current.origin is not SourcePackageOrigin.ODOO:
                raise MigrationFoundationError(
                    "Odoo evidence cannot be added to a file DataVersion"
                )
            package = DataVersionSourcePackage(
                data_version_id=data_version.data_version_id,
                project_id=workspace.project_id,
                revision=current.revision + 1,
                origin=SourcePackageOrigin.ODOO,
                state=SourcePackageState.DRAFT,
                files=(),
                catalogs=(),
                configurations=(),
                datasets=(
                    SourcePackageDataset(
                        dataset_id=dataset.dataset_id,
                        display_name=dataset.name,
                        source_file_ids=(),
                        source=dataset.source,
                        row_count=dataset.row_count,
                        columns=dataset.columns,
                        schema_hash=source_column_contract_hash(dataset.columns),
                        snapshot_hash=snapshot.logical_hash,
                        snapshot_storage_key=snapshot.parquet_storage_key,
                        manifest={
                            "capture_manifest_hash": manifest.content_hash,
                            "capture_manifest_id": manifest.manifest_id,
                            "data_size_bytes": manifest.data_size_bytes,
                            "parquet_sha256": snapshot.parquet_sha256,
                            "provenance_logical_hash": (
                                manifest.provenance_logical_hash
                            ),
                            "provenance_sha256": manifest.provenance_sha256,
                            "provenance_size_bytes": (
                                manifest.provenance_size_bytes
                            ),
                            "provenance_storage_key": (
                                manifest.provenance_storage_key
                            ),
                            "reader_contract_version": (
                                snapshot.reader_contract_version
                            ),
                        },
                    ),
                ),
                updated_at=datetime.now(timezone.utc),
            )
            current = self.packages.replace_draft(
                package,
                actor=actor,
                expected_package_revision=current.revision,
            )
            current = self.packages.freeze(
                data_version.data_version_id,
                actor=actor,
                expected_data_version_revision=data_version.optimistic_revision,
                expected_package_revision=current.revision,
                operation_id=self._operation(
                    selection.selection_id,
                    "accept-odoo-data-version-package",
                ),
            )
        return self._materialize_projection(
            workspace_id,
            selection,
            current,
            actor=actor,
        )

    def _materialize_projection(
        self,
        workspace_id: str,
        selection: SourceSelection,
        package: DataVersionSourcePackage,
        *,
        actor: Actor,
    ) -> WorkspaceSourceProjection:
        selected_ids = tuple(sorted(item.dataset_id for item in selection.datasets))
        packaged_ids = tuple(sorted(item.dataset_id for item in package.datasets))
        selected_datasets = tuple(
            sorted(selection.datasets, key=lambda item: item.dataset_id)
        )
        packaged_datasets = tuple(
            item.to_mapping_dataset() for item in package.datasets
        )
        if selected_ids != packaged_ids or selected_datasets != packaged_datasets:
            raise MigrationFoundationError(
                "The workspace selection does not match its frozen DataVersion"
            )
        projected = self.projections.repository.get_workspace_source_projection(
            workspace_id
        )
        if projected is not None:
            if (
                projected.data_version_id != package.data_version_id
                or projected.package_hash != package.content_hash
                or tuple(item.dataset_id for item in projected.datasets)
                != packaged_ids
            ):
                raise MigrationFoundationError(
                    "The workspace source references do not match its DataVersion"
                )
            return projected
        workspace = self.migration_workspaces.get(workspace_id, actor=actor)
        return self.projections.materialize(
            workspace_id,
            actor=actor,
            dataset_ids=tuple(item.dataset_id for item in package.datasets),
            expected_workspace_revision=workspace.optimistic_revision,
            operation_id=self._operation(
                selection.selection_id,
                "project-workspace-source",
            ),
        )

    @staticmethod
    def _file(source_file) -> SourcePackageFile:
        return SourcePackageFile(
            file_id=source_file.file_id,
            display_name=source_file.display_name,
            storage_key=source_file.stored_name,
            size_bytes=source_file.size_bytes,
            sha256=(
                source_file.sha256
                if source_file.sha256.startswith("sha256:")
                else f"sha256:{source_file.sha256}"
            ),
            received_at=source_file.received_at,
        )

    @staticmethod
    def _catalog(catalog: SourceFileCatalog) -> SourcePackageCatalog:
        return SourcePackageCatalog(
            file_id=catalog.file_id,
            source_sha256=(
                catalog.source_sha256
                if catalog.source_sha256.startswith("sha256:")
                else f"sha256:{catalog.source_sha256}"
            ),
            payload=json.loads(catalog.to_json()),
        )

    @staticmethod
    def _configuration(
        configuration: SourceConfiguration,
    ) -> SourcePackageConfiguration:
        return SourcePackageConfiguration(
            file_id=configuration.file_id,
            catalog_hash=configuration.catalog_hash,
            payload=json.loads(configuration.to_json()),
        )

    @staticmethod
    def _dataset(dataset, snapshot, catalogs) -> SourcePackageDataset:
        binding = require_file_source(dataset.source)
        if binding.file_id not in catalogs:
            raise MigrationFoundationError("Frozen dataset catalogue is missing")
        return SourcePackageDataset(
            dataset_id=dataset.dataset_id,
            display_name=dataset.name,
            source_file_ids=(binding.file_id,),
            source=dataset.source,
            row_count=dataset.row_count,
            columns=dataset.columns,
            schema_hash=source_column_contract_hash(dataset.columns),
            snapshot_hash=snapshot.logical_hash,
            snapshot_storage_key=snapshot.parquet_storage_key,
            manifest={
                "data_logical_hash": snapshot.data_logical_hash,
                "parquet_sha256": snapshot.parquet_sha256,
                "physical_selection_hash": snapshot.physical_selection_hash,
                "reader_contract_version": snapshot.reader_contract_version,
            },
        )

    @staticmethod
    def _operation(selection_id: str, name: str) -> str:
        return str(uuid5(UUID(selection_id), name))
