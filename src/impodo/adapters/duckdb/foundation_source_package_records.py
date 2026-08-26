"""Write immutable Data version source-package evidence."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Mapping
from uuid import uuid4

import duckdb

from impodo.domain.shared.access import Actor
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    SourcePackageState,
)
from ...domain.data_version.models import (
    DataVersion,
)
from ...domain.serialization import canonical_json
from impodo.domain.project.foundation import (
    MigrationConflictError,
)
from impodo.domain.workspace.contracts import SourceDatasetColumn


class FoundationSourcePackageRecords:
    @staticmethod
    def _insert_source_package(
        connection: duckdb.DuckDBPyConnection,
        package: DataVersionSourcePackage,
    ) -> None:
        if package.files:
            connection.executemany(
                "INSERT INTO source_package_file VALUES (?, ?, ?, ?, ?, ?)",
                [
                    [
                        item.file_id,
                        item.display_name,
                        item.storage_key,
                        item.size_bytes,
                        item.sha256,
                        item.received_at.isoformat(),
                    ]
                    for item in package.files
                ],
            )
        if package.catalogs:
            connection.executemany(
                "INSERT INTO source_package_catalog VALUES (?, ?, ?, ?)",
                [
                    [
                        item.file_id,
                        item.source_sha256,
                        item.content_hash,
                        canonical_json(item.payload),
                    ]
                    for item in package.catalogs
                ],
            )
        if package.configurations:
            connection.executemany(
                "INSERT INTO source_package_configuration VALUES (?, ?, ?, ?)",
                [
                    [
                        item.file_id,
                        item.catalog_hash,
                        item.content_hash,
                        canonical_json(item.payload),
                    ]
                    for item in package.configurations
                ],
            )
        if package.datasets:
            connection.executemany(
                "INSERT INTO source_package_dataset "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        item.dataset_id,
                        item.display_name,
                        canonical_json(list(item.source_file_ids)),
                        canonical_json(item.source.to_dict()),
                        item.row_count,
                        canonical_json(
                            [
                                {
                                    "candidate_type": column.candidate_type,
                                    "ordinal": column.ordinal,
                                    "source_name": column.source_name,
                                    "stable_key": column.stable_key,
                                }
                                for column in item.columns
                            ]
                        ),
                        item.schema_hash,
                        item.snapshot_hash,
                        item.snapshot_storage_key,
                        canonical_json(item.manifest),
                    ]
                    for item in package.datasets
                ],
            )

    def _freeze_source_store(
        self,
        data_version: DataVersion,
        *,
        package_hash: str,
        expected_package_revision: int,
        frozen_at: datetime,
        actor: Actor,
    ) -> None:
        path = self.database.ensure_data_version_store(data_version)
        with self.database.connect(path) as connection:
            connection.begin()
            try:
                state = connection.execute(
                    """
                    SELECT revision, state, package_hash
                      FROM source_package_state WHERE singleton_id = 1
                    """
                ).fetchone()
                expected_draft = (
                    expected_package_revision,
                    SourcePackageState.DRAFT.value,
                    package_hash,
                )
                expected_frozen = (
                    expected_package_revision + 1,
                    SourcePackageState.FROZEN.value,
                    package_hash,
                )
                if state == expected_draft:
                    connection.execute(
                        """
                        UPDATE source_package_state
                           SET revision = ?, state = 'FROZEN', frozen_at = ?,
                               updated_at = ?
                         WHERE singleton_id = 1
                        """,
                        [
                            expected_package_revision + 1,
                            frozen_at.isoformat(),
                            frozen_at.isoformat(),
                        ],
                    )
                    connection.execute(
                        """
                        UPDATE data_version_identity
                           SET state = 'FROZEN', source_package_hash = ?
                         WHERE singleton_id = 1
                        """,
                        [package_hash],
                    )
                    self._insert_source_package_event(
                        connection,
                        revision=expected_package_revision + 1,
                        event_type="SOURCE_PACKAGE_FROZEN",
                        detail={"package_hash": package_hash},
                        actor=actor,
                        occurred_at=frozen_at,
                    )
                elif state != expected_frozen:
                    raise MigrationConflictError(
                        "Source package changed before acceptance"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _insert_source_package_event(
        connection: duckdb.DuckDBPyConnection,
        *,
        revision: int,
        event_type: str,
        detail: Mapping[str, object],
        actor: Actor,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO source_package_event VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(uuid4()),
                revision,
                event_type,
                canonical_json(detail),
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
                occurred_at.isoformat(),
            ],
        )

    @staticmethod
    def _json_mapping(value: str) -> Mapping[str, object]:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise MigrationConflictError("Stored source package payload is invalid")
        return payload

    @staticmethod
    def _source_columns(value: str) -> tuple[SourceDatasetColumn, ...]:
        payload = json.loads(value)
        if not isinstance(payload, list):
            raise MigrationConflictError("Stored source dataset columns are invalid")
        if any(not isinstance(item, dict) for item in payload):
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
