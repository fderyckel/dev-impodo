"""Declare the persistence port consumed by Data version commands."""

from __future__ import annotations

from typing import Protocol

from ...access import Actor
from ...domain.data_version.models import DataVersion
from ...migration_foundation import FaultInjector


class DataVersionRepository(Protocol):
    """Persist Data version roots without exposing a storage implementation."""

    def next_data_version_number(self, project_id: str) -> int: ...

    def create_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> DataVersion: ...

    def get_data_version(self, data_version_id: str) -> DataVersion: ...

    def list_data_versions(self, project_id: str) -> tuple[DataVersion, ...]: ...

    def save_data_version(
        self,
        data_version: DataVersion,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> DataVersion: ...
