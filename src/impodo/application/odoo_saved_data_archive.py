"""Authorize password-protected saved-data export and offline admission."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from impodo.application.odoo_saved_data_service import SavedOdooDataService
from impodo.domain.odoo_saved_data import SavedOdooDataDefinition, SavedOdooDataVersion
from impodo.domain.project.foundation import require_uuid
from impodo.domain.shared.access import Actor, Capability


class SavedOdooDataArchivePort(Protocol):
    """Transfer exact immutable evidence without copying write authority."""

    def export_version(
        self,
        definition: SavedOdooDataDefinition,
        version: SavedOdooDataVersion,
        *,
        destination: Path,
        password: str,
        actor: Actor,
    ) -> Path: ...

    def import_version(
        self,
        project_id: str,
        path: Path,
        *,
        password: str,
        actor: Actor,
    ) -> SavedOdooDataDefinition: ...


class OdooSavedDataArchiveService:
    """Apply project authorization before reading archives or protected rows."""

    def __init__(
        self, *, saved_data: SavedOdooDataService, backend: SavedOdooDataArchivePort
    ) -> None:
        self.saved_data = saved_data
        self.backend = backend

    def export_version(
        self,
        project_id: str,
        saved_data_id: str,
        version_number: int,
        *,
        destination: Path,
        password: str,
        actor: Actor,
    ) -> Path:
        """Download one version without contacting its original source."""

        def prepare():
            self.saved_data.get(project_id, saved_data_id, actor=actor)
            version = self.saved_data.repository.get_version(
                project_id, saved_data_id, version_number
            )
            self.saved_data.access.require(
                actor,
                Capability.PROTECTED_EVIDENCE_READ,
                workspace_id=version.workspace_id,
            )
            configuration = self.saved_data.repository.get_configuration(
                project_id, saved_data_id, version.configuration_revision
            )
            return configuration, version

        configuration, version = self.saved_data.local_operation(prepare)
        return self.backend.export_version(
            configuration,
            version,
            destination=destination,
            password=password,
            actor=actor,
        )

    def import_version(
        self,
        project_id: str,
        path: Path,
        *,
        password: str,
        actor: Actor,
    ) -> SavedOdooDataDefinition:
        """Validate a package before admitting it to the chosen project."""

        project_id = require_uuid(project_id, "project_id")
        for capability in (
            Capability.PROJECT_EDIT,
            Capability.DATA_VERSION_CREATE,
            Capability.DATA_VERSION_EDIT,
            Capability.MIGRATION_RUN_CREATE,
            Capability.MIGRATION_WORKSPACE_CREATE,
            Capability.MIGRATION_WORKSPACE_EDIT,
            Capability.PROJECT_REGISTER,
            Capability.SOURCE_CONFIGURE,
            Capability.SOURCE_CAPTURE,
            Capability.SOURCE_SELECT,
            Capability.SCHEMA_DISCOVER,
            Capability.PROTECTED_EVIDENCE_MANAGE,
        ):
            self.saved_data.authorization.require(
                actor, capability, project_id=project_id
            )
        return self.backend.import_version(
            project_id, path, password=password, actor=actor
        )
