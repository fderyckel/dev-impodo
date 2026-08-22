"""Expose one bounded DataVersion projection to current mapping services.

The adapter satisfies ``MappingSourceRepository`` structurally. It resolves
the immutable dataset contracts selected for one MigrationWorkspace and
returns the existing mapping engine's read-only ``SourceSelection`` view. It
does not copy source files, snapshots, catalogues, or mutable current pointers
into the workspace.
"""

from __future__ import annotations

from ..data_version_sources import WorkspaceSourceProjectionRepository
from ..domain.serialization import content_hash
from ..workspace_contracts import SourceSelection


class WorkspaceMappingSourceProjection:
    """Translate a workspace projection into the mapping source port."""

    def __init__(
        self,
        repository: WorkspaceSourceProjectionRepository,
    ) -> None:
        self.repository = repository

    def get_mapping_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        """Return only the immutable datasets selected for this workspace."""

        projection = self.repository.get_workspace_source_projection(
            workspace_id
        )
        if projection is None:
            return None
        datasets = tuple(
            item.to_mapping_dataset() for item in projection.datasets
        )
        version = 1
        return SourceSelection(
            selection_id=projection.projection_id,
            version=version,
            project_id=projection.workspace_id,
            created_at=projection.created_at,
            created_by=projection.created_by,
            datasets=datasets,
            content_hash=content_hash(
                {
                    "datasets": [item.to_dict() for item in datasets],
                    "project_id": projection.workspace_id,
                    "version": version,
                }
            ),
        )

    def get_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        """Expose the same immutable projection to compiler source-shape reads."""

        return self.get_mapping_source_selection(workspace_id)
