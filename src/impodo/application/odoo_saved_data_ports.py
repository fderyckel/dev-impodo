"""Declare persistence for reusable Odoo capture definitions and versions."""

from __future__ import annotations

from typing import Protocol

from impodo.domain.odoo_saved_data import (
    SavedOdooDataDefinition,
    SavedOdooDataVersion,
    SavedOdooRestoreBinding,
    SavedOdooRestoreProfile,
)


class SavedOdooDataRepository(Protocol):
    """Persist source definitions and atomically promote complete versions."""

    def create_definition(
        self, definition: SavedOdooDataDefinition
    ) -> SavedOdooDataDefinition: ...

    def get_definition(
        self, project_id: str, saved_data_id: str
    ) -> SavedOdooDataDefinition: ...

    def list_definitions(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooDataDefinition, ...]: ...

    def save_definition(
        self, definition: SavedOdooDataDefinition, *, expected_revision: int
    ) -> SavedOdooDataDefinition: ...

    def get_configuration(
        self, project_id: str, saved_data_id: str, configuration_revision: int
    ) -> SavedOdooDataDefinition: ...

    def list_versions(
        self, project_id: str, saved_data_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooDataVersion, ...]: ...

    def get_version(
        self, project_id: str, saved_data_id: str, version_number: int
    ) -> SavedOdooDataVersion: ...

    def get_version_for_operation(
        self, project_id: str, saved_data_id: str, operation_id: str
    ) -> SavedOdooDataVersion | None: ...

    def append_version(
        self, version: SavedOdooDataVersion, *, expected_revision: int
    ) -> tuple[SavedOdooDataDefinition, SavedOdooDataVersion]: ...

    def retained_data_version(self, data_version_id: str) -> bool: ...

    def bind_restore(
        self, binding: SavedOdooRestoreBinding
    ) -> SavedOdooRestoreBinding: ...

    def get_restore_binding(
        self, workspace_id: str
    ) -> SavedOdooRestoreBinding | None: ...

    def save_restore_profile(
        self, profile: SavedOdooRestoreProfile, *, expected_revision: int | None
    ) -> SavedOdooRestoreProfile: ...

    def get_restore_profile(
        self, project_id: str, saved_data_id: str, profile_id: str
    ) -> SavedOdooRestoreProfile: ...

    def list_restore_profiles(
        self, project_id: str, saved_data_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooRestoreProfile, ...]: ...
