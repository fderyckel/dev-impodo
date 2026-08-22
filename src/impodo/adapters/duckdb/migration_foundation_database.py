"""Hardened DuckDB and filesystem boundary for clean Project stores."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
from typing import Iterator
from uuid import UUID

import duckdb

from ...data_versions import DataVersion
from ...migration_foundation import (
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationStorageCompatibilityError,
)
from ...migration_workspaces import MigrationWorkspace
from .schema.data_version_store import (
    ensure_data_version_store,
    initialize_data_version_store,
)
from .schema.migration_registry import ensure_migration_registry_schema
from .schema.migration_workspace_store import (
    ensure_migration_workspace_store,
    initialize_migration_workspace_store,
)
from .unit_of_work import DuckDbConnectionFactory


class MigrationFoundationDatabase:
    """Own exact registry, DataVersion, and MigrationWorkspace stores."""

    def __init__(
        self,
        root: str | Path,
        *,
        lock_wait_timeout_seconds: float = 0.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects_root = self.root / "projects"
        self.registry_path = self.root / "registry.duckdb"
        self._assert_root_layout_before_open()
        self.connection_factory = DuckDbConnectionFactory(
            lock_wait_timeout_seconds=lock_wait_timeout_seconds
        )
        with self.connect(self.registry_path) as connection:
            ensure_migration_registry_schema(connection, self.registry_path)
        self.projects_root.mkdir(exist_ok=True)

    def _assert_root_layout_before_open(self) -> None:
        registry_exists = self.registry_path.is_file()
        allowed = {".impodo-development-reset"}
        if registry_exists:
            allowed.update(
                {"projects", "registry.duckdb", "registry.duckdb.wal"}
            )
        unexpected = [
            entry for entry in self.root.iterdir() if entry.name not in allowed
        ]
        invalid_allowed = [
            entry
            for entry in self.root.iterdir()
            if (
                (
                    entry.name in {"projects", ".impodo-development-reset"}
                    and not entry.is_dir()
                )
                or (
                    entry.name in {"registry.duckdb", "registry.duckdb.wal"}
                    and not entry.is_file()
                )
            )
        ]
        if unexpected or invalid_allowed:
            command = (
                ".\\.venv\\Scripts\\python.exe "
                "scripts\\reset-development-storage.py "
                f'--root "{self.root}"'
            )
            raise MigrationStorageCompatibilityError(str(self.root), command)

    @contextmanager
    def connect(self, path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        with self.connection_factory.connect(path) as connection:
            yield connection

    def project_directory(self, project_id: str) -> Path:
        return self._uuid_child(self.projects_root, project_id, "project_id")

    def data_version_directory(
        self,
        project_id: str,
        data_version_id: str,
    ) -> Path:
        project = self.project_directory(project_id)
        return self._uuid_child(
            project / "data_versions",
            data_version_id,
            "data_version_id",
        )

    def data_version_store_path(
        self,
        project_id: str,
        data_version_id: str,
    ) -> Path:
        return (
            self.data_version_directory(project_id, data_version_id)
            / "data-version.duckdb"
        )

    def workspace_directory(self, project_id: str, workspace_id: str) -> Path:
        project = self.project_directory(project_id)
        return self._uuid_child(
            project / "workspaces",
            workspace_id,
            "workspace_id",
        )

    def workspace_store_path(self, project_id: str, workspace_id: str) -> Path:
        return (
            self.workspace_directory(project_id, workspace_id)
            / "workspace.duckdb"
        )

    def create_data_version_store(self, data_version: DataVersion) -> Path:
        path = self.data_version_store_path(
            data_version.project_id,
            data_version.data_version_id,
        )
        if path.is_file():
            with self.connect(path) as connection:
                ensure_data_version_store(connection, path, data_version)
            return path
        directory = path.parent
        created_directory = self._prepare_store_directory(directory)
        try:
            with self.connect(path) as connection:
                connection.begin()
                try:
                    initialize_data_version_store(connection, data_version)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return path
        except Exception:
            if created_directory and directory.is_dir():
                shutil.rmtree(directory)
            raise

    def ensure_data_version_store(self, data_version: DataVersion) -> Path:
        path = self.data_version_store_path(
            data_version.project_id,
            data_version.data_version_id,
        )
        if not path.is_file():
            raise MigrationNotFoundError("DataVersion store not found")
        with self.connect(path) as connection:
            ensure_data_version_store(connection, path, data_version)
        return path

    def create_workspace_store(self, workspace: MigrationWorkspace) -> Path:
        path = self.workspace_store_path(
            workspace.project_id,
            workspace.workspace_id,
        )
        if path.is_file():
            with self.connect(path) as connection:
                ensure_migration_workspace_store(connection, path, workspace)
            return path
        directory = path.parent
        created_directory = self._prepare_store_directory(directory)
        try:
            with self.connect(path) as connection:
                connection.begin()
                try:
                    initialize_migration_workspace_store(connection, workspace)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return path
        except Exception:
            if created_directory and directory.is_dir():
                shutil.rmtree(directory)
            raise

    def ensure_workspace_store(self, workspace: MigrationWorkspace) -> Path:
        path = self.workspace_store_path(
            workspace.project_id,
            workspace.workspace_id,
        )
        if not path.is_file():
            raise MigrationNotFoundError("MigrationWorkspace store not found")
        with self.connect(path) as connection:
            ensure_migration_workspace_store(connection, path, workspace)
        return path

    @staticmethod
    def _uuid_child(parent: Path, value: str, name: str) -> Path:
        try:
            canonical = str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise MigrationNotFoundError(f"Invalid {name}") from error
        if canonical != value:
            raise MigrationNotFoundError(f"Invalid {name}")
        parent = parent.resolve()
        candidate = parent / canonical
        target = candidate.resolve()
        if target != candidate or target.parent != parent:
            raise MigrationNotFoundError(f"Invalid {name}")
        return target

    @staticmethod
    def _prepare_store_directory(directory: Path) -> bool:
        if directory.exists():
            if not directory.is_dir() or any(directory.iterdir()):
                raise MigrationFoundationError(
                    "Store location contains unrecognized persisted state"
                )
            return False
        directory.mkdir(parents=True, exist_ok=False)
        return True
