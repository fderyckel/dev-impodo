"""Bind Final review submissions to the portable durable-job contract.

This module contains no storage or worker implementation.  It translates one
already-frozen comparison request into the value-free request accepted by a
future shared-server job control plane.  The command manifest is represented
only by its hash, so credentials, callbacks, filesystem paths, and database
owners cannot become durable job input through this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from impodo.application.preflight_attempts import PreflightComparisonRequest
from impodo.application.shared.jobs import DurableJobControl
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.shared.jobs import (
    DurableJobKind,
    DurableJobRequest,
)
from impodo.domain.shared.tenancy import AuthorizationReceipt


class DurablePreflightJobConflictError(RuntimeError):
    """Reject a control-plane response with substituted request meaning."""


@dataclass(frozen=True, slots=True)
class DurablePreflightSubmission:
    """Server-safe facts needed to reserve one Final review job.

    ``command_manifest_hash`` identifies the immutable worker command, not the
    readiness manifest produced by a successful comparison.  Its referenced
    payload is owned by future server composition and may contain only opaque
    resource and secret-reference identities.
    """

    comparison: PreflightComparisonRequest
    job_id: str
    idempotency_key: str
    command_manifest_hash: str
    authorization: AuthorizationReceipt
    authorization_revision: int

    def to_job_request(self) -> DurableJobRequest:
        """Return the exact portable request after rechecking authorization."""

        if self.comparison.requested_at < self.authorization.issued_at:
            raise AuthorizationError(
                "Final review authorization was issued after the request"
            )
        if not self.authorization.authorizes(
            Capability.PREFLIGHT_RUN,
            at=self.comparison.requested_at,
            project_id=self.comparison.project_id,
        ):
            raise AuthorizationError(
                "Final review request is not authorized for this Project"
            )
        return DurableJobRequest(
            job_id=self.job_id,
            operation_id=self.comparison.operation_id,
            organization_id=self.authorization.organization_id,
            project_id=self.comparison.project_id,
            workspace_id=self.comparison.workspace_id,
            job_kind=DurableJobKind.PREFLIGHT,
            idempotency_key=self.idempotency_key,
            semantic_input_hash=self.comparison.semantic_input_hash,
            manifest_hash=self.command_manifest_hash,
            authorization_receipt_id=self.authorization.receipt_id,
            authorization_revision=self.authorization_revision,
            requested_by=self.authorization.actor_identity,
            requested_at=self.comparison.requested_at,
        )


class DurablePreflightJobService:
    """Reserve one exact Final review request through durable job control."""

    def __init__(self, jobs: DurableJobControl) -> None:
        self._jobs = jobs

    def enqueue(
        self,
        submission: DurablePreflightSubmission,
    ) -> DurableJobRequest:
        """Create or reuse the same request, rejecting semantic substitution."""

        requested = submission.to_job_request()
        accepted = self._jobs.enqueue(requested)
        if accepted != requested:
            raise DurablePreflightJobConflictError(
                "Durable Final review reservation returned different request facts"
            )
        return accepted


__all__ = [
    "DurablePreflightJobConflictError",
    "DurablePreflightJobService",
    "DurablePreflightSubmission",
]
