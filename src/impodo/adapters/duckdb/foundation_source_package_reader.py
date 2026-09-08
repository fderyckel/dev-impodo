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
from impodo.domain.workspace.contracts import SourceDatasetColumn
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
            return read_source_package_store(
                connection,
                expected_data_version_id=data_version.data_version_id,
                expected_project_id=data_version.project_id,
            )


def read_source_package_store(
    connection,
    *,
    expected_data_version_id: str,
    expected_project_id: str,
) -> DataVersionSourcePackage | None:
    """Rebuild and verify one immutable package without opening the registry.

    Background workspace workers already receive their exact Project and
    DataVersion identities from the browser process. Keeping this reader at
    the DataVersion-store boundary lets those workers validate and consume the
    frozen package without contending for the shared registry database.
    """

    identity = connection.execute(
        """
        SELECT data_version_id, project_id, source_package_hash
          FROM data_version_identity
         WHERE singleton_id = 1
        """
    ).fetchone()
    if identity is None or identity[:2] != (
        expected_data_version_id,
        expected_project_id,
    ):
        raise MigrationConflictError(
            "Stored source package belongs to another DataVersion"
        )
    state = connection.execute(
        """
        SELECT revision, state, origin, package_hash, updated_at, frozen_at
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
            payload=_json_mapping(str(row[3])),
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
            payload=_json_mapping(str(row[3])),
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
            source=source_binding_from_dict(_json_mapping(str(row[3]))),
            row_count=int(row[4]),
            columns=_source_columns(str(row[5])),
            schema_hash=str(row[6]),
            snapshot_hash=str(row[7]),
            snapshot_storage_key=str(row[8]),
            manifest=_json_mapping(str(row[9])),
        )
        for row in connection.execute(
            "SELECT * FROM source_package_dataset ORDER BY dataset_id"
        ).fetchall()
    )
    package = DataVersionSourcePackage(
        data_version_id=expected_data_version_id,
        project_id=expected_project_id,
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
    if (
        package.content_hash != str(state[3])
        or (
            identity[2] is not None
            and package.content_hash != str(identity[2])
        )
    ):
        raise MigrationConflictError(
            "Stored source package hash is inconsistent"
        )
    return package


def _json_mapping(value: str) -> dict[str, object]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise MigrationConflictError("Stored source package payload is invalid")
    return payload


def _source_columns(value: str) -> tuple[SourceDatasetColumn, ...]:
    payload = json.loads(value)
    if not isinstance(payload, list) or any(
        not isinstance(item, dict) for item in payload
    ):
        raise MigrationConflictError("Stored source dataset columns are invalid")
    try:
        return tuple(
            SourceDatasetColumn(
                ordinal=int(item["ordinal"]),
                source_name=str(item["source_name"]),
                stable_key=str(item["stable_key"]),
                candidate_type=str(item["candidate_type"]),
            )
            for item in payload
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MigrationConflictError(
            "Stored source dataset columns are invalid"
        ) from error
