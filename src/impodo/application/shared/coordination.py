"""Application ports for future durable cross-store coordination."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.shared.coordination import (
    ControlPlaneProjectionReceipt,
    DataPlaneOperationReceipt,
    OperationRequest,
    WorkspaceLease,
)
from impodo.domain.shared.tenancy import OrganizationId


class OperationJournal(Protocol):
    """Reserve semantic work and retain its immutable cross-store receipts."""

    def reserve(self, request: OperationRequest) -> OperationRequest:
        """Create or reuse an exact Organization-scoped idempotency reservation."""
        ...

    def record_data_plane_receipt(
        self,
        receipt: DataPlaneOperationReceipt,
    ) -> None:
        """Attach an exact committed data-plane result to its operation."""
        ...

    def record_projection_receipt(
        self,
        receipt: ControlPlaneProjectionReceipt,
    ) -> None:
        """Record that bounded control state projected the exact data receipt."""
        ...


class WorkspaceLeaseRepository(Protocol):
    """Own exclusive Workspace leases and monotonic fencing tokens."""

    def acquire(
        self,
        *,
        organization_id: OrganizationId,
        project_id: str,
        workspace_id: str,
        holder_attempt_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        """Acquire an unowned or expired Workspace using a newer token."""
        ...

    def heartbeat(
        self,
        lease: WorkspaceLease,
        *,
        at: datetime,
        expires_at: datetime,
    ) -> WorkspaceLease:
        """Extend only the current lease version and fencing token."""
        ...

    def release(self, lease: WorkspaceLease, *, at: datetime) -> WorkspaceLease:
        """Release only the current lease version and fencing token."""
        ...
