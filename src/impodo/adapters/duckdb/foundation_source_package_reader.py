"""Read hash-verified Data version source-package state."""

from __future__ import annotations

from datetime import datetime
import json

from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageDataset,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
)
from ...domain.source_binding import source_binding_from_dict
from impodo.domain.project.foundation import MigrationConflictError


class FoundationSourcePackageReader:
    """Own stored source-package reconstruction and content-hash checks."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def get(self, data_version_id: str) -> DataVersionSourcePackage | None:
        data_version = self._repository._get_data_version_registry(data_version_id)
        path = self._repository.database.ensure_data_version_store(data_version)
        with self._repository.database.connect(path) as connection:
            state = connection.execute(
                """
                SELECT revision, state, origin, package_hash, updated_at,
                       frozen_at
                  FROM source_package_state WHERE singleton_id = 1
                """
            ).fetchone()
            if state is None or int(state[0]) == 0:
                return None
            files = tuple(
                SourcePackageFile(
                    file_id=str(row[0]),
                    display_name=str(row[1]),
                    storage_key=str(row[2]),
                    size_bytes=int(row[3]),
                    sha256=str(row[4]),
                    received_at=datetime.fromisoformat(str(row[5])),
                )
                for row in connection.execute(
                    "SELECT * FROM source_package_file ORDER BY file_id"
                ).fetchall()
            )
            catalog_rows = connection.execute(
                "SELECT * FROM source_package_catalog ORDER BY file_id"
            ).fetchall()
            catalogs = tuple(
                SourcePackageCatalog(
                    file_id=str(row[0]),
                    source_sha256=str(row[1]),
                    payload=self._repository._json_mapping(str(row[3])),
                )
                for row in catalog_rows
            )
            if any(
                item.content_hash != str(row[2])
                for item, row in zip(catalogs, catalog_rows, strict=True)
            ):
                raise MigrationConflictError(
                    "Stored source catalogue hash is inconsistent"
                )
            configuration_rows = connection.execute(
                "SELECT * FROM source_package_configuration ORDER BY file_id"
            ).fetchall()
            configurations = tuple(
                SourcePackageConfiguration(
                    file_id=str(row[0]),
                    catalog_hash=str(row[1]),
                    payload=self._repository._json_mapping(str(row[3])),
                )
                for row in configuration_rows
            )
            if any(
                item.content_hash != str(row[2])
                for item, row in zip(
                    configurations,
                    configuration_rows,
                    strict=True,
                )
            ):
                raise MigrationConflictError(
                    "Stored source confirmation hash is inconsistent"
                )
            datasets = tuple(
                SourcePackageDataset(
                    dataset_id=str(row[0]),
                    display_name=str(row[1]),
                    source_file_ids=tuple(json.loads(str(row[2]))),
                    source=source_binding_from_dict(
                        self._repository._json_mapping(str(row[3]))
                    ),
                    row_count=int(row[4]),
                    columns=self._repository._source_columns(str(row[5])),
                    schema_hash=str(row[6]),
                    snapshot_hash=str(row[7]),
                    snapshot_storage_key=str(row[8]),
                    manifest=self._repository._json_mapping(str(row[9])),
                )
                for row in connection.execute(
                    "SELECT * FROM source_package_dataset ORDER BY dataset_id"
                ).fetchall()
            )
        package = DataVersionSourcePackage(
            data_version_id=data_version.data_version_id,
            project_id=data_version.project_id,
            revision=int(state[0]),
            origin=SourcePackageOrigin(str(state[2])),
            state=SourcePackageState(str(state[1])),
            files=files,
            catalogs=catalogs,
            configurations=configurations,
            datasets=datasets,
            updated_at=datetime.fromisoformat(str(state[4])),
            frozen_at=(
                datetime.fromisoformat(str(state[5]))
                if state[5] is not None
                else None
            ),
        )
        if package.content_hash != str(state[3]):
            raise MigrationConflictError(
                "Stored source package hash is inconsistent"
            )
        return package
