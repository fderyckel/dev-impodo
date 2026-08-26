"""Persist the mapping engine state contained by one MigrationWorkspace."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import shutil

from ...access import Actor
from ...data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
)
from ...migration_foundation import MigrationConflictError, utc_now
from ...migration_run_setup import MigrationRunTargetSetup
from ...domain.workspace.models import (
    MigrationWorkspaceSetupState,
    MigrationWorkspaceState,
)
from ...workspace_state import (
    DataClassification,
    OdooConnectionMode,
    SourceFile,
    SourceMode,
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
    WorkspaceStatus,
)
from .migration_foundation_repository import MigrationFoundationRepository
from .migration_workspace_engine_database import MigrationWorkspaceEngineDatabase
from .workspace_state_repository import WorkspaceStateRepository
from .repository import DuckDbRepository


class MigrationWorkspaceStateRepository(WorkspaceStateRepository):
    """Persist mapping-engine state for workspaces resolved by the registry."""

    _ENGINE_DIRECTORIES = (
        "inbox",
        "staging",
        "snapshots",
        "protected",
        "reports",
        "audit",
    )

    def __init__(
        self,
        database: MigrationWorkspaceEngineDatabase,
        foundation: MigrationFoundationRepository,
    ) -> None:
        DuckDbRepository.__init__(self, database)
        self.foundation = foundation

    def initialize_workbench(
        self,
        workspace_state: WorkspaceState,
        *,
        actor: Actor,
    ) -> None:
        """Initialize only the engine state for an existing clean workspace."""

        workspace = self.foundation.get_migration_workspace(
            workspace_state.workspace_id
        )
        if workspace.state is not MigrationWorkspaceState.OPEN:
            raise WorkspaceStateError("A closed MigrationWorkspace cannot be initialized")
        directory = self.workspace_directory(workspace_state.workspace_id)
        database_path = directory / "workspace-engine.duckdb"
        if database_path.is_file():
            current = self.get(workspace_state.workspace_id)
            if current != workspace_state:
                raise MigrationConflictError(
                    "MigrationWorkspace engine was already initialized differently"
                )
            return
        created: list[Path] = []
        try:
            for name in self._ENGINE_DIRECTORIES:
                child = directory / name
                child.mkdir(exist_ok=False)
                created.append(child)
            (directory / "protected").chmod(0o700)
            with self._connect(database_path) as connection:
                self._initialize_workspace_database(connection)
                self._insert_workspace(connection, workspace_state)
                self._insert_audit(
                    connection,
                    workspace_state,
                    event_type="MIGRATION_WORKSPACE_ENGINE_CREATED",
                    detail="",
                    actor=actor,
                )
        except Exception:
            database_path.unlink(missing_ok=True)
            for child in reversed(created):
                if child.is_dir():
                    shutil.rmtree(child)
            raise

    def get(self, workspace_id: str) -> WorkspaceState:
        workspace = self.foundation.get_migration_workspace(workspace_id)
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace engine not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
        workbench = self._get_workspace_unresolved(workspace_id)
        project = self.foundation.get_project(workspace.project_id)
        package = self.foundation.get_source_package(workspace.data_version_id)
        if package is None:
            raise WorkspaceStateError(
                "The MigrationWorkspace DataVersion source package is missing"
            )
        target = self.foundation.get_migration_run_target_setup(
            workspace.migration_run_id
        )
        if workspace.state is MigrationWorkspaceState.CLOSED:
            status = WorkspaceStatus.CLOSED
        elif workspace.setup_state is MigrationWorkspaceSetupState.READY:
            status = WorkspaceStatus.REGISTERED
        else:
            status = WorkspaceStatus.DRAFT
        return replace(
            workbench,
            name=workspace.display_name,
            source_system=project.source_system_identity,
            source_mode=SourceMode(package.origin.value),
            data_classification=DataClassification(
                project.data_classification.value
            ),
            retention_days=project.retention_days,
            odoo_connection_mode=(
                OdooConnectionMode(target.connection_mode.value)
                if target is not None
                else None
            ),
            odoo_base_url=target.base_url if target is not None else "",
            odoo_database=target.database if target is not None else "",
            intended_applications=(
                target.intended_applications if target is not None else ()
            ),
            source_files=tuple(self._source_file(item) for item in package.files),
            status=status,
            created_at=workspace.created_at,
            registered_at=workspace.setup_completed_at,
        )

    def save(
        self,
        workbench: WorkspaceState,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        """Persist workbench effects, then advance the canonical owner."""

        workspace = self.foundation.get_migration_workspace(workbench.workspace_id)
        super().save(
            workbench,
            expected_revision=expected_revision,
            event_type=event_type,
            event_detail=event_detail,
            actor=actor,
        )
        if event_type == "WORKSPACE_TARGET_UPDATED":
            self._replace_run_target(workspace.migration_run_id, workbench, actor)
        elif event_type == "WORKSPACE_REGISTERED":
            current = self.foundation.get_migration_workspace(workbench.workspace_id)
            if current.setup_state is MigrationWorkspaceSetupState.DRAFT:
                now = utc_now()
                self.foundation.save_migration_workspace(
                    replace(
                        current,
                        setup_state=MigrationWorkspaceSetupState.READY,
                        setup_completed_at=now,
                        updated_at=now,
                    ),
                    expected_revision=current.optimistic_revision,
                    event_type="MIGRATION_WORKSPACE_SETUP_COMPLETED",
                    actor=actor,
                )

    def add_source_file(
        self,
        workbench: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Add canonical DataVersion evidence before its workbench cache."""

        workspace = self.foundation.get_migration_workspace(workbench.workspace_id)
        current = self._draft_file_package(workspace.data_version_id)
        candidate = replace(
            current,
            revision=current.revision + 1,
            files=current.files + (self._package_file(source_file),),
            updated_at=utc_now(),
        )
        saved = self.foundation.replace_draft_source_package(
            candidate,
            expected_package_revision=current.revision,
            actor=actor,
        )
        try:
            super().add_source_file(
                workbench,
                source_file,
                expected_revision=expected_revision,
                actor=actor,
            )
        except Exception:
            self._restore_source_package(saved, current, actor)
            raise

    def remove_source_file(
        self,
        workbench: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Remove canonical DataVersion evidence and its workbench cache."""

        workspace = self.foundation.get_migration_workspace(workbench.workspace_id)
        current = self._draft_file_package(workspace.data_version_id)
        if source_file.file_id not in {item.file_id for item in current.files}:
            raise WorkspaceStateError("The DataVersion source file was not found")
        candidate = replace(
            current,
            revision=current.revision + 1,
            files=tuple(
                item for item in current.files if item.file_id != source_file.file_id
            ),
            catalogs=tuple(
                item
                for item in current.catalogs
                if item.file_id != source_file.file_id
            ),
            configurations=tuple(
                item
                for item in current.configurations
                if item.file_id != source_file.file_id
            ),
            datasets=tuple(
                item
                for item in current.datasets
                if source_file.file_id not in item.source_file_ids
            ),
            updated_at=utc_now(),
        )
        saved = self.foundation.replace_draft_source_package(
            candidate,
            expected_package_revision=current.revision,
            actor=actor,
        )
        try:
            super().remove_source_file(
                workbench,
                source_file,
                expected_revision=expected_revision,
                actor=actor,
            )
        except Exception:
            self._restore_source_package(saved, current, actor)
            raise

    def assert_workspace_mutable(self, workspace_id: str) -> None:
        workspace = self.foundation.get_migration_workspace(workspace_id)
        if workspace.state is not MigrationWorkspaceState.OPEN:
            raise WorkspaceStateError("This MigrationWorkspace is closed and read-only")

    def record_credential_removal_receipt(
        self,
        *,
        receipt_hash: str,
        workspace_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        self.record_credential_event(
            workspace_id,
            event_type="TARGET_CREDENTIAL_REMOVED",
            detail=(
                f"receipt={receipt_hash};role={role};reason={reason};"
                f"target={connection_target_hash};binding="
                f"{credential_binding_hash or ''};storage={storage_class};"
                f"removed_at={removed_at.isoformat()}"
            ),
            actor=actor,
        )

    def _replace_run_target(
        self,
        migration_run_id: str,
        workbench: WorkspaceState,
        actor: Actor,
    ) -> None:
        if workbench.odoo_connection_mode is None:
            raise WorkspaceStateError("Choose Local Odoo or Remote Odoo")
        run = self.foundation.get_migration_run(migration_run_id)
        current = self.foundation.get_migration_run_target_setup(migration_run_id)
        values = (
            workbench.odoo_connection_mode.value,
            workbench.odoo_base_url,
            workbench.odoo_database,
            workbench.intended_applications,
        )
        if current is not None and values == (
            current.connection_mode.value,
            current.base_url,
            current.database,
            current.intended_applications,
        ):
            return
        self.foundation.replace_migration_run_target_setup(
            MigrationRunTargetSetup(
                migration_run_id=migration_run_id,
                project_id=run.project_id,
                revision=current.revision + 1 if current else 1,
                connection_mode=workbench.odoo_connection_mode,
                base_url=workbench.odoo_base_url,
                database=workbench.odoo_database,
                intended_applications=workbench.intended_applications,
                updated_at=utc_now(),
            ),
            expected_revision=current.revision if current else None,
            actor=actor,
        )

    def _draft_file_package(self, data_version_id: str) -> DataVersionSourcePackage:
        package = self.foundation.get_source_package(data_version_id)
        if (
            package is None
            or package.state is not SourcePackageState.DRAFT
            or package.origin is not SourcePackageOrigin.FILE
        ):
            raise WorkspaceStateError(
                "Source files require a draft file DataVersion"
            )
        return package

    def _restore_source_package(
        self,
        saved: DataVersionSourcePackage,
        previous: DataVersionSourcePackage,
        actor: Actor,
    ) -> None:
        self.foundation.replace_draft_source_package(
            replace(
                previous,
                revision=saved.revision + 1,
                updated_at=utc_now(),
            ),
            expected_package_revision=saved.revision,
            actor=actor,
        )

    @staticmethod
    def _package_file(source_file: SourceFile) -> SourcePackageFile:
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
    def _source_file(source_file: SourcePackageFile) -> SourceFile:
        return SourceFile(
            file_id=source_file.file_id,
            display_name=source_file.display_name,
            stored_name=source_file.storage_key,
            size_bytes=source_file.size_bytes,
            sha256=source_file.sha256.removeprefix("sha256:"),
            received_at=source_file.received_at,
        )
