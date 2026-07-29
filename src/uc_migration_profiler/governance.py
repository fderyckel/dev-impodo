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

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


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
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
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
        approved_groups: Required correction groups accepted by the manager.
        rejected_groups: Required correction groups rejected by the manager.
        approved_by: Data-manager identity for whole-run approval.
        approved_at: Timezone-aware whole-run approval time.
        canonical_dataset_hash: Final canonical-data binding, present only
            after freezing.
    """

    run_id: str
    source_hashes: Mapping[str, str]
    ruleset_hash: str
    status: DryRunStatus = DryRunStatus.RUNNING
    summary: DryRunSummary | None = None
    approved_groups: frozenset[CorrectionGroupKey] = frozenset()
    rejected_groups: frozenset[CorrectionGroupKey] = frozenset()
    approved_by: str | None = None
    approved_at: datetime | None = None
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

        if self.approved_groups.intersection(self.rejected_groups):
            raise ValueError("a correction group cannot be both approved and rejected")
        known_required = (
            self.summary.required_group_keys if self.summary is not None else frozenset()
        )
        decided = self.approved_groups.union(self.rejected_groups)
        unknown = decided.difference(known_required)
        if unknown:
            raise ValueError("only approval-required correction groups may be decided")

        if self.status == DryRunStatus.RUNNING and self.summary is not None:
            raise ValueError("a running dry run cannot already have a summary")
        if self.status != DryRunStatus.RUNNING and self.summary is None:
            raise ValueError("a completed dry run requires a summary")
        if self.status in {DryRunStatus.APPROVED, DryRunStatus.FROZEN}:
            _require_text(self.approved_by, "approved_by")
            if self.approved_at is None or self.approved_at.utcoffset() is None:
                raise ValueError("approved_at must be timezone-aware")
        if self.status == DryRunStatus.FROZEN and self.canonical_dataset_hash is None:
            raise ValueError("a frozen dry run requires canonical_dataset_hash")
        if self.status != DryRunStatus.FROZEN and self.canonical_dataset_hash is not None:
            raise ValueError(
                "canonical_dataset_hash is retained only after the dry run is frozen"
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
        status = DryRunStatus.BLOCKED if summary.blocked else DryRunStatus.REVIEW_REQUIRED
        return replace(self, status=status, summary=summary)

    def approve_group(self, key: CorrectionGroupKey) -> "DryRun":
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
        return replace(
            self,
            approved_groups=self.approved_groups.union({key}),
            rejected_groups=self.rejected_groups.difference({key}),
        )

    def reject_group(self, key: CorrectionGroupKey) -> "DryRun":
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
        return replace(
            self,
            status=DryRunStatus.BLOCKED,
            approved_groups=self.approved_groups.difference({key}),
            rejected_groups=self.rejected_groups.union({key}),
        )

    def approve(self, *, approved_by: str, approved_at: datetime) -> "DryRun":
        """Approve the complete reviewed dry run.

        Whole-run approval is required even when every correction is
        automatic. For required corrections, every group must first be present
        in :attr:`approved_groups`.

        Args:
            approved_by: Non-blank identity of the data manager.
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
            approved_by=_require_text(approved_by, "approved_by"),
            approved_at=approved_at,
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
                f"dry run must be {expected.value}; current status is {self.status.value}"
            )


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
