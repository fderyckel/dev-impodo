"""Read a Recipe application's projected sources without the shared registry."""

from __future__ import annotations

import json

from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    SourcePackageDataset,
    SourcePackageState,
)
from impodo.application.workspace.preparation.job_models import PreparationWorkspace
from impodo.domain.project.foundation import MigrationConflictError
from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.workspace.contracts import (
    SourceSelection,
    canonical_mapping_source_selection,
)
from impodo.domain.workspace.derived_entities import mapping_source_selection
from impodo.domain.workspace.workbench import WorkspaceStateError

from .foundation_source_package_reader import read_source_package_store
from .source_repository import SourceRepository


class ProjectedPreparationSourceRepository(SourceRepository):
    """Expose one immutable DataVersion projection to a preparation worker.

    Integrated Recipe workspaces intentionally keep only a bounded projection
    in the shared foundation. The worker receives that projection's dataset and
    package identities before spawn, then reads the immutable DataVersion store
    directly. It never opens ``registry.duckdb`` and never copies source state
    into the isolated workspace database.
    """

    def __init__(
        self,
        database,
        derived_entities,
        workspace: PreparationWorkspace,
    ) -> None:
        super().__init__(database, derived_entities)
        self._workspace = workspace
        self._package_cache: DataVersionSourcePackage | None = None

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        if not self._workspace.source_dataset_ids:
            return super().get_source_selection(workspace_id)
        package, datasets = self._selected_package(workspace_id)
        physical_hashes = {
            str(item.manifest.get("physical_selection_hash", ""))
            for item in datasets
        }
        if len(physical_hashes) != 1 or not next(iter(physical_hashes)):
            raise WorkspaceStateError(
                "The projected source selection identity is incomplete"
            )
        return SourceSelection(
            selection_id=workspace_id,
            version=1,
            data_version_id=package.data_version_id,
            created_at=package.frozen_at or package.updated_at,
            created_by="Impodo DataVersion projection",
            datasets=tuple(item.to_mapping_dataset() for item in datasets),
            content_hash=next(iter(physical_hashes)),
        )

    def get_mapping_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        if not self._workspace.source_dataset_ids:
            return super().get_mapping_source_selection(workspace_id)
        selection = self.get_source_selection(workspace_id)
        if selection is None:
            return None
        return mapping_source_selection(
            canonical_mapping_source_selection(selection),
            self._derived_entities.get_derived_entity_plan(workspace_id),
            self.get_source_catalogs(workspace_id),
        )

    def get_source_catalogs(
        self,
        workspace_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        if not self._workspace.source_dataset_ids:
            return super().get_source_catalogs(workspace_id)
        package, datasets = self._selected_package(workspace_id)
        selected_file_ids = {
            file_id for item in datasets for file_id in item.source_file_ids
        }
        return tuple(
            SourceFileCatalog.from_json(
                json.dumps(
                    dict(item.payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            for item in package.catalogs
            if item.file_id in selected_file_ids
        )

    def get_current_source_snapshots(
        self,
        workspace_id: str,
    ) -> tuple[SourceSnapshot, ...]:
        if not self._workspace.source_dataset_ids:
            return super().get_current_source_snapshots(workspace_id)
        package, datasets = self._selected_package(workspace_id)
        try:
            return tuple(
                SourceSnapshot(
                    data_version_id=package.data_version_id,
                    dataset_id=item.dataset_id,
                    dataset_name=item.display_name,
                    source=item.source,
                    physical_selection_hash=str(
                        item.manifest["physical_selection_hash"]
                    ),
                    reader_contract_version=int(
                        item.manifest["reader_contract_version"]
                    ),
                    schema=SourceSnapshotSchema.create(
                        SourceSnapshotColumn.create(
                            ordinal=column.ordinal,
                            stable_key=column.stable_key,
                            source_name=column.source_name,
                            candidate_type=column.candidate_type,
                        )
                        for column in item.columns
                    ),
                    row_count=item.row_count,
                    data_logical_hash=str(item.manifest["data_logical_hash"]),
                    logical_hash=item.snapshot_hash,
                    parquet_storage_key=item.snapshot_storage_key,
                    parquet_sha256=str(item.manifest["parquet_sha256"]),
                    created_at=package.updated_at,
                )
                for item in datasets
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WorkspaceStateError(
                "The projected source snapshot manifest is incomplete"
            ) from error

    def _selected_package(
        self,
        workspace_id: str,
    ) -> tuple[DataVersionSourcePackage, tuple[SourcePackageDataset, ...]]:
        if workspace_id != self._workspace.workspace_id:
            raise WorkspaceStateError(
                "The worker was asked to read another MigrationWorkspace"
            )
        package = self._package()
        selected_ids = set(self._workspace.source_dataset_ids)
        datasets = tuple(
            item for item in package.datasets if item.dataset_id in selected_ids
        )
        if (
            len(datasets) != len(selected_ids)
            or {item.dataset_id for item in datasets} != selected_ids
        ):
            raise WorkspaceStateError(
                "The projected source datasets are missing from this DataVersion"
            )
        return package, datasets

    def _package(self) -> DataVersionSourcePackage:
        if self._package_cache is not None:
            return self._package_cache
        path = (
            self.root
            / "projects"
            / self._workspace.project_id
            / "data_versions"
            / self._workspace.data_version_id
            / "data-version.duckdb"
        )
        if not path.is_file():
            raise WorkspaceStateError("DataVersion source package is missing")
        try:
            with self._connect(path) as connection:
                package = read_source_package_store(
                    connection,
                    expected_data_version_id=self._workspace.data_version_id,
                    expected_project_id=self._workspace.project_id,
                )
        except MigrationConflictError as error:
            raise WorkspaceStateError(str(error)) from error
        if (
            package is None
            or package.state is not SourcePackageState.FROZEN
            or package.content_hash != self._workspace.source_package_hash
        ):
            raise WorkspaceStateError(
                "The projected DataVersion source package is no longer current"
            )
        self._package_cache = package
        return package
