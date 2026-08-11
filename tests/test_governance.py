"""Executable documentation for normalization dry-run governance.

The tests are organized in the same two layers as the production contracts:

* :class:`CorrectionSummaryTests` checks aggregation and fail-closed counts.
* :class:`DryRunLifecycleTests` checks manager decisions and state transitions.

The values are deliberately small and deterministic. They illustrate the
business rules without requiring DuckDB, source files, a browser, or Odoo.
"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from impodo.access import (
    Actor,
    ActorIdentity,
    Capability,
    LOCAL_ACTOR,
)
from impodo.governance import (
    ApprovalMode,
    CorrectionGroupKey,
    CorrectionImpact,
    DryRun,
    DryRunStatus,
    DryRunSummary,
    DryRunTransitionError,
)


# Fixed hashes make lifecycle examples readable while satisfying the production
# evidence contract. They are syntactically valid test values, not real hashes.
SOURCE_HASH = "sha256:" + "a" * 64
RULESET_HASH = "sha256:" + "b" * 64
CANONICAL_HASH = "sha256:" + "c" * 64

# A fixed timezone-aware timestamp keeps tests deterministic and demonstrates
# that approvals cannot use an ambiguous local time.
APPROVED_AT = datetime(2026, 7, 29, 10, 30, tzinfo=timezone.utc)


def make_run() -> DryRun:
    """Create the common initial ``RUNNING`` state used by lifecycle tests."""

    return DryRun(
        run_id="dry-run-001",
        source_hashes={"products.xlsx": SOURCE_HASH},
        ruleset_hash=RULESET_HASH,
    )


class CorrectionSummaryTests(unittest.TestCase):
    """Verify grouped correction counts before lifecycle decisions begin."""

    def test_summary_separates_automatic_and_approval_required_counts(self) -> None:
        """Expose separate dashboard totals and the exact pending group key."""

        automatic = CorrectionImpact(
            key=CorrectionGroupKey("trim-product-code", "products", "default_code"),
            approval_mode=ApprovalMode.AUTOMATIC,
            affected_count=241,
        )
        required = CorrectionImpact(
            key=CorrectionGroupKey("remap-product-code", "products", "default_code"),
            approval_mode=ApprovalMode.REQUIRED,
            affected_count=18,
        )

        summary = DryRunSummary(corrections=(automatic, required))

        self.assertEqual(summary.total_correction_count, 259)
        self.assertEqual(summary.automatic_correction_count, 241)
        self.assertEqual(summary.approval_required_correction_count, 18)
        self.assertEqual(summary.required_group_keys, frozenset({required.key}))
        self.assertFalse(summary.blocked)

    def test_correction_collisions_block_the_summary(self) -> None:
        """Block even an automatic rule when normalization creates a collision."""

        correction = CorrectionImpact(
            key=CorrectionGroupKey("trim-product-code", "products", "default_code"),
            approval_mode=ApprovalMode.AUTOMATIC,
            affected_count=241,
            collision_count=2,
        )

        summary = DryRunSummary(corrections=(correction,))

        self.assertTrue(summary.blocked)

    def test_duplicate_correction_groups_are_rejected(self) -> None:
        """Prevent two summary rows from claiming the same business decision."""

        key = CorrectionGroupKey("trim-product-code", "products", "default_code")
        corrections = (
            CorrectionImpact(key, ApprovalMode.AUTOMATIC, 2),
            CorrectionImpact(key, ApprovalMode.AUTOMATIC, 3),
        )

        with self.assertRaisesRegex(ValueError, "duplicate correction groups"):
            DryRunSummary(corrections=corrections)

    def test_invalid_impact_counts_are_rejected(self) -> None:
        """Reject totals that cannot reconcile with row-level evidence."""

        key = CorrectionGroupKey("trim-product-code", "products", "default_code")
        with self.assertRaisesRegex(ValueError, "at least 1"):
            CorrectionImpact(key, ApprovalMode.AUTOMATIC, 0)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            CorrectionImpact(key, ApprovalMode.AUTOMATIC, 1, collision_count=2)


class DryRunLifecycleTests(unittest.TestCase):
    """Verify allowed and forbidden data-manager workflow transitions."""

    def test_automatic_corrections_still_require_whole_run_approval(self) -> None:
        """Require final review even when no group needs an individual decision."""

        correction = CorrectionImpact(
            CorrectionGroupKey("trim-product-code", "products", "default_code"),
            ApprovalMode.AUTOMATIC,
            241,
        )

        completed = make_run().complete(DryRunSummary(corrections=(correction,)))
        approved = completed.approve(
            actor=LOCAL_ACTOR,
            approved_at=APPROVED_AT,
        )
        frozen = approved.freeze(canonical_dataset_hash=CANONICAL_HASH)

        self.assertEqual(completed.status, DryRunStatus.REVIEW_REQUIRED)
        self.assertEqual(approved.status, DryRunStatus.APPROVED)
        self.assertEqual(frozen.status, DryRunStatus.FROZEN)
        self.assertEqual(frozen.canonical_dataset_hash, CANONICAL_HASH)

    def test_required_correction_group_must_be_approved_first(self) -> None:
        """Refuse whole-run approval while a required group remains pending."""

        correction = CorrectionImpact(
            CorrectionGroupKey("remap-product-code", "products", "default_code"),
            ApprovalMode.REQUIRED,
            18,
        )
        completed = make_run().complete(DryRunSummary(corrections=(correction,)))

        with self.assertRaisesRegex(
            DryRunTransitionError,
            "still require approval",
        ):
            completed.approve(
                actor=LOCAL_ACTOR,
                approved_at=APPROVED_AT,
            )

        approved_group = completed.approve_group(
            correction.key,
            actor=LOCAL_ACTOR,
            decided_at=APPROVED_AT,
        )
        with self.assertRaisesRegex(DryRunTransitionError, "already decided"):
            approved_group.approve_group(
                correction.key,
                actor=LOCAL_ACTOR,
                decided_at=APPROVED_AT,
            )
        approved_run = approved_group.approve(
            actor=LOCAL_ACTOR,
            approved_at=APPROVED_AT,
        )

        self.assertEqual(approved_run.status, DryRunStatus.APPROVED)
        self.assertIn(correction.key, approved_run.approved_groups)

    def test_required_groups_can_be_approved_together(self) -> None:
        """Record every required group before one whole-run approval."""

        corrections = (
            CorrectionImpact(
                CorrectionGroupKey("case-name", "contacts", "name"),
                ApprovalMode.REQUIRED,
                18,
            ),
            CorrectionImpact(
                CorrectionGroupKey("match-language", "contacts", "lang"),
                ApprovalMode.REQUIRED,
                12,
            ),
        )
        completed = make_run().complete(DryRunSummary(corrections=corrections))

        reviewed = completed.approve_all_required_groups(
            actor=LOCAL_ACTOR,
            decided_at=APPROVED_AT,
        )
        approved = reviewed.approve(
            actor=LOCAL_ACTOR,
            approved_at=APPROVED_AT,
        )

        self.assertEqual(reviewed.approved_groups, {item.key for item in corrections})
        self.assertEqual(approved.status, DryRunStatus.APPROVED)

    def test_automatic_group_cannot_receive_a_group_approval(self) -> None:
        """Keep rule-policy authorization separate from manager decisions."""

        correction = CorrectionImpact(
            CorrectionGroupKey("trim-product-code", "products", "default_code"),
            ApprovalMode.AUTOMATIC,
            241,
        )
        completed = make_run().complete(DryRunSummary(corrections=(correction,)))

        with self.assertRaisesRegex(
            DryRunTransitionError,
            "only approval-required",
        ):
            completed.approve_group(
                correction.key,
                actor=LOCAL_ACTOR,
                decided_at=APPROVED_AT,
            )

    def test_rejecting_required_correction_blocks_the_run(self) -> None:
        """Fail closed after rejection instead of silently retaining raw values."""

        correction = CorrectionImpact(
            CorrectionGroupKey("remap-product-code", "products", "default_code"),
            ApprovalMode.REQUIRED,
            18,
        )
        completed = make_run().complete(DryRunSummary(corrections=(correction,)))

        blocked = completed.reject_group(
            correction.key,
            actor=LOCAL_ACTOR,
            decided_at=APPROVED_AT,
        )

        self.assertEqual(blocked.status, DryRunStatus.BLOCKED)
        self.assertIn(correction.key, blocked.rejected_groups)
        with self.assertRaises(DryRunTransitionError):
            blocked.approve(
                actor=LOCAL_ACTOR,
                approved_at=APPROVED_AT,
            )

        reopened = blocked.reopen_review()

        self.assertEqual(reopened.status, DryRunStatus.REVIEW_REQUIRED)
        self.assertFalse(reopened.rejected_groups)
        self.assertEqual(reopened.group_decisions, ())

    def test_collision_finishes_as_blocked(self) -> None:
        """Route an unsafe execution summary directly from running to blocked."""

        correction = CorrectionImpact(
            CorrectionGroupKey("trim-product-code", "products", "default_code"),
            ApprovalMode.AUTOMATIC,
            241,
            collision_count=2,
        )

        blocked = make_run().complete(DryRunSummary(corrections=(correction,)))

        self.assertEqual(blocked.status, DryRunStatus.BLOCKED)

    def test_run_cannot_be_frozen_before_approval(self) -> None:
        """Prevent creation of canonical evidence before manager approval."""

        completed = make_run().complete(DryRunSummary())

        with self.assertRaisesRegex(DryRunTransitionError, "must be APPROVED"):
            completed.freeze(canonical_dataset_hash=CANONICAL_HASH)

    def test_approval_time_must_be_timezone_aware(self) -> None:
        """Require audit timestamps that identify one unambiguous instant."""

        completed = make_run().complete(DryRunSummary())

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            completed.approve(
                actor=LOCAL_ACTOR,
                approved_at=datetime(2026, 7, 29, 10, 30),
            )

    def test_hash_contract_rejects_unbound_runs(self) -> None:
        """Reject runs that are not bound to a canonical ruleset hash."""

        with self.assertRaisesRegex(ValueError, "ruleset_hash"):
            DryRun(
                run_id="dry-run-001",
                source_hashes={"products.xlsx": SOURCE_HASH},
                ruleset_hash="not-a-hash",
            )

    def test_group_reviewer_and_run_approver_are_distinct_capabilities(self) -> None:
        correction = CorrectionImpact(
            CorrectionGroupKey("remap-product-code", "products", "default_code"),
            ApprovalMode.REQUIRED,
            18,
        )
        completed = make_run().complete(DryRunSummary(corrections=(correction,)))
        reviewer = Actor(
            identity=ActorIdentity("test", "reviewer", "Reviewer"),
            capabilities=frozenset({Capability.NORMALIZATION_DECIDE}),
        )
        approver = Actor(
            identity=ActorIdentity("test", "approver", "Approver"),
            capabilities=frozenset({Capability.NORMALIZATION_APPROVE}),
        )

        with self.assertRaises(PermissionError):
            completed.approve_group(
                correction.key,
                actor=approver,
                decided_at=APPROVED_AT,
            )
        reviewed = completed.approve_group(
            correction.key,
            actor=reviewer,
            decided_at=APPROVED_AT,
        )
        with self.assertRaises(PermissionError):
            reviewed.approve(
                actor=reviewer,
                approved_at=APPROVED_AT,
            )
        approved = reviewed.approve(
            actor=approver,
            approved_at=APPROVED_AT,
        )

        self.assertEqual(
            approved.group_decisions[0].evidence.approved_by,
            reviewer.identity,
        )
        self.assertEqual(approved.approval.approved_by, approver.identity)


if __name__ == "__main__":
    unittest.main()
