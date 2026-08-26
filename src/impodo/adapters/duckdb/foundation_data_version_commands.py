"""Create, resume, replace, and freeze Data version deliveries."""

from __future__ import annotations

from datetime import datetime

from ...access import Actor
from ...data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageState,
)
from ...domain.data_version.models import (
    DataVersion,
    DataVersionState,
)
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    utc_now,
)


class FoundationDataVersionCommands:
    def create_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersion:
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=data_version.project_id,
            owner_kind="DATA_VERSION",
            owner_id=data_version.data_version_id,
            kind=MigrationOperationKind.DATA_VERSION_CREATE,
            request_hash=request_hash,
            expected_revision=expected_workspace_revision,
            detail={"data_version": self._data_version_dict(data_version)},
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_data_version(intent.owner_id)
        stored = self._data_version_from_dict(dict(intent.detail["data_version"]))
        self._fault(fault, "INTENT_RESERVED")
        self._insert_data_version_if_needed(stored, intent, actor)
        self._fault(fault, "REGISTRY_COMMITTED")
        self.database.create_data_version_store(stored)
        self._fault(fault, "STORE_CREATED")
        self._finish_pending_intent(
            intent.operation_id,
            stage="STORE_LINKED",
            result={"data_version_id": stored.data_version_id},
        )
        return self.get_data_version(stored.data_version_id)

    def resume_data_version_creation(
        self,
        operation_id: str,
        *,
        actor: Actor,
    ) -> DataVersion:
        """Resume one reserved create without exposing adapter serialization."""

        intent = self._pending_create_intent(
            operation_id,
            MigrationOperationKind.DATA_VERSION_CREATE,
            "DATA_VERSION",
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_data_version(intent.owner_id)
        stored = self._data_version_from_dict(dict(intent.detail["data_version"]))
        return self.create_data_version(
            stored,
            expected_workspace_revision=int(intent.expected_revision or 0),
            operation_id=intent.operation_id,
            request_hash=intent.request_hash,
            actor=actor,
        )

    def replace_draft_source_package(
        self,
        package: DataVersionSourcePackage,
        *,
        expected_package_revision: int | None,
        actor: Actor,
    ) -> DataVersionSourcePackage:
        if expected_package_revision is not None:
            expected_package_revision = require_revision(
                expected_package_revision,
                "expected_package_revision",
            )
        if package.state is not SourcePackageState.DRAFT:
            raise MigrationConflictError("Frozen source packages are immutable")
        data_version = self._get_data_version_registry(package.data_version_id)
        if (
            data_version.project_id != package.project_id
            or data_version.state is not DataVersionState.DRAFT
        ):
            raise MigrationConflictError(
                "Source package does not belong to an editable DataVersion"
            )
        path = self.database.ensure_data_version_store(data_version)
        with self.database.connect(path) as connection:
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, state FROM source_package_state "
                    "WHERE singleton_id = 1"
                ).fetchone()
                current_revision = int(current[0])
                required_current = expected_package_revision or 0
                if (
                    str(current[1]) != SourcePackageState.DRAFT.value
                    or current_revision != required_current
                    or package.revision != current_revision + 1
                ):
                    raise MigrationConflictError(
                        "Source package changed; reload and retry"
                    )
                connection.execute("DELETE FROM source_package_configuration")
                connection.execute("DELETE FROM source_package_catalog")
                connection.execute("DELETE FROM source_package_file")
                connection.execute("DELETE FROM source_package_dataset")
                self._insert_source_package(connection, package)
                connection.execute(
                    """
                    UPDATE source_package_state
                       SET revision = ?, state = 'DRAFT', origin = ?,
                           package_hash = ?, updated_at = ?, frozen_at = NULL
                     WHERE singleton_id = 1
                    """,
                    [
                        package.revision,
                        package.origin.value,
                        package.content_hash,
                        package.updated_at.isoformat(),
                    ],
                )
                self._insert_source_package_event(
                    connection,
                    revision=package.revision,
                    event_type="SOURCE_PACKAGE_DRAFT_REPLACED",
                    detail={"package_hash": package.content_hash},
                    actor=actor,
                    occurred_at=package.updated_at,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        saved = self.get_source_package(package.data_version_id)
        if saved is None:
            raise MigrationConflictError("Source package was not persisted")
        return saved

    def freeze_source_package(
        self,
        data_version_id: str,
        *,
        expected_data_version_revision: int,
        expected_package_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersionSourcePackage:
        data_version = self._get_data_version_registry(data_version_id)
        package = self.get_source_package(data_version_id)
        if package is None:
            raise MigrationConflictError("Source package is not assembled")
        package.require_acceptance_ready()
        if package.state is SourcePackageState.FROZEN:
            try:
                self.get_operation_intent(operation_id)
            except MigrationNotFoundError as error:
                raise MigrationConflictError(
                    "Frozen source packages cannot be accepted again"
                ) from error
        frozen_at = utc_now()
        detail = {
            "data_version_id": data_version_id,
            "frozen_at": frozen_at.isoformat(),
            "package_hash": package.content_hash,
            "package_revision": expected_package_revision,
        }
        intent = self._reserve_intent(
            operation_id=operation_id,
            project_id=data_version.project_id,
            owner_kind="DATA_VERSION",
            owner_id=data_version_id,
            kind=MigrationOperationKind.DATA_VERSION_FREEZE,
            request_hash=request_hash,
            expected_revision=expected_data_version_revision,
            detail=detail,
            actor=actor,
        )
        stored = dict(intent.detail)
        self._fault(fault, "INTENT_RESERVED")
        self._freeze_source_store(
            data_version,
            package_hash=str(stored["package_hash"]),
            expected_package_revision=int(stored["package_revision"]),
            frozen_at=datetime.fromisoformat(str(stored["frozen_at"])),
            actor=actor,
        )
        self._fault(fault, "STORE_CREATED")
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT state, source_package_hash, optimistic_revision
                      FROM data_version WHERE data_version_id = ?
                    """,
                    [data_version_id],
                ).fetchone()
                expected = expected_data_version_revision
                if row == (DataVersionState.DRAFT.value, None, expected):
                    connection.execute(
                        """
                        UPDATE data_version
                           SET state = 'FROZEN', source_package_hash = ?,
                               optimistic_revision = ?, updated_at = ?,
                               frozen_at = ?
                         WHERE data_version_id = ?
                        """,
                        [
                            stored["package_hash"],
                            expected + 1,
                            stored["frozen_at"],
                            stored["frozen_at"],
                            data_version_id,
                        ],
                    )
                    self._insert_event(
                        connection,
                        project_id=data_version.project_id,
                        aggregate_kind="DATA_VERSION",
                        aggregate_id=data_version_id,
                        aggregate_revision=expected + 1,
                        event_type="DATA_VERSION_SOURCE_PACKAGE_FROZEN",
                        detail={"package_hash": stored["package_hash"]},
                        actor=actor,
                        occurred_at=datetime.fromisoformat(str(stored["frozen_at"])),
                    )
                elif row != (
                    DataVersionState.FROZEN.value,
                    stored["package_hash"],
                    expected + 1,
                ):
                    raise MigrationConflictError(
                        "DataVersion changed before source acceptance"
                    )
                self._commit_intent(
                    connection,
                    operation_id,
                    stage="SOURCE_PACKAGE_FROZEN",
                    result={"data_version_id": data_version_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._fault(fault, "REGISTRY_COMMITTED")
        frozen = self.get_source_package(data_version_id)
        if frozen is None:
            raise MigrationConflictError("Frozen source package is missing")
        return frozen
