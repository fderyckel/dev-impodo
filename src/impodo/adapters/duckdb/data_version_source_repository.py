"""Expose DataVersion-owned source metadata through the mapping workbench."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace

from ...access import Actor
from ...data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageState,
)
from ...domain.odoo_capture import OdooCaptureSelection
from ...domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from ...inspection import SourceFileCatalog
from ...migration_foundation import MigrationFoundationError, utc_now
from ...workspace_contracts import SourceConfiguration, SourceSelection
from .migration_foundation_repository import MigrationFoundationRepository
from .source_repository import SourceRepository


class DataVersionOwnedSourceRepository(SourceRepository):
    """Keep workbench tables as derived caches of one DataVersion package.

    Snapshot and invalidation code still needs local workspace tables. Reads of
    files, catalogues, and confirmations nevertheless come from the canonical
    DataVersion package, and every browser write advances that package.
    """

    def __init__(
        self,
        *args,
        foundation: MigrationFoundationRepository,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.foundation = foundation

    def get_source_catalogs(
        self,
        workspace_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        package = self._package(workspace_id)
        return tuple(
            SourceFileCatalog.from_json(self._payload_json(item.payload))
            for item in package.catalogs
        )

    def save_source_catalogs(
        self,
        workspace_id: str,
        catalogs: Iterable[SourceFileCatalog],
        *,
        actor: Actor,
    ) -> None:
        catalog_set = tuple(catalogs)
        super().save_source_catalogs(workspace_id, catalog_set, actor=actor)
        current = self._draft_package(workspace_id)
        candidate = replace(
            current,
            revision=current.revision + 1,
            catalogs=tuple(self._catalog(item) for item in catalog_set),
            configurations=(),
            datasets=(),
            updated_at=utc_now(),
        )
        self._replace(current, candidate, actor)

    def save_source_catalog(
        self,
        workspace_id: str,
        catalog: SourceFileCatalog,
        *,
        actor: Actor,
    ) -> None:
        super().save_source_catalog(workspace_id, catalog, actor=actor)
        current = self._draft_package(workspace_id)
        catalogs = {item.file_id: item for item in current.catalogs}
        catalogs[catalog.file_id] = self._catalog(catalog)
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
            updated_at=utc_now(),
        )
        self._replace(current, candidate, actor)

    def get_source_configurations(
        self,
        workspace_id: str,
    ) -> tuple[SourceConfiguration, ...]:
        package = self._package(workspace_id)
        return tuple(
            SourceConfiguration.from_json(self._payload_json(item.payload))
            for item in package.configurations
        )

    def save_source_configuration(
        self,
        workspace_id: str,
        configuration: SourceConfiguration,
        *,
        actor: Actor,
    ) -> None:
        super().save_source_configuration(workspace_id, configuration, actor=actor)
        current = self._draft_package(workspace_id)
        configurations = {
            item.file_id: item for item in current.configurations
        }
        configurations[configuration.file_id] = self._configuration(
            configuration
        )
        candidate = replace(
            current,
            revision=current.revision + 1,
            configurations=tuple(configurations.values()),
            datasets=tuple(
                item
                for item in current.datasets
                if configuration.file_id not in item.source_file_ids
            ),
            updated_at=utc_now(),
        )
        self._replace(current, candidate, actor)

    def get_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        selection = super().get_source_selection(workspace_id)
        if (
            selection is not None
            and selection.data_version_id != self._package(workspace_id).data_version_id
        ):
            raise MigrationFoundationError(
                "The workspace source selection belongs to another DataVersion"
            )
        return selection

    def get_current_source_snapshots(
        self,
        workspace_id: str,
    ) -> tuple[SourceSnapshot, ...]:
        """Read local snapshots or rebuild their immutable package manifests."""

        snapshots = super().get_current_source_snapshots(workspace_id)
        if snapshots:
            return snapshots
        selection = self.get_source_selection(workspace_id)
        if selection is None:
            return ()
        package = self._package(workspace_id)
        selected_ids = {item.dataset_id for item in selection.datasets}
        packaged = {
            item.dataset_id: item
            for item in package.datasets
            if item.dataset_id in selected_ids
        }
        if set(packaged) != selected_ids:
            raise MigrationFoundationError(
                "The projected source snapshots are incomplete"
            )
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
                for item in sorted(
                    packaged.values(),
                    key=lambda value: value.dataset_id,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MigrationFoundationError(
                "The projected source snapshot manifest is incomplete"
            ) from error

    def save_source_selection(
        self,
        workspace_id: str,
        selection: SourceSelection,
        *,
        actor: Actor,
    ) -> None:
        self._require_source_owner(workspace_id, selection, ())
        super().save_source_selection(workspace_id, selection, actor=actor)

    def save_odoo_capture_selection(
        self,
        workspace_id: str,
        selection: OdooCaptureSelection,
        *,
        actor: Actor,
    ) -> None:
        if selection.data_version_id != self._package(workspace_id).data_version_id:
            raise MigrationFoundationError(
                "The Odoo capture selection belongs to another DataVersion"
            )
        super().save_odoo_capture_selection(
            workspace_id,
            selection,
            actor=actor,
        )

    def publish_source_selection_with_snapshots(
        self,
        workspace_id: str,
        selection: SourceSelection,
        snapshots: Iterable[SourceSnapshot],
        *,
        actor: Actor,
    ) -> None:
        snapshot_set = tuple(snapshots)
        self._require_source_owner(workspace_id, selection, snapshot_set)
        super().publish_source_selection_with_snapshots(
            workspace_id,
            selection,
            snapshot_set,
            actor=actor,
        )

    def _require_source_owner(
        self,
        workspace_id: str,
        selection: SourceSelection,
        snapshots: tuple[SourceSnapshot, ...],
    ) -> None:
        data_version_id = self._package(workspace_id).data_version_id
        if selection.data_version_id != data_version_id or any(
            item.data_version_id != data_version_id for item in snapshots
        ):
            raise MigrationFoundationError(
                "Source evidence belongs to another DataVersion"
            )

    def _package(self, workspace_id: str) -> DataVersionSourcePackage:
        context = self.foundation.resolve_workspace_access_context(workspace_id)
        package = self.foundation.get_source_package(context.data_version_id)
        if (
            package is None
            or package.data_version_id != context.data_version_id
            or package.project_id != context.project_id
        ):
            raise MigrationFoundationError(
                "The workspace DataVersion source package is missing"
            )
        return package

    def _draft_package(self, workspace_id: str) -> DataVersionSourcePackage:
        package = self._package(workspace_id)
        if package.state is not SourcePackageState.DRAFT:
            raise MigrationFoundationError(
                "Accepted DataVersion source evidence is immutable"
            )
        return package

    def _replace(
        self,
        current: DataVersionSourcePackage,
        candidate: DataVersionSourcePackage,
        actor: Actor,
    ) -> None:
        self.foundation.replace_draft_source_package(
            candidate,
            expected_package_revision=current.revision,
            actor=actor,
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
    def _payload_json(payload) -> str:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
