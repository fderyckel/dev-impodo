"""Persist the mapping engine state contained by one MigrationWorkspace."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
)
from impodo.application.odoo_database_binding_ports import OdooDatabaseBindingRepository
from impodo.application.odoo_saved_data_ports import SavedOdooDataRepository
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBindingRole,
    database_scope_hash,
)
from impodo.domain.project.foundation import MigrationConflictError, utc_now
from impodo.domain.run.setup import MigrationRunTargetSetup
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.workbench import (
    DataClassification,
    OdooConnectionMode,
    SourceFile,
    SourceMode,
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
    WorkspaceStatus,
)

from ...domain.workspace.models import (
    MigrationWorkspaceSetupState,
    MigrationWorkspaceState,
)
from .migration_foundation_repository import MigrationFoundationRepository
from .migration_workspace_engine_database import MigrationWorkspaceEngineDatabase
from .repository import DuckDbRepository
from .schema.odoo_saved_source_company import read_saved_source_company
from .workspace_state_repository import WorkspaceStateRepository


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
        *, saved_odoo_data: SavedOdooDataRepository | None = None,
        database_bindings: OdooDatabaseBindingRepository | None = None,
    ) -> None:
        DuckDbRepository.__init__(self, database)
        self.foundation = foundation
        self.saved_odoo_data = saved_odoo_data
        self.database_bindings = database_bindings

    def initialize_workbench(
        self,
        workspace: WorkspaceState,
        *,
        actor: Actor,
    ) -> None:
        """Initialize only the engine state for an existing clean workspace."""

        migration_workspace = self.foundation.get_migration_workspace(
            workspace.workspace_id
        )
        if migration_workspace.state is not MigrationWorkspaceState.OPEN:
            raise WorkspaceStateError("A closed MigrationWorkspace cannot be initialized")
        target = self.foundation.get_migration_run_target_setup(
            migration_workspace.migration_run_id
        )
        company = (
            self.get_odoo_company_binding(
                target.company_workspace_id, target.company_binding_hash
            )
            if target is not None and target.company_workspace_id is not None
            else None
        )
        directory = self.workspace_directory(workspace.workspace_id)
        database_path = directory / "workspace-engine.duckdb"
        if database_path.is_file():
            current = self.get(workspace.workspace_id)
            if current != workspace:
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
                if company is not None:
                    # Each isolated worker needs its own protected evidence.
                    # The run's canonical hash remains the authority; workers
                    # never open a sibling setup workspace to resolve it.
                    connection.execute(
                        "INSERT INTO odoo_company_binding VALUES (?, ?)",
                        [company.binding_hash, company.to_json()],
                    )
                self._insert_workspace(connection, workspace)
                self._insert_audit(
                    connection,
                    workspace,
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
        return self.get_with_source_package(workspace_id)[0]

    def get_with_source_package(
        self, workspace_id: str,
    ) -> tuple[WorkspaceState, DataVersionSourcePackage]:
        """Return state and its verified source package for one browser page query."""

        workspace = self.foundation.get_migration_workspace(workspace_id)
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace engine not found")
        package = self.foundation.get_source_package(workspace.data_version_id)
        if package is None:
            raise WorkspaceStateError(
                "The MigrationWorkspace DataVersion source package is missing"
            )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            source_company = (
                read_saved_source_company(connection)
                if package.origin is SourcePackageOrigin.ODOO else None
            )
        # The verified DataVersion package below owns these files; reading the
        # engine mirror only to replace it would add one query per state read.
        workbench = self._get_workspace_unresolved(workspace_id, include_source_files=False)
        project = self.foundation.get_project(workspace.project_id)
        target = self.foundation.get_migration_run_target_setup(
            workspace.migration_run_id
        )
        if workspace.state is MigrationWorkspaceState.CLOSED:
            status = WorkspaceStatus.CLOSED
        elif workspace.setup_state is MigrationWorkspaceSetupState.READY:
            status = WorkspaceStatus.REGISTERED
        else:
            status = WorkspaceStatus.DRAFT
        restore = (
            self.saved_odoo_data.get_restore_binding(workspace_id)
            if self.saved_odoo_data is not None and package.origin is SourcePackageOrigin.ODOO
            else None
        )
        if restore is not None and (restore.project_id != workspace.project_id or restore.data_version_id != workspace.data_version_id):
            raise WorkspaceStateError("The saved restore belongs to another source")
        projected = replace(
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
            odoo_company=(self.get_odoo_company_binding(workspace_id, target.company_binding_hash)
                if target is not None and target.company_workspace_id is not None and package.origin is SourcePackageOrigin.FILE else source_company if package.origin is SourcePackageOrigin.ODOO else None),
            destination_company=(self.get_odoo_company_binding(workspace_id, target.company_binding_hash)
                if target is not None and target.company_workspace_id is not None and package.origin is SourcePackageOrigin.ODOO else None),
            intended_applications=(
                target.intended_applications if target is not None else ()
            ),
            source_files=tuple(self._source_file(item) for item in package.files),
            status=status,
            created_at=workspace.created_at,
            registered_at=workspace.setup_completed_at,
            saved_restore_generation_id=restore.generation_id if restore else None,
        )
        if self.database_bindings is not None and projected.source_mode is SourceMode.ODOO:
            if projected.odoo_base_url and projected.odoo_database:
                source_binding, source_current = self.database_bindings.workspace_binding_status(
                    workspace_id, workspace.project_id, OdooDatabaseBindingRole.SOURCE,
                    database_scope_hash(projected.odoo_base_url, projected.odoo_database),
                )
                projected = replace(projected, source_database_binding=source_binding,
                    source_database_generation_hash=source_current or "")
            if projected.destination_configured:
                destination_binding, destination_current = self.database_bindings.workspace_binding_status(
                    workspace_id, workspace.project_id, OdooDatabaseBindingRole.DESTINATION,
                    database_scope_hash(projected.destination_odoo_base_url, projected.destination_odoo_database),
                )
                projected = replace(projected, destination_database_binding=destination_binding,
                    destination_database_generation_hash=destination_current or "",
                    saved_restore_database_reset_admitted=self.can_restore_to_declared_database(
                        workspace_id, base_url=projected.destination_odoo_base_url,
                        database=projected.destination_odoo_database,
                    ) if restore is not None else False)
        return projected, package

    def can_restore_to_declared_database(
        self, workspace_id: str, *, base_url: str, database: str,
    ) -> bool:
        """Admit offline same-endpoint restore only after an explicit later reset."""

        if self.saved_odoo_data is None or self.database_bindings is None:
            return False
        workspace = self.foundation.get_migration_workspace(workspace_id)
        restore = self.saved_odoo_data.get_restore_binding(workspace_id)
        if restore is None or restore.project_id != workspace.project_id:
            return False
        binding, current_hash = self.database_bindings.workspace_binding_status(
            workspace_id, workspace.project_id, OdooDatabaseBindingRole.DESTINATION,
            database_scope_hash(base_url, database),
        )
        if binding is None or binding.generation_hash != current_hash:
            return False
        generation = self.database_bindings.current(binding.organization_id, binding.scope_hash)
        version = self.saved_odoo_data.get_version(
            restore.project_id, restore.saved_data_id, restore.version_number,
        )
        return bool(
            generation.generation > 0
            and generation.declared_at is not None
            and version.captured_at < generation.declared_at
        )

    def save(
        self,
        workspace: WorkspaceState,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        """Persist workbench effects, then advance the canonical owner."""

        self.assert_workspace_mutable(workspace.workspace_id)
        migration_workspace = self.foundation.get_migration_workspace(
            workspace.workspace_id
        )
        if event_type == "WORKSPACE_COMPANY_UPDATED":
            run = self.foundation.get_migration_run(migration_workspace.migration_run_id)
            if run.purpose.value != "AUTHORING" and run.target_binding_id is not None:
                raise WorkspaceStateError("The company is fixed for this activated run. Start a fresh run with the saved Recipes to choose another company.")
        super().save(
            workspace,
            expected_revision=expected_revision,
            event_type=event_type,
            event_detail=event_detail,
            actor=actor,
        )
        if event_type in {"WORKSPACE_TARGET_UPDATED", "WORKSPACE_COMPANY_UPDATED", "WORKSPACE_TRANSFER_DESTINATION_UPDATED"}:
            self._replace_run_target(
                migration_workspace.migration_run_id,
                workspace,
                actor,
            )
        elif event_type == "WORKSPACE_REGISTERED":
            current = self.foundation.get_migration_workspace(workspace.workspace_id)
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
        workspace: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Add canonical DataVersion evidence before its workbench cache."""

        self.assert_workspace_mutable(workspace.workspace_id)
        migration_workspace = self.foundation.get_migration_workspace(
            workspace.workspace_id
        )
        current = self._draft_file_package(migration_workspace.data_version_id)
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
                workspace,
                source_file,
                expected_revision=expected_revision,
                actor=actor,
            )
        except Exception:
            self._restore_source_package(saved, current, actor)
            raise

    def remove_source_file(
        self,
        workspace: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Remove canonical DataVersion evidence and its workbench cache."""

        self.assert_workspace_mutable(workspace.workspace_id)
        migration_workspace = self.foundation.get_migration_workspace(
            workspace.workspace_id
        )
        current = self._draft_file_package(migration_workspace.data_version_id)
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
                workspace,
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
        company = workbench.destination_company if workbench.source_mode is SourceMode.ODOO else workbench.odoo_company
        values = (
            workbench.odoo_connection_mode.value,
            workbench.odoo_base_url,
            workbench.odoo_database,
            workbench.intended_applications,
            company.binding_hash if company else "",
        )
        if current is not None and values == (
            current.connection_mode.value,
            current.base_url,
            current.database,
            current.intended_applications,
            current.company_binding_hash,
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
                company_binding_hash=company.binding_hash if company else "",
                company_workspace_id=(workbench.workspace_id if company and
                    (current is None or current.company_binding_hash != company.binding_hash)
                    else current.company_workspace_id if current and company else None),
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
