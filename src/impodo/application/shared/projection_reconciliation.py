"""Authorize bounded receipt scans and governed projection rebuilds."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from impodo.application.shared.cross_store_coordinator import (
    CrossStoreOperationResult,
)
from impodo.domain.project.foundation import MigrationFoundationError, require_uuid
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.shared.coordination import (
    CrossStoreOperationSnapshot,
    CrossStoreOperationState,
    OperationNotFoundError,
    OperationRequest,
)
from impodo.domain.shared.projection_reconciliation import (
    ProjectionRebuildNotPermittedError,
    ProjectionReconciliationNotFoundError,
    ProjectionReconciliationPage,
    ProjectionReconciliationQuery,
)
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    RequestOrganizationScope,
)


class ProjectionReconciliationRepository(Protocol):
    """Read bounded verified receipt facts from the control plane."""

    def list_reconciliation_page(
        self,
        organization_id: OrganizationId,
        project_id: str,
        query: ProjectionReconciliationQuery,
    ) -> ProjectionReconciliationPage:
        """Return one keyset page after verifying its stored receipt hashes."""
        ...

    def get_reconciliation_snapshot(
        self,
        organization_id: OrganizationId,
        project_id: str,
        operation_id: str,
    ) -> CrossStoreOperationSnapshot:
        """Return one exact tenant- and Project-scoped durable operation."""
        ...


class ProjectionRecoveryCoordinator(Protocol):
    """Recover one operation only from its existing durable identity."""

    def recover(self, request: OperationRequest) -> CrossStoreOperationResult:
        """Recover without authorizing a fresh data-plane execution."""
        ...


class ProjectionReconciliationService:
    """Govern administrative scans and receipt-only projection recovery."""

    def __init__(
        self,
        repository: ProjectionReconciliationRepository,
        coordinator: ProjectionRecoveryCoordinator,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._clock = clock or (lambda: datetime.now(UTC))

    def scan(
        self,
        project_id: str,
        query: ProjectionReconciliationQuery,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> ProjectionReconciliationPage:
        """List verified receipt pointers visible to one audit-capable principal."""

        project_id = self._visible_project_id(
            project_id,
            scope=scope,
            authorization=authorization,
            capability=Capability.AUDIT_VIEW,
        )
        return self._repository.list_reconciliation_page(
            scope.organization_id,
            project_id,
            query,
        )

    def rebuild_missing_projection(
        self,
        project_id: str,
        operation_id: str,
        *,
        scope: RequestOrganizationScope,
        authorization: AuthorizationReceipt,
    ) -> CrossStoreOperationResult:
        """Rebuild only a missing projection from verified immutable receipts."""

        project_id = self._visible_project_id(
            project_id,
            scope=scope,
            authorization=authorization,
            capability=Capability.PROJECT_ADMIN,
        )
        try:
            operation_id = require_uuid(operation_id, "operation_id")
            snapshot = self._repository.get_reconciliation_snapshot(
                scope.organization_id,
                project_id,
                operation_id,
            )
        except (MigrationFoundationError, OperationNotFoundError) as error:
            raise ProjectionReconciliationNotFoundError(
                "Operation not found"
            ) from error
        if snapshot.state is not CrossStoreOperationState.DATA_COMMITTED:
            raise ProjectionRebuildNotPermittedError(
                "Only a proven missing receipt projection can be rebuilt"
            )
        return self._coordinator.recover(snapshot.request)

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
            raise ProjectionReconciliationNotFoundError("Project not found") from error
        if not (
            scope.organization_id == authorization.organization_id
            and scope.principal_id == authorization.principal_id
            and authorization.authorizes(
                capability,
                at=self._clock(),
                project_id=project_id,
            )
        ):
            if capability is Capability.PROJECT_ADMIN:
                raise AuthorizationError("Projection rebuild is not authorized")
            raise ProjectionReconciliationNotFoundError("Project not found")
        return project_id


__all__ = [
    "ProjectionReconciliationRepository",
    "ProjectionReconciliationService",
    "ProjectionRecoveryCoordinator",
]
