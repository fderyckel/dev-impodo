"""Read one authorized mapping workspace without consulting the registry."""

from __future__ import annotations

from impodo.application.workspace.preparation.job_models import PreparationWorkspace
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
)
from .schema.data_version_store import (
    DATA_VERSION_STORE_GENERATION,
    DATA_VERSION_STORE_VERSION,
)
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository
from .serialization import _workspace_from_rows


class WorkspaceStateReader(DuckDbRepository):
    """Load the exact authorized workspace state passed to a child worker.

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

    def get(self, workspace_id: str) -> WorkspaceState:
        workspace_directory = self.workspace_directory(workspace_id)
        self._verify_data_version_store()
        database_path = workspace_directory / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                "SELECT * FROM workspace_projection_cache"
            ).fetchone()
            if row is None:
                raise WorkspaceStateNotFoundError("Workspace engine state not found")
            columns = [item[0] for item in connection.description]
            source_rows = connection.execute(
                """
                SELECT file_id, display_name, stored_name, size_bytes, sha256,
                       received_at
                  FROM source_file
                 ORDER BY received_at, file_id
                """
            ).fetchall()
        return _workspace_from_rows(
            dict(zip(columns, row, strict=True)),
            source_rows,
            workspace_id=self._workspace.workspace_id,
        )

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
