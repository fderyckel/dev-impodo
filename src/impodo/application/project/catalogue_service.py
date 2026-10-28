"""Authorize deployment-neutral Project catalogue reads and mutations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from impodo.application.project.catalogue_ports import ProjectCatalogueRepository
from impodo.domain.project.catalogue import (
    ProjectCatalogueListQuery,
    ProjectCatalogueNotFoundError,
    ProjectCataloguePage,
    ProjectCatalogueRecord,
    ProjectCatalogueState,
)
from impodo.domain.project.foundation import MigrationFoundationError, require_uuid
from impodo.domain.project.models import MigrationProjectStatus
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    RequestOrganizationScope,
)


class ProjectCatalogueService:
    """Apply one verified request scope before opening the catalogue repository."""

    def __init__(
        self,
        repository: ProjectCatalogueRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(
        self,
        record: ProjectCatalogueRecord,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> ProjectCatalogueRecord:
        """Create a Project only inside the verified request Organization."""

        if (
            record.organization_id != scope.organization_id
            or not self._authorizes(
                scope,
                authorization,
                Capability.PROJECT_CREATE,
            )
        ):
            raise AuthorizationError("Project creation is not authorized")
        return self._repository.create(record)

    def get(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> ProjectCatalogueRecord:
        """Return an authorized Project or one opaque not-found failure."""

        project_id = self._visible_project_id(
            project_id,
            scope=scope,
            authorization=authorization,
            capability=Capability.PROJECT_VIEW,
        )
        return self._repository.get(scope.organization_id, project_id)

    def list_visible(
        self,
        query: ProjectCatalogueListQuery,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> ProjectCataloguePage:
        """List only the Projects named by one verified visibility receipt."""

        if not self._authorizes(
            scope,
            authorization,
            Capability.PROJECT_VIEW,
        ):
            raise AuthorizationError("Project list is not authorized")
        return self._repository.list_visible(
            authorization.project_visibility,
            query,
        )

    def rename(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        expected_revision: int,
        display_name: str,
        updated_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Rename one visible Project through one repository mutation."""

        project_id = self._visible_project_id(
            project_id,
            scope=scope,
            authorization=authorization,
            capability=Capability.PROJECT_EDIT,
        )
        return self._repository.rename(
            scope.organization_id,
            project_id,
            expected_revision=expected_revision,
            display_name=display_name,
            updated_at=updated_at,
        )

    def update_lifecycle_summary(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        expected_revision: int,
        status: MigrationProjectStatus,
        catalogue_state: ProjectCatalogueState,
        artifact_root_key: str | None,
        current_receipt_hash: str | None,
        updated_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Project an authorized bounded lifecycle summary once."""

        project_id = self._visible_project_id(
            project_id,
            scope=scope,
            authorization=authorization,
            capability=Capability.PROJECT_EDIT,
        )
        return self._repository.update_lifecycle_summary(
            scope.organization_id,
            project_id,
            expected_revision=expected_revision,
            status=status,
            catalogue_state=catalogue_state,
            artifact_root_key=artifact_root_key,
            current_receipt_hash=current_receipt_hash,
            updated_at=updated_at,
        )

    def transition_deletion_state(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        expected_revision: int,
        target_state: ProjectCatalogueState,
        occurred_at: datetime,
    ) -> ProjectCatalogueRecord:
        """Advance soft deletion only for a visible Project delete grant."""

        project_id = self._visible_project_id(
            project_id,
            scope=scope,
            authorization=authorization,
            capability=Capability.PROJECT_DELETE,
        )
        return self._repository.transition_deletion_state(
            scope.organization_id,
            project_id,
            expected_revision=expected_revision,
            target_state=target_state,
            occurred_at=occurred_at,
        )

    def _visible_project_id(
        self,
        project_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        capability: Capability,
    ) -> str:
        try:
            project_id = require_uuid(project_id, "project_id")
        except MigrationFoundationError as error:
            raise ProjectCatalogueNotFoundError("Project not found") from error
        if not self._authorizes(
            scope,
            authorization,
            capability,
            project_id=project_id,
        ):
            raise ProjectCatalogueNotFoundError("Project not found")
        return project_id

    def _authorizes(
        self,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
        capability: Capability,
        *,
        project_id: str | None = None,
    ) -> bool:
        return (
            scope.organization_id == authorization.organization_id
            and scope.principal_id == authorization.principal_id
            and authorization.authorizes(
                capability,
                at=self._clock(),
                project_id=project_id,
            )
        )


__all__ = ["ProjectCatalogueService"]
