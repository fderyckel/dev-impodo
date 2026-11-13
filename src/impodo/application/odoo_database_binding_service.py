"""Authorize explicit database reset declarations without claiming detection.

First review requires a verified request receipt. Workers subsequently check
the immutable stored Organization binding after ordinary workspace access.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from impodo.application.odoo_database_binding_ports import OdooDatabaseBindingRepository
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
)
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingError,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    OdooDatabaseGenerationConflict,
    database_scope_hash,
)
from impodo.domain.project.foundation import require_hash, require_uuid
from impodo.domain.shared.access import (
    Actor,
    AuthorizationError,
    AuthorizationPolicy,
    Capability,
)
from impodo.domain.shared.tenancy import AuthorizationReceipt, RequestOrganizationScope


class OdooDatabaseBindingService:
    """Share one declared generation across every Project and Company in an Organization."""

    def __init__(
        self,
        access: WorkspaceAccessService,
        authorization: AuthorizationPolicy,
        repository: OdooDatabaseBindingRepository,
        *,
        review_admission: Callable[[WorkspaceAccessContext, OdooDatabaseBindingRole, str], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.access = access
        self.authorization = authorization
        self.repository = repository
        self._review_admission = review_admission
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(
        self,
        workspace_id: str,
        role: OdooDatabaseBindingRole | str,
        *,
        base_url: str,
        database: str,
        actor: Actor,
        capability: Capability,
    ) -> OdooDatabaseBinding | None:
        """Read one immutable proof only after authorizing its actual Project."""

        owner = self.access.require(actor, capability, workspace_id=workspace_id)
        binding = self.repository.get(
            owner.workspace_id,
            OdooDatabaseBindingRole(role),
            database_scope_hash(base_url, database),
        )
        if binding is not None and binding.project_id != owner.project_id:
            raise OdooDatabaseBindingError(
                "The reviewed database binding belongs to another Project"
            )
        return binding

    def current(
        self,
        workspace_id: str,
        *,
        base_url: str,
        database: str,
        actor: Actor,
        capability: Capability,
        scope: RequestOrganizationScope,
        authorization_receipt: AuthorizationReceipt,
    ) -> OdooDatabaseGeneration:
        """Read a deterministic initial generation without writing on a page read."""

        owner = self.access.require(actor, capability, workspace_id=workspace_id)
        self._request(owner, actor, capability, scope, authorization_receipt)
        return self.repository.current(
            str(scope.organization_id), database_scope_hash(base_url, database)
        )

    def bind(
        self,
        workspace_id: str,
        role: OdooDatabaseBindingRole | str,
        *,
        base_url: str,
        database: str,
        expected_generation_hash: str,
        operation_id: str,
        actor: Actor,
        capability: Capability,
        scope: RequestOrganizationScope,
        authorization_receipt: AuthorizationReceipt,
    ) -> OdooDatabaseBinding:
        """Admit first review once; a reset never overwrites an earlier proof."""

        owner = self.access.require(actor, capability, workspace_id=workspace_id)
        self._request(owner, actor, capability, scope, authorization_receipt)
        require_hash(expected_generation_hash, "expected_generation_hash")
        binding = OdooDatabaseBinding(
            workspace_id=owner.workspace_id,
            project_id=owner.project_id,
            organization_id=str(scope.organization_id),
            role=OdooDatabaseBindingRole(role),
            scope_hash=database_scope_hash(base_url, database),
            generation_hash=expected_generation_hash,
            actor=actor.identity,
            bound_at=self._clock(),
            operation_id=require_uuid(operation_id, "operation_id"),
        )
        if self.repository.get(owner.workspace_id, binding.role, binding.scope_hash) is None:
            self._review_admission(owner, binding.role, binding.scope_hash)
        return self.repository.bind(
            binding, expected_generation_hash=expected_generation_hash
        )

    def declare_recreated(
        self,
        workspace_id: str,
        *,
        base_url: str,
        database: str,
        expected_generation_hash: str,
        operation_id: str,
        reason: str,
        actor: Actor,
        scope: RequestOrganizationScope,
        authorization_receipt: AuthorizationReceipt,
    ) -> OdooDatabaseGeneration:
        """Declare a reset for the whole Organization with compare-and-swap authority."""

        owner = self.access.require(
            actor, Capability.PROJECT_ADMIN, workspace_id=workspace_id
        )
        self.authorization.require(
            actor, Capability.PROJECT_ADMIN, project_id=owner.project_id
        )
        self._request(
            owner,
            actor,
            Capability.PROJECT_ADMIN,
            scope,
            authorization_receipt,
            organization_wide=True,
        )
        require_hash(expected_generation_hash, "expected_generation_hash")
        scope_hash = database_scope_hash(base_url, database)
        current = self.repository.current(str(scope.organization_id), scope_hash)
        # The repository resolves an exact operation replay before checking CAS.
        # Its generation ordinal is derived from the expected previous record.
        generation = OdooDatabaseGeneration(
            organization_id=str(scope.organization_id),
            scope_hash=scope_hash,
            generation=current.generation + 1,
            previous_generation_hash=expected_generation_hash,
            operation_id=require_uuid(operation_id, "operation_id"),
            actor=actor.identity,
            declared_at=self._clock(),
            reason=reason,
        )
        return self.repository.declare(
            generation, expected_generation_hash=expected_generation_hash
        )

    def require_current(
        self,
        workspace_id: str,
        role: OdooDatabaseBindingRole | str,
        *,
        base_url: str,
        database: str,
        actor: Actor,
        capability: Capability,
    ) -> OdooDatabaseBinding:
        """Reject unreviewed or superseded authority before opening remote access."""

        owner = self.access.require(actor, capability, workspace_id=workspace_id)
        return self.repository.assert_current(
            owner.workspace_id,
            OdooDatabaseBindingRole(role),
            database_scope_hash(base_url, database),
            project_id=owner.project_id,
        )

    def assert_current(
        self, binding: OdooDatabaseBinding, *, actor: Actor, capability: Capability
    ) -> OdooDatabaseBinding:
        """Verify one exact frozen proof without reinterpreting endpoint details."""

        owner = self.access.require(
            actor, capability, workspace_id=binding.workspace_id
        )
        current = self.repository.assert_current(
            owner.workspace_id,
            binding.role,
            binding.scope_hash,
            project_id=owner.project_id,
        )
        if current.content_hash != binding.content_hash:
            raise OdooDatabaseGenerationConflict(
                "The reviewed database authority changed; start a fresh review workspace"
            )
        return current

    def _request(
        self,
        owner: WorkspaceAccessContext,
        actor: Actor,
        capability: Capability,
        scope: RequestOrganizationScope,
        receipt: AuthorizationReceipt,
        *,
        organization_wide: bool = False,
    ) -> None:
        now = self._clock()
        if (
            scope.organization_id != receipt.organization_id
            or scope.principal_id != receipt.principal_id
            or actor.identity != receipt.actor_identity
            or receipt.issued_at > now
            or not receipt.authorizes(
                Capability.PROJECT_VIEW, at=now, project_id=owner.project_id
            )
            or not receipt.authorizes(capability, at=now, project_id=owner.project_id)
            or (organization_wide and not receipt.project_visibility.organization_wide)
        ):
            raise AuthorizationError(
                "The database action requires a verified matching Organization and Project authorization"
            )
