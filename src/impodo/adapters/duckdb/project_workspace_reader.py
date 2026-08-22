"""Read one governed project without consulting the cross-project registry."""

from __future__ import annotations

from ...preparation_jobs import PreparationWorkspace
from ...projects import (
    WorkspaceState,
    ProjectError,
    ProjectNotFoundError,
)
from .database import DuckDbProjectDatabase
from .repository import DuckDbRepository
from .serialization import _project_from_rows


class ProjectWorkspaceReader(DuckDbRepository):
    """Load the exact registry-authorized workspace passed to a child worker.

    The browser process resolves the Recipe/DataVersion namespace before it
    starts a worker. The worker then verifies that immutable identity against
    the linkage stored inside the project database and rejects sealed
    workspaces. It never opens ``registry.duckdb``.
    """

    def __init__(
        self,
        database: DuckDbProjectDatabase,
        workspace: PreparationWorkspace,
    ) -> None:
        super().__init__(database)
        self._workspace = workspace

    def get(self, project_id: str) -> WorkspaceState:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            linkage = connection.execute(
                """
                SELECT recipe_id, data_version_id, data_version_number
                  FROM recipe_workspace_linkage
                 WHERE singleton_id = 1
                """
            ).fetchone()
            expected = (
                self._workspace.recipe_id,
                self._workspace.data_version_id,
                self._workspace.data_version_number,
            )
            if linkage is None or (
                str(linkage[0]),
                str(linkage[1]),
                int(linkage[2]),
            ) != expected:
                raise ProjectError(
                    "Workspace Recipe/DataVersion linkage is inconsistent"
                )
            sealed = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM recipe_workspace_seal)"
            ).fetchone()
            if sealed and bool(sealed[0]):
                raise ProjectError("This historical data version is read-only")
            row = connection.execute("SELECT * FROM project").fetchone()
            if row is None:
                raise ProjectNotFoundError("Project not found")
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
