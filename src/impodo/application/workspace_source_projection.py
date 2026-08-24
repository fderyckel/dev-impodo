"""Expose one bounded DataVersion projection to current mapping services.

The adapter satisfies ``MappingSourceRepository`` structurally. It resolves
the immutable dataset contracts selected for one MigrationWorkspace and
returns the existing mapping engine's read-only ``SourceSelection`` view. It
does not copy source files, snapshots, catalogues, or mutable current pointers
into the workspace.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..data_version_sources import (
    DataVersionSourcePackage,
    WorkspaceSourceProjectionRepository,
)
from ..derived_entities import DerivedEntityPlan, mapping_source_selection
from ..domain.serialization import content_hash
from ..inspection import SourceFileCatalog
from ..workspace_contracts import (
    SourceSelection,
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
)


class WorkspaceProjectionRepository(WorkspaceSourceProjectionRepository, Protocol):
    """Read workspace selection and its owning immutable source package."""

    def get_source_package(
        self,
        data_version_id: str,
    ) -> DataVersionSourcePackage | None: ...


class WorkspacePreparationReader(Protocol):
    """Read source-organization rules authored inside one workspace."""

    def get_derived_entity_plan(
        self,
        workspace_id: str,
    ) -> DerivedEntityPlan | None: ...


class WorkspaceMappingSourceProjection:
    """Translate a workspace projection into the mapping source port."""

    def __init__(
        self,
        repository: WorkspaceProjectionRepository,
        preparation: WorkspacePreparationReader | None = None,
    ) -> None:
        self.repository = repository
        self.preparation = preparation

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
        selection = SourceSelection(
            selection_id=projection.projection_id,
            version=version,
            data_version_id=projection.data_version_id,
            created_at=projection.created_at,
            created_by=projection.created_by,
            datasets=datasets,
            content_hash=content_hash(
                {
                    "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
                    "datasets": [item.to_dict() for item in datasets],
                    "data_version_id": projection.data_version_id,
                    "version": version,
                }
            ),
        )
        if self.preparation is None:
            return selection
        plan = self.preparation.get_derived_entity_plan(workspace_id)
        if plan is None:
            return selection
        package = self.repository.get_source_package(projection.data_version_id)
        if package is None:
            return selection
        catalogs = tuple(
            SourceFileCatalog.from_json(
                json.dumps(
                    dict(item.payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            for item in package.catalogs
        )
        return mapping_source_selection(
            selection,
            plan,
            catalogs,
        )

    def get_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        """Expose the same immutable projection to compiler source-shape reads."""

        return self.get_mapping_source_selection(workspace_id)
