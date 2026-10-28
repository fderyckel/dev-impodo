"""Declare the bounded Project catalogue persistence contract."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.project.catalogue import (
    ProjectCatalogueListQuery,
    ProjectCataloguePage,
    ProjectCatalogueRecord,
    ProjectCatalogueState,
)
from impodo.domain.project.models import MigrationProjectStatus
from impodo.domain.shared.tenancy import OrganizationId, ProjectVisibilityScope


class ProjectCatalogueRepository(Protocol):
    """Persist Organization-scoped discovery facts without data-plane evidence."""

    def create(self, record: ProjectCatalogueRecord) -> ProjectCatalogueRecord:
        """Create revision one for one Organization without a hard-delete path."""
        ...

    def get(
        self,
        organization_id: OrganizationId,
        project_id: str,
    ) -> ProjectCatalogueRecord:
        """Return one scoped record or the same opaque not-found failure."""
        ...

    def list_visible(
        self,
        visibility: ProjectVisibilityScope,
        query: ProjectCatalogueListQuery,
    ) -> ProjectCataloguePage:
        """Filter visibility, state, ordering, and pagination inside PostgreSQL."""
        ...

    def rename(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
        display_name: str,
        updated_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Atomically rename one exact Organization-scoped revision."""
        ...

    def update_lifecycle_summary(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
        status: MigrationProjectStatus,
        catalogue_state: ProjectCatalogueState,
        artifact_root_key: str | None,
        current_receipt_hash: str | None,
        updated_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Project one bounded, non-deletion lifecycle result atomically."""
        ...

    def transition_deletion_state(
        self,
        organization_id: OrganizationId,
        project_id: str,
        *,
        expected_revision: int,
        target_state: ProjectCatalogueState,
        occurred_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Move to `DELETING` or `DELETED` without removing the catalogue row."""
        ...
