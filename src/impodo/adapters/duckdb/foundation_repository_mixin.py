"""Type-only collaboration contract for migration-foundation mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path
    from typing import Never

    import duckdb

    from impodo.application.data_version.source_packages import (
        DataVersionSourcePackage,
        WorkspaceSourceProjection,
    )
    from impodo.domain.project.foundation import (
        FaultInjector,
        MigrationOperationIntent,
        MigrationOperationKind,
    )
    from impodo.domain.run.setup import MigrationRunTargetSetup
    from impodo.domain.shared.access import Actor

    from ...domain.data_version.models import DataVersion
    from ...domain.project.models import MigrationProject
    from ...domain.run.models import MigrationRun
    from ...domain.workspace.models import MigrationWorkspace
    from .migration_foundation_database import MigrationFoundationDatabase
    from .registry_transaction import RegistryTransactionCoordinator


class FoundationRepositoryMixin:
    """Expose the concrete facade capabilities used across focused mixins.

    The declarations exist only during static analysis. At runtime this base is
    intentionally empty, so repository method resolution remains owned by the
    concrete mixins composed by ``MigrationFoundationRepository``.
    """

    if TYPE_CHECKING:
        database: MigrationFoundationDatabase
        _registry_transactions: RegistryTransactionCoordinator

        @property
        def registry_path(self) -> Path: ...

        def get_data_version(self, data_version_id: str) -> DataVersion: ...

        def get_migration_run(self, migration_run_id: str) -> MigrationRun: ...

        def get_migration_workspace(
            self,
            workspace_id: str,
        ) -> MigrationWorkspace: ...

        def get_operation_intent(
            self,
            operation_id: str,
        ) -> MigrationOperationIntent: ...

        def get_source_package(
            self,
            data_version_id: str,
        ) -> DataVersionSourcePackage | None: ...

        def get_workspace_source_projection(
            self,
            workspace_id: str,
        ) -> WorkspaceSourceProjection | None: ...

        def _get_data_version_registry(
            self,
            data_version_id: str,
        ) -> DataVersion: ...

        def _get_workspace_registry(
            self,
            workspace_id: str,
        ) -> MigrationWorkspace: ...

        def _pending_create_intent(
            self,
            operation_id: str,
            expected_kind: MigrationOperationKind,
            expected_owner_kind: str,
        ) -> MigrationOperationIntent: ...

        def _reserve_intent(
            self,
            *,
            operation_id: str,
            project_id: str,
            owner_kind: str,
            owner_id: str,
            kind: MigrationOperationKind,
            request_hash: str,
            expected_revision: int | None,
            detail: Mapping[str, object],
            actor: Actor,
        ) -> MigrationOperationIntent: ...

        def _finish_pending_intent(
            self,
            operation_id: str,
            *,
            stage: str,
            result: Mapping[str, object],
        ) -> None: ...

        @staticmethod
        def _commit_intent(
            connection: duckdb.DuckDBPyConnection,
            operation_id: str,
            *,
            stage: str,
            result: Mapping[str, object],
        ) -> None: ...

        @staticmethod
        def _set_pending_stage(
            connection: duckdb.DuckDBPyConnection,
            operation_id: str,
            stage: str,
        ) -> None: ...

        def _insert_data_version_if_needed(
            self,
            data_version: DataVersion,
            intent: MigrationOperationIntent,
            actor: Actor,
        ) -> None: ...

        def _insert_run_if_needed(
            self,
            run: MigrationRun,
            intent: MigrationOperationIntent,
            actor: Actor,
        ) -> None: ...

        def _insert_workspace_if_needed(
            self,
            workspace: MigrationWorkspace,
            intent: MigrationOperationIntent,
            actor: Actor,
        ) -> None: ...

        def _next_number(self, project_id: str, table: str, column: str) -> int: ...

        def _require_project(
            self,
            connection: duckdb.DuckDBPyConnection,
            project_id: str,
        ) -> None: ...

        def _assert_workspace_revision(
            self,
            connection: duckdb.DuckDBPyConnection,
            project_id: str,
            expected_revision: int | None,
        ) -> None: ...

        @staticmethod
        def _advance_project(
            connection: duckdb.DuckDBPyConnection,
            project_id: str,
            expected_revision: int | None,
            updated_at: datetime,
        ) -> int: ...

        @classmethod
        def _exact_row(
            cls,
            connection: duckdb.DuckDBPyConnection,
            *,
            table: str,
            id_column: str,
            identity: str,
            expected_kind: str,
        ) -> dict[str, object]: ...

        @classmethod
        def _raise_missing_identity(
            cls,
            connection: duckdb.DuckDBPyConnection,
            identity: str,
        ) -> Never: ...

        @classmethod
        def _assert_identity_available(
            cls,
            connection: duckdb.DuckDBPyConnection,
            identity: str,
        ) -> None: ...

        @staticmethod
        def _rows(
            connection: duckdb.DuckDBPyConnection,
            query: str,
            parameters: list[object],
        ) -> list[dict[str, object]]: ...

        @staticmethod
        def _insert_source_package(
            connection: duckdb.DuckDBPyConnection,
            package: DataVersionSourcePackage,
        ) -> None: ...

        def _freeze_source_store(
            self,
            data_version: DataVersion,
            *,
            package_hash: str,
            expected_package_revision: int,
            frozen_at: datetime,
            actor: Actor,
        ) -> None: ...

        @staticmethod
        def _insert_source_package_event(
            connection: duckdb.DuckDBPyConnection,
            *,
            revision: int,
            event_type: str,
            detail: Mapping[str, object],
            actor: Actor,
            occurred_at: datetime,
        ) -> None: ...

        @staticmethod
        def _insert_event(
            connection: duckdb.DuckDBPyConnection,
            *,
            project_id: str,
            aggregate_kind: str,
            aggregate_id: str,
            aggregate_revision: int,
            event_type: str,
            detail: Mapping[str, object],
            actor: Actor,
            occurred_at: datetime,
        ) -> None: ...

        @staticmethod
        def _fault(fault: FaultInjector | None, stage: str) -> None: ...

        @staticmethod
        def _time(value: datetime | None) -> str | None: ...

        @staticmethod
        def _project_values(project: MigrationProject) -> list[object]: ...

        @staticmethod
        def _project_dict(project: MigrationProject) -> dict[str, object]: ...

        @staticmethod
        def _project_from_dict(
            value: Mapping[str, object],
        ) -> MigrationProject: ...

        @classmethod
        def _project_from_row(
            cls,
            value: Mapping[str, object],
        ) -> MigrationProject: ...

        @staticmethod
        def _data_version_values(value: DataVersion) -> list[object]: ...

        @staticmethod
        def _data_version_dict(value: DataVersion) -> dict[str, object]: ...

        @staticmethod
        def _data_version_from_dict(
            value: Mapping[str, object],
        ) -> DataVersion: ...

        @classmethod
        def _data_version_from_row(
            cls,
            value: Mapping[str, object],
        ) -> DataVersion: ...

        @staticmethod
        def _run_values(value: MigrationRun) -> list[object]: ...

        @staticmethod
        def _run_dict(value: MigrationRun) -> dict[str, object]: ...

        @staticmethod
        def _run_from_dict(value: Mapping[str, object]) -> MigrationRun: ...

        @classmethod
        def _run_from_row(
            cls,
            value: Mapping[str, object],
        ) -> MigrationRun: ...

        @staticmethod
        def _target_setup_from_row(
            value: Mapping[str, object],
        ) -> MigrationRunTargetSetup: ...

        @staticmethod
        def _workspace_values(value: MigrationWorkspace) -> list[object]: ...

        @staticmethod
        def _workspace_dict(value: MigrationWorkspace) -> dict[str, object]: ...

        @staticmethod
        def _workspace_from_dict(
            value: Mapping[str, object],
        ) -> MigrationWorkspace: ...

        @classmethod
        def _workspace_from_row(
            cls,
            value: Mapping[str, object],
        ) -> MigrationWorkspace: ...

        @staticmethod
        def _intent_from_row(
            value: Mapping[str, object],
        ) -> MigrationOperationIntent: ...

        @staticmethod
        def _stored_mapping(
            value: object,
            field_name: str,
        ) -> dict[str, object]: ...

        @staticmethod
        def _stored_string_tuple(
            value: object,
            field_name: str,
        ) -> tuple[str, ...]: ...

        @staticmethod
        def _integer(value: object, field_name: str) -> int: ...
