"""Read and validate immutable workspace source-projection records."""

from __future__ import annotations

from datetime import datetime

from impodo.application.data_version.source_packages import SourcePackageState, WorkspaceSourceProjection
from impodo.domain.project.foundation import MigrationConflictError


class WorkspaceSourceProjectionRecords:
    """Own source-projection reads after their cross-store creation commits."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def get(self, workspace_id: str) -> WorkspaceSourceProjection | None:
        workspace = self._repository.get_migration_workspace(workspace_id)
        path = self._repository.database.ensure_workspace_store(workspace)
        with self._repository.database.connect(path) as connection:
            row = connection.execute(
                """
                SELECT projection_id, package_hash, created_at, created_by
                  FROM workspace_source_projection WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                return None
            dataset_rows = connection.execute(
                "SELECT dataset_id, snapshot_hash "
                "FROM workspace_source_dataset ORDER BY dataset_id"
            ).fetchall()
        package = self._repository.get_source_package(workspace.data_version_id)
        if (
            package is None
            or package.state is not SourcePackageState.FROZEN
            or package.content_hash != str(row[1])
        ):
            raise MigrationConflictError(
                "Workspace source projection no longer matches its DataVersion"
            )
        datasets = tuple(package.dataset(str(item[0])) for item in dataset_rows)
        if any(
            dataset.snapshot_hash != str(stored[1])
            for dataset, stored in zip(datasets, dataset_rows, strict=True)
        ):
            raise MigrationConflictError(
                "Workspace source snapshot reference is inconsistent"
            )
        return WorkspaceSourceProjection(
            projection_id=str(row[0]),
            workspace_id=workspace.workspace_id,
            project_id=workspace.project_id,
            data_version_id=workspace.data_version_id,
            package_hash=str(row[1]),
            datasets=datasets,
            created_at=datetime.fromisoformat(str(row[2])),
            created_by=str(row[3]),
        )
