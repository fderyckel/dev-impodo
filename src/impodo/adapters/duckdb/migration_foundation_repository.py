"""Persist current Project, source, run, and workspace roots."""

from __future__ import annotations

from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    WorkspaceSourceProjection,
)
from .foundation_data_version_commands import FoundationDataVersionCommands
from .foundation_data_version_records import FoundationDataVersionRecords
from .foundation_migration_run_commands import FoundationMigrationRunCommands
from .foundation_migration_run_records import FoundationMigrationRunRecords
from .foundation_operation_intents import FoundationOperationIntents
from .foundation_project_records import FoundationProjectRecords
from .foundation_record_codecs import FoundationRecordCodecs
from .foundation_registry_support import FoundationRegistrySupport
from .foundation_source_package_reader import FoundationSourcePackageReader
from .foundation_source_package_records import FoundationSourcePackageRecords
from .foundation_workspace_commands import FoundationWorkspaceCommands
from .foundation_workspace_records import FoundationWorkspaceRecords
from .migration_foundation_database import MigrationFoundationDatabase
from .registry_transaction import RegistryTransactionCoordinator
from .workspace_source_projection_records import WorkspaceSourceProjectionRecords


class MigrationFoundationRepository(
    FoundationProjectRecords,
    FoundationDataVersionRecords,
    FoundationMigrationRunRecords,
    FoundationWorkspaceRecords,
    FoundationDataVersionCommands,
    FoundationMigrationRunCommands,
    FoundationWorkspaceCommands,
    FoundationOperationIntents,
    FoundationRegistrySupport,
    FoundationSourcePackageRecords,
    FoundationRecordCodecs,
):
    """Stable facade over owner-focused registry adapter collaborators."""

    def __init__(self, database: MigrationFoundationDatabase) -> None:
        self.database = database
        self._registry_transactions = RegistryTransactionCoordinator(database)
        self._source_packages = FoundationSourcePackageReader(self)
        self._workspace_source_projections = WorkspaceSourceProjectionRecords(self)

    @property
    def registry_path(self):
        return self.database.registry_path

    def get_source_package(
        self,
        data_version_id: str,
    ) -> DataVersionSourcePackage | None:
        return self._source_packages.get(data_version_id)

    def data_version_project_id(self, data_version_id: str) -> str:
        return self._get_data_version_registry(data_version_id).project_id

    def get_workspace_source_projection(
        self,
        workspace_id: str,
    ) -> WorkspaceSourceProjection | None:
        return self._workspace_source_projections.get(workspace_id)
