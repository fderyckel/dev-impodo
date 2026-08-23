"""Read one authorized mapping workspace without consulting the registry."""

from __future__ import annotations

from ...preparation_jobs import PreparationWorkspace
from ...workspace_state import (
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
)
from .schema.data_version_store import (
    DATA_VERSION_STORE_GENERATION,
    DATA_VERSION_STORE_VERSION,
)
from .schema.migration_workspace_store import (
    MIGRATION_WORKSPACE_GENERATION,
    MIGRATION_WORKSPACE_VERSION,
)
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository
from .serialization import _project_from_rows


class ProjectWorkspaceReader(DuckDbRepository):
    """Load the exact Project-authorized workspace passed to a child worker.

    The browser resolves Project, DataVersion, run, and workspace identities
    before it starts a worker. The worker checks those identities against the
    isolated stores and never opens ``registry.duckdb``.
    """

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        workspace: PreparationWorkspace,
    ) -> None:
        super().__init__(database)
        self._workspace = workspace

    def get(self, project_id: str) -> WorkspaceState:
        workspace_directory = self.workspace_directory(project_id)
        self._verify_workspace_store(workspace_directory / "workspace.duckdb")
        self._verify_data_version_store()
        database_path = workspace_directory / "project.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute("SELECT * FROM project").fetchone()
            if row is None:
                raise WorkspaceStateNotFoundError("Project not found")
            columns = [item[0] for item in connection.description]
            source_rows = connection.execute(
                """
                SELECT file_id, display_name, stored_name, size_bytes, sha256,
                       received_at
                  FROM source_file
                 ORDER BY received_at, file_id
                """
            ).fetchall()
        return _project_from_rows(
            dict(zip(columns, row, strict=True)),
            source_rows,
        )

    def _verify_workspace_store(self, path) -> None:
        if not path.is_file():
            raise WorkspaceStateError("MigrationWorkspace linkage is missing")
        with self._connect(path) as connection:
            version = connection.execute(
                "SELECT generation, version FROM schema_version WHERE singleton_id = 1"
            ).fetchone()
            linkage = connection.execute(
                """
                SELECT workspace_id, project_id, data_version_id,
                       migration_run_id, recipe_application_id
                  FROM workspace_linkage
                 WHERE singleton_id = 1
                """
            ).fetchone()
        if version != (
            MIGRATION_WORKSPACE_GENERATION,
            MIGRATION_WORKSPACE_VERSION,
        ) or linkage != (
            self._workspace.workspace_id,
            self._workspace.project_id,
            self._workspace.data_version_id,
            self._workspace.migration_run_id,
            None,
        ):
            raise WorkspaceStateError("MigrationWorkspace linkage is inconsistent")

    def _verify_data_version_store(self) -> None:
        path = (
            self.root
            / "projects"
            / self._workspace.project_id
            / "data_versions"
            / self._workspace.data_version_id
            / "data-version.duckdb"
        )
        if not path.is_file():
            raise WorkspaceStateError("DataVersion source package is missing")
        with self._connect(path) as connection:
            version = connection.execute(
                "SELECT generation, version FROM schema_version WHERE singleton_id = 1"
            ).fetchone()
            identity = connection.execute(
                """
                SELECT data_version_id, project_id, version_number, state,
                       source_package_hash
                  FROM data_version_identity
                 WHERE singleton_id = 1
                """
            ).fetchone()
        if version != (DATA_VERSION_STORE_GENERATION, DATA_VERSION_STORE_VERSION):
            raise WorkspaceStateError("DataVersion store contract is unsupported")
        if identity is None or not identity[4]:
            raise WorkspaceStateError("DataVersion source package is not accepted")
        if identity[:4] != (
            self._workspace.data_version_id,
            self._workspace.project_id,
            self._workspace.data_version_number,
            "FROZEN",
        ):
            raise WorkspaceStateError("DataVersion source package is not accepted")

