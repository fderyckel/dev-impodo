"""Application ports for durable cross-store coordination."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.shared.coordination import (
    ControlPlaneProjectionReceipt,
    CrossStoreOperationSnapshot,
    DataPlaneOperationReceipt,
    OperationRequest,
    OperationReservation,
    RecoveryDisposition,
    WorkspaceLease,
)
from impodo.domain.shared.tenancy import OrganizationId


class OperationJournal(Protocol):
    """Reserve semantic work and retain its immutable cross-store receipts."""

    def reserve(self, request: OperationRequest) -> OperationRequest:
        """Create or reuse an exact Organization-scoped idempotency reservation."""
        ...

    def reserve_with_status(
        self,
        request: OperationRequest,
    ) -> OperationReservation:
        """Create or reuse a reservation and identify an idempotent replay."""
        ...

    def get(self, request: OperationRequest) -> CrossStoreOperationSnapshot:
        """Read exact durable state for one Organization-scoped operation."""
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

    def record_interruption(
        self,
        request: OperationRequest,
        *,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        """Record that recovery proved no committed data-plane receipt."""
        ...

    def require_manual_review(
        self,
        request: OperationRequest,
        *,
        interrupted_at: datetime,
        disposition: RecoveryDisposition,
    ) -> None:
        """Stop automatic recovery for ambiguous or substituted evidence."""
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
