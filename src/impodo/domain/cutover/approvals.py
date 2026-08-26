"""Immutable approval evidence used by normalization and future Stage I plans.

``ApprovalEvidence`` is integrated into the Stage-G ``DryRun`` decision state.
``FrozenExportPlan`` and ``ExportPlanApproval`` are standalone domain contracts
for a future clean-package/import-plan workflow: no application service,
repository, browser route, or executor currently creates or consumes them.
Their presence must not be interpreted as Odoo write authorization.

Stages J-K remain outside this module. The practical local writer and journal
do not consume these optional higher-risk approvals, and post-write
reconciliation is not implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from impodo.domain.shared.access import Actor, ActorIdentity, Capability


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    """Who approved one exact decision, when, and under which capability.

    Stage G uses this inside ``governance.CorrectionDecision`` and the final
    normalization approval. Stable issuer/subject identity is retained rather
    than trusting the display name as identity.
    """

    approved_by: ActorIdentity
    approved_at: datetime
    capability: Capability
    reason: str = ""

    def __post_init__(self) -> None:
        if self.approved_at.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        clean_reason = self.reason.strip()
        if len(clean_reason) > 2_000:
            raise ValueError("approval reason is too long")
        object.__setattr__(self, "reason", clean_reason)

    @classmethod
    def from_actor(
        cls,
        actor: Actor,
        *,
        capability: Capability,
        approved_at: datetime,
        reason: str = "",
    ) -> "ApprovalEvidence":
        """Create evidence only when ``actor`` owns the claimed capability."""

        if not actor.has(capability):
            raise PermissionError(
                f"{actor.identity.display_name} lacks {capability.value}"
            )
        return cls(
            approved_by=actor.identity,
            approved_at=approved_at,
            capability=capability,
            reason=reason.strip(),
        )

    def to_portable_dict(self) -> dict[str, object]:
        """Serialize stable actor identity, capability, time, and reason."""

        return {
            "approved_by": {
                "issuer": self.approved_by.issuer,
                "subject_id": self.approved_by.subject_id,
                "display_name": self.approved_by.display_name,
            },
            "approved_at": self.approved_at.isoformat(),
            "capability": self.capability.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ApprovalEvidence":
        """Reconstruct validated evidence from a portable decision payload."""

        actor_payload = dict(payload["approved_by"])
        return cls(
            approved_by=ActorIdentity(
                issuer=str(actor_payload["issuer"]),
                subject_id=str(actor_payload["subject_id"]),
                display_name=str(actor_payload["display_name"]),
            ),
            approved_at=datetime.fromisoformat(str(payload["approved_at"])),
            capability=Capability(str(payload["capability"])),
            reason=str(payload.get("reason", "")),
        )


@dataclass(frozen=True, slots=True)
class FrozenExportPlan:
    """Future Stage-I contract binding every proposed execution input.

    This value object is not built by the current Stage-H readiness workflow.
    A future clean-package service must define and verify ``actions_hash`` and
    every upstream/target binding before persisting one of these plans.
    """

    plan_id: str
    workspace_id: str
    run_id: str
    source_hashes: Mapping[str, str]
    mapping_hash: str
    ruleset_hash: str
    canonical_dataset_hash: str
    target_snapshot_hash: str
    actions_hash: str
    frozen_at: datetime
    contract_version: int = 3

    def __post_init__(self) -> None:
        for name in ("plan_id", "workspace_id", "run_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        if self.contract_version != 3:
            raise ValueError("unsupported export-plan contract version")
        if self.frozen_at.utcoffset() is None:
            raise ValueError("frozen_at must be timezone-aware")
        if not self.source_hashes:
            raise ValueError("source_hashes must not be empty")
        immutable_hashes = {
            _required_text(name, "source name"): _sha256_hash(
                value,
                f"source hash for {name!r}",
            )
            for name, value in self.source_hashes.items()
        }
        object.__setattr__(
            self,
            "source_hashes",
            MappingProxyType(dict(sorted(immutable_hashes.items()))),
        )
        for name in (
            "mapping_hash",
            "ruleset_hash",
            "canonical_dataset_hash",
            "target_snapshot_hash",
            "actions_hash",
        ):
            _sha256_hash(getattr(self, name), name)

    @property
    def semantic_hash(self) -> str:
        """Return a deterministic binding for every execution-relevant input."""

        payload = {
            "actions_hash": self.actions_hash,
            "canonical_dataset_hash": self.canonical_dataset_hash,
            "contract_version": self.contract_version,
            "frozen_at": self.frozen_at.isoformat(),
            "mapping_hash": self.mapping_hash,
            "plan_id": self.plan_id,
            "workspace_id": self.workspace_id,
            "ruleset_hash": self.ruleset_hash,
            "run_id": self.run_id,
            "source_hashes": dict(self.source_hashes),
            "target_snapshot_hash": self.target_snapshot_hash,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ExportPlanApproval:
    """Future approval of one exact frozen plan, never a generic Odoo write.

    A later executor would still need a separately authorized, idempotent,
    journaled Stage-J operation and must re-check plan hash and expiry. No such
    executor or persistence integration exists today.
    """

    approval_id: str
    plan_hash: str
    evidence: ApprovalEvidence
    policy_version: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "approval_id",
            _required_text(self.approval_id, "approval_id"),
        )
        _sha256_hash(self.plan_hash, "plan_hash")
        object.__setattr__(
            self,
            "policy_version",
            _required_text(self.policy_version, "policy_version"),
        )
        if self.evidence.capability is not Capability.EXPORT_PLAN_APPROVE:
            raise ValueError("export approval requires export_plan.approve evidence")
        if self.expires_at is not None:
            if self.expires_at.utcoffset() is None:
                raise ValueError("expires_at must be timezone-aware")
            if self.expires_at <= self.evidence.approved_at:
                raise ValueError("expires_at must be after approved_at")

    @classmethod
    def approve(
        cls,
        plan: FrozenExportPlan,
        *,
        approval_id: str,
        actor: Actor,
        approved_at: datetime,
        policy_version: str,
        expires_at: datetime | None = None,
        reason: str = "",
    ) -> "ExportPlanApproval":
        """Approve exactly ``plan`` using a verified key-user capability."""

        return cls(
            approval_id=approval_id,
            plan_hash=plan.semantic_hash,
            evidence=ApprovalEvidence.from_actor(
                actor,
                capability=Capability.EXPORT_PLAN_APPROVE,
                approved_at=approved_at,
                reason=reason,
            ),
            policy_version=policy_version,
            expires_at=expires_at,
        )

    def authorizes(self, plan: FrozenExportPlan, *, at: datetime) -> bool:
        """Return whether this evidence still applies to the exact plan."""

        if at.utcoffset() is None:
            raise ValueError("authorization time must be timezone-aware")
        return (
            self.plan_hash == plan.semantic_hash
            and (self.expires_at is None or at < self.expires_at)
        )


def _required_text(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    if len(cleaned) > 500:
        raise ValueError(f"{name} is too long")
    return cleaned


def _sha256_hash(value: str, name: str) -> str:
    digest = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(digest) != 64:
        raise ValueError(f"{name} must use sha256:<64 hex characters>")
    try:
        int(digest, 16)
    except ValueError as error:
        raise ValueError(f"{name} must use sha256:<64 hex characters>") from error
    return value
