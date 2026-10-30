"""Keep ancestor, descendant, and sibling roles accurate in cyclic cause graphs."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.preparation.quality import default_quality_ruleset, evaluate_quality
from impodo.domain.preparation.staging_contracts import (
    CanonicalIssue,
    StagingDisposition,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import LogicalReference
from tests.domain.preparation import test_quality as fixtures


class HierarchyCauseImpactTests(TestCase):
    def test_deep_bad_child_preserves_two_ancestors_and_distinct_siblings(self):
        def reference(key):
            return LogicalReference(origin="incoming", key=(key,), dataset="contacts")

        rows = tuple(
            fixtures._canonical_row(
                str(index),
                index + 1,
                source_identity=(key,),
                target_identity=(key,),
                target_scope=(reference(parent),) if parent else (None,),
            )
            for index, (key, parent) in enumerate(
                (("ROOT", ""), ("A", "ROOT"), ("AA", "A"), ("B", "ROOT"), ("BB", "B")),
                1,
            )
        )
        rows = (
            rows[0],
            rows[1],
            replace(
                rows[2],
                disposition=StagingDisposition.BLOCKED,
                issues=(
                    CanonicalIssue(
                        "SOURCE_TYPE_INVALID",
                        "Fictional invalid value",
                        "error",
                        "contacts",
                        4,
                        "amount",
                    ),
                ),
            ),
            rows[3],
            rows[4],
            replace(
                fixtures._canonical_row(
                    "6", 7, source_identity=("CONSUMER",), target_identity=("CONSUMER",)
                ),
                references={"parent_id": reference("AA")},
            ),
        )
        self._assert_impact(rows, (1, 2, 1, 2))

    def test_component_failure_reaches_the_dependent_line_parent_and_sibling(self):
        def row(token, key, parent=None):
            return fixtures._canonical_row(
                str(token),
                token + 1,
                source_identity=(key,),
                target_identity=(key,),
                target_scope=(
                    LogicalReference(
                        origin="incoming", key=(parent,), dataset="contacts"
                    ),
                )
                if parent
                else (),
            )

        rows = (
            replace(
                row(1, "COMPONENT"),
                disposition=StagingDisposition.BLOCKED,
                issues=(
                    CanonicalIssue(
                        "SOURCE_TYPE_INVALID",
                        "Fictional invalid value",
                        "error",
                        "contacts",
                        2,
                        "amount",
                    ),
                ),
            ),
            row(2, "HEADER"),
            replace(
                row(3, "LINE1", "HEADER"),
                references={
                    "product_id": LogicalReference(
                        origin="incoming", key=("COMPONENT",), dataset="contacts"
                    )
                },
            ),
            row(4, "LINE2", "HEADER"),
        )
        self._assert_impact(rows, (1, 1, 1, 1))

    def _assert_impact(self, rows, expected):
        fixture = fixtures.QualityStoreTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        staging = fixtures._staging(fixture.workspace_state.workspace_id, rows)
        published = fixture.staging.publish_canonical_staging(
            fixture.workspace_state.workspace_id,
            staging,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset = default_quality_ruleset(
            workspace_id=fixture.workspace_state.workspace_id,
            mapping_hash=fixtures.MAPPING_HASH,
            schema_hash=fixtures.SCHEMA_HASH,
            datasets=("contacts",),
        )
        fixture.quality.publish_quality_ruleset(
            fixture.workspace_state.workspace_id, ruleset, actor=LOCAL_ACTOR
        )
        run = evaluate_quality(
            workspace_state=fixture.workspace_state,
            staging=staging,
            physical_rows={"dataset:contacts": tuple(row.source_row for row in rows)},
            ruleset=ruleset,
        )
        result = fixture.quality.publish_quality_run(
            fixture.workspace_state.workspace_id,
            run,
            staging_run_id=published.run_id,
            actor=LOCAL_ACTOR,
        )
        page = fixture.quality.get_quality_review_page(
            fixture.workspace_state.workspace_id,
            result.run_id,
            include_run_cause_counts=True,
        )
        cause = next(
            cause
            for cause in page.run_cause_counts
            if cause.reason_code == "SOURCE_TYPE_INVALID"
        )
        self.assertEqual(
            (
                cause.affected_record_count,
                cause.affected_parent_count,
                cause.affected_child_count,
                cause.affected_sibling_count,
            ),
            expected,
        )
        detail = fixture.quality.get_quality_cause_review_page(
            fixture.workspace_state.workspace_id, result.run_id, cause.cause_key
        )
        self.assertEqual(detail.matching_count, len(rows))
