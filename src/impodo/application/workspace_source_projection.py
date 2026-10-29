"""Expose one bounded DataVersion projection to current mapping services.

The adapter satisfies ``MappingSourceRepository`` structurally. It resolves
the immutable dataset contracts selected for one MigrationWorkspace and
returns the existing mapping engine's read-only ``SourceSelection`` view. It
does not copy source files, snapshots, catalogues, or mutable current pointers
into the workspace.
"""

from __future__ import annotations

import json
from typing import Protocol

from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    WorkspaceSourceProjectionRepository,
)
from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.workspace.contracts import (
    SourceSelection,
    canonical_mapping_source_selection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    mapping_source_selection,
)


class WorkspaceProjectionRepository(WorkspaceSourceProjectionRepository, Protocol):
    """Read workspace selection and its owning immutable source package."""

    def get_source_package(
        self,
        data_version_id: str,
    ) -> DataVersionSourcePackage | None: ...


class WorkspacePreparationReader(Protocol):
    """Read source-organization rules authored inside one workspace."""

    def get_derived_entity_plan(
        self,
        workspace_id: str,
    ) -> DerivedEntityPlan | None: ...


class WorkspaceMappingSourceProjection:
    """Translate a workspace projection into the mapping source port."""

    def __init__(
        self,
        repository: WorkspaceProjectionRepository,
        preparation: WorkspacePreparationReader | None = None,
    ) -> None:
        self.repository = repository
        self.preparation = preparation

    def get_mapping_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        """Return only the immutable datasets selected for this workspace."""

        projection = self.repository.get_workspace_source_projection(workspace_id)
        if projection is None:
            return None
        datasets = tuple(item.to_mapping_dataset() for item in projection.datasets)
        version = 1
        selection = canonical_mapping_source_selection(
            SourceSelection(
                selection_id=projection.projection_id,
                version=version,
                data_version_id=projection.data_version_id,
                created_at=projection.created_at,
                created_by=projection.created_by,
                datasets=datasets,
                content_hash=projection.package_hash,
            )
        )
        if self.preparation is None:
            return selection
        plan = self.preparation.get_derived_entity_plan(workspace_id)
        if plan is None:
            return selection
        package = self.repository.get_source_package(projection.data_version_id)
        if package is None:
            return selection
        catalogs = tuple(
            SourceFileCatalog.from_json(
                json.dumps(
                    dict(item.payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            for item in package.catalogs
        )
        return mapping_source_selection(
            selection,
            plan,
            catalogs,
        )

    def get_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        """Return the physical selection identity bound to source snapshots."""

        projection = self.repository.get_workspace_source_projection(workspace_id)
        if projection is None:
            return None
        package = self.repository.get_source_package(projection.data_version_id)
        if package is None or package.content_hash != projection.package_hash:
            return None
        selected_ids = {item.dataset_id for item in projection.datasets}
        datasets = tuple(
            item.to_mapping_dataset()
            for item in package.datasets
            if item.dataset_id in selected_ids
        )
        if len(datasets) != len(selected_ids):
            return None
        physical_hashes = {
            str(item.manifest.get("physical_selection_hash", ""))
            for item in package.datasets
            if item.dataset_id in selected_ids
        }
        if len(physical_hashes) != 1 or not next(iter(physical_hashes)):
            return None
        return SourceSelection(
            selection_id=projection.projection_id,
            version=1,
            data_version_id=projection.data_version_id,
            created_at=projection.created_at,
            created_by=projection.created_by,
            datasets=datasets,
            content_hash=next(iter(physical_hashes)),
        )

    def get_current_source_snapshots(
        self,
        workspace_id: str,
    ) -> tuple[SourceSnapshot, ...]:
        """Rebuild immutable snapshot manifests from the owning source package."""

        projection = self.repository.get_workspace_source_projection(workspace_id)
        if projection is None:
            return ()
        package = self.repository.get_source_package(projection.data_version_id)
        if package is None or package.content_hash != projection.package_hash:
            return ()
        selected_ids = {item.dataset_id for item in projection.datasets}
        snapshots: list[SourceSnapshot] = []
        for dataset in package.datasets:
            if dataset.dataset_id not in selected_ids:
                continue
            manifest = dataset.manifest
            reader_contract_version = manifest.get("reader_contract_version")
            if type(reader_contract_version) is not int:
                return ()
            snapshots.append(
                SourceSnapshot(
                    data_version_id=projection.data_version_id,
                    dataset_id=dataset.dataset_id,
                    dataset_name=dataset.display_name,
                    source=dataset.source,
                    physical_selection_hash=str(manifest["physical_selection_hash"]),
                    reader_contract_version=reader_contract_version,
                    schema=SourceSnapshotSchema.create(
                        SourceSnapshotColumn.create(
                            ordinal=column.ordinal,
                            stable_key=column.stable_key,
                            source_name=column.source_name,
                            candidate_type=column.candidate_type,
                        )
                        for column in dataset.columns
                    ),
                    row_count=dataset.row_count,
                    data_logical_hash=str(manifest["data_logical_hash"]),
                    logical_hash=dataset.snapshot_hash,
                    parquet_storage_key=dataset.snapshot_storage_key,
                    parquet_sha256=str(manifest["parquet_sha256"]),
                    created_at=projection.created_at,
                )
            )
        if {item.dataset_id for item in snapshots} != selected_ids:
            return ()
        return tuple(sorted(snapshots, key=lambda item: item.dataset_id))
