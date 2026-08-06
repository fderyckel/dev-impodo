"""Normalization dry-run governance contracts.

The normalization evaluator and DuckDB adapter will eventually produce the
objects defined here. The local browser will consume them to display correction
counts and record the data manager's decisions.

The module is deliberately independent from Odoo preflight, DuckDB, and the
browser framework. It answers only governance questions:

* Is a correction automatic or does it need a manager decision?
* Did normalization create an identity collision?
* May the complete dry run be approved?
* May the approved canonical dataset be frozen?

All models are immutable. Lifecycle methods return a new :class:`DryRun`
instead of modifying the object passed to them. This keeps earlier states
available for an eventual append-only audit trail.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import json
from types import MappingProxyType
from typing import Any, Mapping

from .access import Actor, Capability
from .approvals import ApprovalEvidence


class ApprovalMode(StrEnum):
    """How a deterministic correction is authorized within a dry run.

    ``AUTOMATIC`` rules are low-risk rules approved in advance, such as
    trimming ordinary leading and trailing whitespace. Their impact is still
    included in the dry-run summary and the whole run still needs approval.

    ``REQUIRED`` rules need an explicit group decision before the whole run can
    be approved. They cover corrections that may change business meaning or
    identity.
    """

    AUTOMATIC = "automatic"
    REQUIRED = "required"


class DryRunStatus(StrEnum):
    """Lifecycle states for normalization and validation.

    A new run starts as ``RUNNING``. :meth:`DryRun.complete` moves it to
    ``REVIEW_REQUIRED`` when the summary is safe to review, or directly to
    ``BLOCKED`` when blocking issues or normalization collisions exist.
    Approval produces ``APPROVED`` and hashing the final canonical dataset
    produces ``FROZEN``.
    """

    RUNNING = "RUNNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    FROZEN = "FROZEN"


class DryRunTransitionError(ValueError):
    """Raised when a dry-run lifecycle transition is not permitted."""


class CorrectionDecisionKind(StrEnum):
    """Reviewer choice recorded for one required normalization group."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True, order=True)
class CorrectionGroupKey:
    """Stable identity for one manager-facing correction group.

    The browser groups corrections by rule, dataset, and field so a data
    manager can review one coherent business decision instead of approving
    thousands of individual rows.

    Attributes:
        rule_id: Stable identifier of the normalization rule.
        dataset: Logical source dataset, for example ``products``.
        field: Source or governed field affected by the rule.
    """

    rule_id: str
    dataset: str
    field: str

    def __post_init__(self) -> None:
        """Reject keys that cannot be displayed, stored, or audited safely."""

        _require_text(self.rule_id, "rule_id")
        _require_text(self.dataset, "dataset")
        _require_text(self.field, "field")

    def to_portable_dict(self) -> dict[str, str]:
        """Serialize the stable rule/dataset/field group identity."""

        return {
            "rule_id": self.rule_id,
            "dataset": self.dataset,
            "field": self.field,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CorrectionGroupKey":
        """Reconstruct a group key from persisted decision evidence."""

        return cls(
            rule_id=str(payload["rule_id"]),
            dataset=str(payload["dataset"]),
            field=str(payload["field"]),
        )


@dataclass(frozen=True, slots=True)
class CorrectionImpact:
    """Aggregated impact of one rule on one dataset field.

    ``affected_count`` counts rows whose value would change. A collision means
    two or more governed identities become equal after correction. A collision
    always blocks the dry run, including when the rule itself is automatic.

    Attributes:
        key: Manager-facing group identity.
        approval_mode: Whether this group needs a separate decision.
        affected_count: Number of changed rows in the group.
        collision_count: Number of affected rows involved in collisions.
    """

    key: CorrectionGroupKey
    approval_mode: ApprovalMode
    affected_count: int
    collision_count: int = 0

    def __post_init__(self) -> None:
        """Ensure the summary cannot contain impossible reconciliation counts."""

        if self.affected_count < 1:
            raise ValueError("affected_count must be at least 1")
        if self.collision_count < 0:
            raise ValueError("collision_count cannot be negative")
        if self.collision_count > self.affected_count:
            raise ValueError("collision_count cannot exceed affected_count")

    @property
    def blocking(self) -> bool:
        """Return whether this correction group makes the dry run unsafe."""

        return self.collision_count > 0

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize the correction counts and approval policy."""

        return {
            "key": self.key.to_portable_dict(),
            "approval_mode": self.approval_mode.value,
            "affected_count": self.affected_count,
            "collision_count": self.collision_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CorrectionImpact":
        """Reconstruct and validate an aggregated correction impact."""

        return cls(
            key=CorrectionGroupKey.from_dict(dict(payload["key"])),
            approval_mode=ApprovalMode(str(payload["approval_mode"])),
            affected_count=int(payload["affected_count"]),
            collision_count=int(payload.get("collision_count", 0)),
        )


@dataclass(frozen=True, slots=True)
class CorrectionDecision:
    """Auditable decision by one verified normalization reviewer."""

    key: CorrectionGroupKey
    decision: CorrectionDecisionKind
    evidence: ApprovalEvidence

    def __post_init__(self) -> None:
        if self.evidence.capability is not Capability.NORMALIZATION_DECIDE:
            raise ValueError(
                "correction decisions require normalization.decide evidence"
            )

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize the decision together with authorization evidence."""

        return {
            "key": self.key.to_portable_dict(),
            "decision": self.decision.value,
            "evidence": self.evidence.to_portable_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CorrectionDecision":
        """Reconstruct and authorize-check a stored group decision."""

        return cls(
            key=CorrectionGroupKey.from_dict(dict(payload["key"])),
            decision=CorrectionDecisionKind(str(payload["decision"])),
            evidence=ApprovalEvidence.from_dict(dict(payload["evidence"])),
        )


@dataclass(frozen=True, slots=True)
class DryRunSummary:
    """Reconciled correction and blocking counts produced by a dry run.

    This is the summary-table contract for the future browser. Row-level
    correction evidence will live in the staging store; this object carries
    only the grouped counts and decision keys required by the lifecycle.

    Attributes:
        corrections: One aggregate for each unique rule/dataset/field group.
        blocking_issue_count: Validation errors unrelated to correction
            collisions, such as missing required values.
    """

    corrections: tuple[CorrectionImpact, ...] = ()
    blocking_issue_count: int = 0

    def __post_init__(self) -> None:
        """Reject negative counts and duplicate summary-table rows."""

        if self.blocking_issue_count < 0:
            raise ValueError("blocking_issue_count cannot be negative")
        keys = [correction.key for correction in self.corrections]
        duplicates = sorted(
            key for key, count in Counter(keys).items() if count > 1
        )
        if duplicates:
            rendered = ", ".join(
                f"{key.dataset}.{key.field}:{key.rule_id}" for key in duplicates
            )
            raise ValueError(f"duplicate correction groups: {rendered}")

    @property
    def total_correction_count(self) -> int:
        """Return all corrected or proposed rows across every group."""

        return sum(correction.affected_count for correction in self.corrections)

    @property
    def automatic_correction_count(self) -> int:
        """Return rows changed by pre-approved automatic rules."""

        return sum(
            correction.affected_count
            for correction in self.corrections
            if correction.approval_mode == ApprovalMode.AUTOMATIC
        )

    @property
    def approval_required_correction_count(self) -> int:
        """Return rows awaiting an explicit grouped manager decision."""

        return sum(
            correction.affected_count
            for correction in self.corrections
            if correction.approval_mode == ApprovalMode.REQUIRED
        )

    @property
    def required_group_keys(self) -> frozenset[CorrectionGroupKey]:
        """Return the exact group decisions required before run approval."""

        return frozenset(
            correction.key
            for correction in self.corrections
            if correction.approval_mode == ApprovalMode.REQUIRED
        )

    @property
    def blocked(self) -> bool:
        """Return whether validation errors or collisions make the run unsafe."""

        return self.blocking_issue_count > 0 or any(
            correction.blocking for correction in self.corrections
        )

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "corrections": [
                item.to_portable_dict() for item in self.corrections
            ],
            "blocking_issue_count": self.blocking_issue_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DryRunSummary":
        return cls(
            corrections=tuple(
                CorrectionImpact.from_dict(item)
                for item in payload.get("corrections", ())
            ),
            blocking_issue_count=int(payload.get("blocking_issue_count", 0)),
        )


@dataclass(frozen=True, slots=True)
class DryRun:
    """Immutable lifecycle state for one normalization and validation run.

    A run is bound to exact source-file hashes and one ruleset hash from the
    moment it starts. Decisions are retained as correction-group keys. Final
    approval records the manager and timezone-aware timestamp. Freezing then
    binds that approval to the exact canonical dataset hash.

    The class does not store row values or execute rules. It coordinates the
    lifecycle around results produced by those future components.

    Attributes:
        run_id: Stable identifier of this dry-run attempt.
        source_hashes: Exact source filename-to-SHA-256 bindings.
        ruleset_hash: SHA-256 binding of the normalization rule contract.
        status: Current lifecycle state.
        summary: Grouped results after execution completes.
        group_decisions: Immutable actor-bound decisions for required groups.
        approval: Actor-bound whole-run approval evidence.
        canonical_dataset_hash: Final canonical-data binding, present only
            after freezing.
    """

    run_id: str
    source_hashes: Mapping[str, str]
    ruleset_hash: str
    status: DryRunStatus = DryRunStatus.RUNNING
    summary: DryRunSummary | None = None
    group_decisions: tuple[CorrectionDecision, ...] = ()
    approval: ApprovalEvidence | None = None
    canonical_dataset_hash: str | None = None

    def __post_init__(self) -> None:
        """Enforce invariants that must hold in every lifecycle state.

        The source mapping is copied, sorted, and wrapped in
        :class:`types.MappingProxyType`. Callers therefore cannot mutate source
        evidence after creating the run.
        """

        _require_text(self.run_id, "run_id")
        if not self.source_hashes:
            raise ValueError("source_hashes must contain at least one source file")
        immutable_hashes = {
            _require_text(name, "source filename"): _require_sha256(
                content_hash, f"source hash for {name!r}"
            )
            for name, content_hash in self.source_hashes.items()
        }
        object.__setattr__(
            self,
            "source_hashes",
            MappingProxyType(dict(sorted(immutable_hashes.items()))),
        )
        _require_sha256(self.ruleset_hash, "ruleset_hash")
        if self.canonical_dataset_hash is not None:
            _require_sha256(self.canonical_dataset_hash, "canonical_dataset_hash")

        known_required = (
            self.summary.required_group_keys
            if self.summary is not None
            else frozenset()
        )
        decision_keys = tuple(item.key for item in self.group_decisions)
        if len(set(decision_keys)) != len(decision_keys):
            raise ValueError("a correction group can have only one decision")
        unknown = set(decision_keys).difference(known_required)
        if unknown:
            raise ValueError("only approval-required correction groups may be decided")

        if self.status == DryRunStatus.RUNNING and self.summary is not None:
            raise ValueError("a running dry run cannot already have a summary")
        if self.status != DryRunStatus.RUNNING and self.summary is None:
            raise ValueError("a completed dry run requires a summary")
        if self.status in {DryRunStatus.APPROVED, DryRunStatus.FROZEN}:
            if self.approval is None:
                raise ValueError("an approved dry run requires approval evidence")
            if self.approval.capability is not Capability.NORMALIZATION_APPROVE:
                raise ValueError(
                    "dry-run approval requires normalization.approve evidence"
                )
        elif self.approval is not None:
            raise ValueError("approval evidence is retained only after approval")
        if self.status == DryRunStatus.FROZEN and self.canonical_dataset_hash is None:
            raise ValueError("a frozen dry run requires canonical_dataset_hash")
        if (
            self.status != DryRunStatus.FROZEN
            and self.canonical_dataset_hash is not None
        ):
            raise ValueError(
                "canonical_dataset_hash is retained only after the dry run is frozen"
            )

    @property
    def approved_groups(self) -> frozenset[CorrectionGroupKey]:
        return frozenset(
            item.key
            for item in self.group_decisions
            if item.decision is CorrectionDecisionKind.APPROVED
        )

    @property
    def rejected_groups(self) -> frozenset[CorrectionGroupKey]:
        return frozenset(
            item.key
            for item in self.group_decisions
            if item.decision is CorrectionDecisionKind.REJECTED
        )

    def complete(self, summary: DryRunSummary) -> "DryRun":
        """Attach execution results and return the next review state.

        Args:
            summary: Reconciled correction and blocking counts.

        Returns:
            ``BLOCKED`` when the summary contains a blocking issue or
            collision; otherwise ``REVIEW_REQUIRED``.

        Raises:
            DryRunTransitionError: If the run is no longer ``RUNNING``.
        """

        self._require_status(DryRunStatus.RUNNING)
        status = (
            DryRunStatus.BLOCKED
            if summary.blocked
            else DryRunStatus.REVIEW_REQUIRED
        )
        return replace(self, status=status, summary=summary)

    def approve_group(
        self,
        key: CorrectionGroupKey,
        *,
        actor: Actor,
        decided_at: datetime,
        reason: str = "",
    ) -> "DryRun":
        """Approve one correction group whose approval mode is ``required``.

        Automatic groups cannot receive group approval because their rule
        policy already authorizes the correction. They remain visible through
        :class:`DryRunSummary` and whole-run approval.

        The returned object remains in ``REVIEW_REQUIRED`` until all required
        groups are decided and :meth:`approve` is called.
        """

        self._require_status(DryRunStatus.REVIEW_REQUIRED)
        assert self.summary is not None
        if key not in self.summary.required_group_keys:
            raise DryRunTransitionError(
                "only approval-required correction groups can be approved"
            )
        decision = CorrectionDecision(
            key=key,
            decision=CorrectionDecisionKind.APPROVED,
            evidence=ApprovalEvidence.from_actor(
                actor,
                capability=Capability.NORMALIZATION_DECIDE,
                approved_at=decided_at,
                reason=reason,
            ),
        )
        return replace(self, group_decisions=self._append_decision(decision))

    def reject_group(
        self,
        key: CorrectionGroupKey,
        *,
        actor: Actor,
        decided_at: datetime,
        reason: str = "",
    ) -> "DryRun":
        """Reject one required correction group and block this dry run.

        Rejection is fail-closed. The same dry run cannot later be approved;
        the data manager must correct the source or rules and start a new run.
        """

        self._require_status(DryRunStatus.REVIEW_REQUIRED)
        assert self.summary is not None
        if key not in self.summary.required_group_keys:
            raise DryRunTransitionError(
                "only approval-required correction groups can be rejected"
            )
        decision = CorrectionDecision(
            key=key,
            decision=CorrectionDecisionKind.REJECTED,
            evidence=ApprovalEvidence.from_actor(
                actor,
                capability=Capability.NORMALIZATION_DECIDE,
                approved_at=decided_at,
                reason=reason,
            ),
        )
        return replace(
            self,
            status=DryRunStatus.BLOCKED,
            group_decisions=self._append_decision(decision),
        )

    def approve(
        self,
        *,
        actor: Actor,
        approved_at: datetime,
        reason: str = "",
    ) -> "DryRun":
        """Approve the complete reviewed dry run.

        Whole-run approval is required even when every correction is
        automatic. For required corrections, every group must first be present
        in :attr:`approved_groups`.

        Args:
            actor: Verified actor with normalization approval capability.
            approved_at: Timezone-aware approval timestamp.

        Returns:
            A new dry run in ``APPROVED`` status.
        """

        self._require_status(DryRunStatus.REVIEW_REQUIRED)
        assert self.summary is not None
        pending = self.summary.required_group_keys.difference(self.approved_groups)
        if pending:
            raise DryRunTransitionError(
                f"{len(pending)} correction group(s) still require approval"
            )
        return replace(
            self,
            status=DryRunStatus.APPROVED,
            approval=ApprovalEvidence.from_actor(
                actor,
                capability=Capability.NORMALIZATION_APPROVE,
                approved_at=approved_at,
                reason=reason,
            ),
        )

    def freeze(self, *, canonical_dataset_hash: str) -> "DryRun":
        """Bind an approved run to the exact canonical dataset.

        Freezing does not write to Odoo. It only records the SHA-256 hash of the
        normalized and validated dataset that later stages may consume.
        """

        self._require_status(DryRunStatus.APPROVED)
        return replace(
            self,
            status=DryRunStatus.FROZEN,
            canonical_dataset_hash=_require_sha256(
                canonical_dataset_hash, "canonical_dataset_hash"
            ),
        )

    def _require_status(self, expected: DryRunStatus) -> None:
        """Raise an actionable error when a lifecycle method is called early."""

        if self.status != expected:
            raise DryRunTransitionError(
                f"dry run must be {expected.value}; "
                f"current status is {self.status.value}"
            )

    def _append_decision(
        self,
        decision: CorrectionDecision,
    ) -> tuple[CorrectionDecision, ...]:
        if any(item.key == decision.key for item in self.group_decisions):
            raise DryRunTransitionError("correction group is already decided")
        return tuple(
            sorted(self.group_decisions + (decision,), key=lambda item: item.key)
        )

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_hashes": dict(self.source_hashes),
            "ruleset_hash": self.ruleset_hash,
            "status": self.status.value,
            "summary": (
                self.summary.to_portable_dict()
                if self.summary is not None
                else None
            ),
            "group_decisions": [
                item.to_portable_dict() for item in self.group_decisions
            ],
            "approval": (
                self.approval.to_portable_dict()
                if self.approval is not None
                else None
            ),
            "canonical_dataset_hash": self.canonical_dataset_hash,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_portable_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DryRun":
        summary_payload = payload.get("summary")
        approval_payload = payload.get("approval")
        return cls(
            run_id=str(payload["run_id"]),
            source_hashes={
                str(key): str(value)
                for key, value in dict(payload["source_hashes"]).items()
            },
            ruleset_hash=str(payload["ruleset_hash"]),
            status=DryRunStatus(str(payload.get("status", "RUNNING"))),
            summary=(
                DryRunSummary.from_dict(dict(summary_payload))
                if summary_payload is not None
                else None
            ),
            group_decisions=tuple(
                CorrectionDecision.from_dict(item)
                for item in payload.get("group_decisions", ())
            ),
            approval=(
                ApprovalEvidence.from_dict(dict(approval_payload))
                if approval_payload is not None
                else None
            ),
            canonical_dataset_hash=(
                str(payload["canonical_dataset_hash"])
                if payload.get("canonical_dataset_hash")
                else None
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "DryRun":
        return cls.from_dict(json.loads(value))


def _require_text(value: str | None, name: str) -> str:
    """Return a non-blank string or raise a field-specific validation error."""

    if value is None or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _require_sha256(value: str, name: str) -> str:
    """Validate and return the canonical ``sha256:<hex>`` representation."""

    prefix = "sha256:"
    digest = value.removeprefix(prefix)
    if not value.startswith(prefix) or len(digest) != 64:
        raise ValueError(f"{name} must use the sha256:<64 hex characters> format")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ValueError(
            f"{name} must use the sha256:<64 hex characters> format"
        ) from exc
    return value
