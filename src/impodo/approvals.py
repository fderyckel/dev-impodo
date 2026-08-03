"""Immutable approval evidence for normalization and future target exports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping

from .access import Actor, ActorIdentity, Capability


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    """Who approved one exact decision, when, and under which capability."""

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


@dataclass(frozen=True, slots=True)
class FrozenExportPlan:
    """Exact immutable evidence that a later restricted executor may consume."""

    plan_id: str
    project_id: str
    run_id: str
    source_hashes: Mapping[str, str]
    mapping_hash: str
    ruleset_hash: str
    canonical_dataset_hash: str
    target_snapshot_hash: str
    actions_hash: str
    frozen_at: datetime
    contract_version: int = 2

    def __post_init__(self) -> None:
        for name in ("plan_id", "project_id", "run_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name),
            )
        if self.contract_version != 2:
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
            "project_id": self.project_id,
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
    """Approval of one frozen plan; it never grants a generic Odoo write."""

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
