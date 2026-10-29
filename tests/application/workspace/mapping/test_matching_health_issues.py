from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import uuid4

from impodo.application.workspace.mapping.health_issues import (
    matching_health_issue_counts,
)
from impodo.domain.mapping.contracts import ResolverOrigin
from impodo.domain.matching_order import (
    MatchingIdentityCheckStatus,
    MatchingIdentityNotCheckableReason,
    MatchingIdentityResult,
    MatchingOrderCheck,
)
from impodo.domain.relationship_health import (
    RelationshipHealthNotCheckableReason,
    RelationshipHealthResult,
    RelationshipHealthStatus,
)


HASH = "sha256:" + "1" * 64


class MatchingHealthIssueCountTests(unittest.TestCase):
    def test_counts_blockers_and_unchecked_reviews_without_pinned_sources(
        self,
    ) -> None:
        check = _check(
            identity_results=(
                _blocked_identity(),
                MatchingIdentityResult(
                    dataset_id="indirect",
                    target_model="x.child",
                    target_fields=(),
                    status=MatchingIdentityCheckStatus.NOT_CHECKABLE,
                    not_checkable_reason=(
                        MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
                    ),
                ),
                MatchingIdentityResult(
                    dataset_id="odoo",
                    target_model="x.existing",
                    target_fields=(),
                    status=MatchingIdentityCheckStatus.NOT_CHECKABLE,
                    not_checkable_reason=(
                        MatchingIdentityNotCheckableReason.ODOO_PINNED_SELECTION
                    ),
                ),
            ),
            relationship_health_results=(_unchecked_relationship(),),
        )

        counts = matching_health_issue_counts(check)

        self.assertEqual(counts.must_fix, 1)
        self.assertEqual(counts.review, 2)


def _blocked_identity() -> MatchingIdentityResult:
    return MatchingIdentityResult(
        dataset_id="products",
        target_model="product.template",
        target_fields=("default_code",),
        status=MatchingIdentityCheckStatus.CHECKED,
        source_row_count=3,
        source_unique_row_count=2,
        source_blank_row_count=1,
        target_unique_key_count=2,
        expected_new_count=2,
        blocked_count=1,
    )


def _unchecked_relationship() -> RelationshipHealthResult:
    return RelationshipHealthResult(
        owner_dataset_id="lines",
        target_field="line_ids",
        relationship_kind="one2many",
        resolver_origin=ResolverOrigin.TARGET_CATALOG,
        related_model="x.line",
        dependency_dataset_id=None,
        required=False,
        status=RelationshipHealthStatus.NOT_CHECKABLE,
        not_checkable_reason=(
            RelationshipHealthNotCheckableReason.ONE2MANY_INVERSE_REQUIRED
        ),
    )


def _check(
    *,
    identity_results: tuple[MatchingIdentityResult, ...] = (),
    relationship_health_results: tuple[RelationshipHealthResult, ...] = (),
) -> MatchingOrderCheck:
    return MatchingOrderCheck(
        check_id=str(uuid4()),
        workspace_id="workspace:test",
        source_selection_hash=HASH,
        schema_hash=HASH,
        governance_hash=HASH,
        working_draft_version=1,
        working_draft_hash=HASH,
        target_hash=HASH,
        read_credential_binding_hash=HASH,
        read_principal_hash=HASH,
        read_permission_hash=HASH,
        read_context_hash=HASH,
        relationship_results=(),
        unchecked_relationship_count=0,
        ordered_dataset_ids=(),
        recommendation_hash=HASH,
        schema_changed=False,
        captured_at=datetime.now(UTC),
        actor_issuer="local",
        actor_subject="operator",
        actor_display_name="Operator",
        identity_results=identity_results,
        relationship_health_results=relationship_health_results,
    )


if __name__ == "__main__":
    unittest.main()
