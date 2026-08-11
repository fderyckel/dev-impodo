from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.access import LOCAL_ACTOR
from impodo.models import Issue, LogicalReference, PreparedRecord
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.quality_repository import QualityRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.application.bounded_quality import build_bounded_quality_run
from impodo.domain.compiler import compile_profile_document
from impodo.planner import plan_record_requests
from impodo.domain.preflight.frozen_input import canonical_rows_to_prepared_bundle
from impodo.profile import load_profile
from impodo.projects import MigrationProject, OdooConnectionMode, ProjectStatus
from impodo.quality import (
    QualityDisposition,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRuleFamily,
    QualityRunStatus,
    QualityRun,
    SourceAccountingState,
    default_quality_ruleset,
    evaluate_quality,
    manager_quality_rule,
)
from impodo.source import PreparedBundle, prepare_sources
from impodo.staging_contracts import (
    CanonicalIssue,
    CanonicalLineage,
    CanonicalRow,
    CanonicalStagingRun,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingDisposition,
    StagingReconciliation,
)
from impodo.domain.staging.preparation_session import StoredCanonicalStagingRun
from impodo.workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


ROOT = Path(__file__).resolve().parents[1]
PHYSICAL_HASH = "sha256:" + "1" * 64
MAPPING_HASH = "sha256:" + "2" * 64
SCHEMA_HASH = "sha256:" + "3" * 64
SOURCE_HASH = "sha256:" + "4" * 64


class QualityEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = _project()

    def test_collision_quarantines_complete_group_and_reconciles_sources(self) -> None:
        rows = (
            _canonical_row("5", 2, source_identity=("A",), target_identity=("SAME",)),
            _canonical_row("6", 3, source_identity=("B",), target_identity=("SAME",)),
            _canonical_row("7", 4, source_identity=("C",), target_identity=("SAFE",)),
        )
        staging = _staging(self.project.project_id, rows)
        prepared = _prepared(rows)
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )

        first = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:contacts": (2, 3, 4)},
            ruleset=ruleset,
        )
        repeated = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:contacts": (2, 3, 4)},
            ruleset=ruleset,
        )

        self.assertEqual(first.to_json(), repeated.to_json())
        self.assertEqual(first.quarantined_count, 2)
        self.assertEqual(first.ready_count, 1)
        self.assertEqual(
            {item.state for item in first.source_accounting},
            {SourceAccountingState.REPRESENTED},
        )
        self.assertEqual(len(first.quarantine), 2)
        eligible = canonical_rows_to_prepared_bundle(
            staging, first, source_hashes=prepared.source_hashes
        )
        self.assertEqual([item.source_row for item in eligible.records], [4])

    def test_existing_row_error_becomes_plain_quarantine_evidence(self) -> None:
        issue = CanonicalIssue(
            code="SOURCE_REQUIRED_VALUE_MISSING",
            message="required value is empty",
            severity="error",
            dataset="contacts",
            source_row=2,
            field="name",
        )
        row = replace(
            _canonical_row("5", 2),
            disposition=StagingDisposition.BLOCKED,
            issues=(issue,),
        )
        staging = _staging(self.project.project_id, (row,))
        prepared = _prepared((row,))
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )

        run = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )

        self.assertEqual(
            run.row_results[0].effective_disposition,
            QualityDisposition.QUARANTINED,
        )
        self.assertEqual(run.issues[0].owner_label, "Data Manager")
        self.assertIn("Correct the source", run.quarantine[0].correction_route)

    def test_relationship_to_quarantined_incoming_record_is_propagated(self) -> None:
        parent_issue = CanonicalIssue(
            code="SOURCE_TYPE_INVALID",
            message="invalid value",
            severity="error",
            dataset="categories",
            source_row=2,
        )
        parent = replace(
            _canonical_row(
                "5",
                2,
                dataset="categories",
                source_identity=("CAT",),
                target_identity=("CAT",),
                physical_dataset_id="dataset:categories",
            ),
            disposition=StagingDisposition.BLOCKED,
            issues=(parent_issue,),
        )
        child = replace(
            _canonical_row(
                "6",
                2,
                dataset="products",
                source_identity=("P1",),
                target_identity=("P1",),
                physical_dataset_id="dataset:products",
            ),
            references={
                "categ_id": LogicalReference(
                    origin="incoming",
                    key=("CAT",),
                    dataset="categories",
                    target_fields=("name",),
                )
            },
        )
        staging = _staging(self.project.project_id, (parent, child))
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories", "products"),
        )

        run = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={
                "dataset:categories": (2,),
                "dataset:products": (2,),
            },
            ruleset=ruleset,
        )

        by_dataset = {item.dataset: item for item in run.row_results}
        self.assertEqual(
            by_dataset["products"].effective_disposition,
            QualityDisposition.QUARANTINED,
        )
        self.assertIn(
            "INCOMING_RELATIONSHIP_NOT_READY",
            {item.reason_code for item in run.issues},
        )

    def test_relationship_readiness_propagates_through_a_long_chain(self) -> None:
        row_count = 64
        rows = tuple(
            replace(
                _canonical_row(
                    "5",
                    index + 2,
                    dataset="categories",
                    source_identity=(f"CAT-{index:03d}",),
                    target_identity=(f"CAT-{index:03d}",),
                    physical_dataset_id="dataset:categories",
                ),
                row_id=(
                    "sha256:"
                    + sha256(f"category:{index}".encode("utf-8")).hexdigest()
                ),
            )
            for index in range(row_count)
        )
        root_issue = CanonicalIssue(
            code="SOURCE_TYPE_INVALID",
            message="invalid value",
            severity="error",
            dataset="categories",
            source_row=2,
        )
        rows = (
            replace(
                rows[0],
                disposition=StagingDisposition.BLOCKED,
                issues=(root_issue,),
            ),
            *rows[1:],
        )
        rows = tuple(
            replace(
                row,
                references=(
                    {}
                    if index == 0
                    else {
                        "parent_id": LogicalReference(
                            origin="incoming",
                            key=(f"CAT-{index - 1:03d}",),
                            dataset="categories",
                            target_fields=("name",),
                        )
                    }
                ),
            )
            for index, row in enumerate(rows)
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories",),
        )

        run = evaluate_quality(
            project=self.project,
            staging=_staging(self.project.project_id, rows),
            physical_rows={
                "dataset:categories": tuple(range(2, row_count + 2)),
            },
            ruleset=ruleset,
        )

        self.assertEqual(run.quarantined_count, row_count)
        self.assertEqual(
            sum(
                issue.reason_code == "INCOMING_RELATIONSHIP_NOT_READY"
                for issue in run.issues
            ),
            row_count - 1,
        )

    def test_relationship_cycle_terminates_and_keeps_complete_evidence(self) -> None:
        first = _canonical_row(
            "5",
            2,
            dataset="categories",
            source_identity=("A",),
            target_identity=("A",),
            physical_dataset_id="dataset:categories",
        )
        second = _canonical_row(
            "6",
            3,
            dataset="categories",
            source_identity=("B",),
            target_identity=("B",),
            physical_dataset_id="dataset:categories",
        )
        root_issue = CanonicalIssue(
            code="SOURCE_TYPE_INVALID",
            message="invalid value",
            severity="error",
            dataset="categories",
            source_row=2,
        )
        first = replace(
            first,
            disposition=StagingDisposition.BLOCKED,
            issues=(root_issue,),
            references={
                "parent_id": LogicalReference(
                    origin="incoming",
                    key=("B",),
                    dataset="categories",
                    target_fields=("name",),
                )
            },
        )
        second = replace(
            second,
            references={
                "parent_id": LogicalReference(
                    origin="incoming",
                    key=("A",),
                    dataset="categories",
                    target_fields=("name",),
                )
            },
        )
        rows = (first, second)
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories",),
        )

        run = evaluate_quality(
            project=self.project,
            staging=_staging(self.project.project_id, rows),
            physical_rows={"dataset:categories": (2, 3)},
            ruleset=ruleset,
        )

        self.assertEqual(run.quarantined_count, 2)
        self.assertEqual(
            sum(
                issue.reason_code == "INCOMING_RELATIONSHIP_NOT_READY"
                for issue in run.issues
            ),
            2,
        )

    def test_bounded_collision_evidence_matches_complete_evaluator(self) -> None:
        rows = (
            _canonical_row(
                "5",
                2,
                source_identity=("A",),
                target_identity=("SAME",),
            ),
            _canonical_row(
                "6",
                3,
                source_identity=("B",),
                target_identity=("SAME",),
            ),
            _canonical_row(
                "7",
                4,
                source_identity=("C",),
                target_identity=("SAFE",),
            ),
        )
        staging = _staging(self.project.project_id, rows)
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        expected = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:contacts": (2, 3, 4)},
            ruleset=ruleset,
        )
        bounded = build_bounded_quality_run(
            project=self.project,
            staging=_stored_staging(staging),
            physical_rows={"dataset:contacts": (2, 3, 4)},
            ruleset=ruleset,
            published_staging_content_hash=staging.content_hash,
        )

        self.assertEqual(
            _materialized_bounded_quality(bounded).to_json(),
            expected.to_json(),
        )

    def test_bounded_relationship_propagation_matches_complete_evaluator(
        self,
    ) -> None:
        first = replace(
            _canonical_row(
                "5",
                2,
                dataset="categories",
                source_identity=("A",),
                target_identity=("A",),
                physical_dataset_id="dataset:categories",
            ),
            disposition=StagingDisposition.BLOCKED,
            issues=(
                CanonicalIssue(
                    code="SOURCE_TYPE_INVALID",
                    message="invalid value",
                    severity="error",
                    dataset="categories",
                    source_row=2,
                ),
            ),
            references={
                "parent_id": LogicalReference(
                    origin="incoming",
                    key=("B",),
                    dataset="categories",
                    target_fields=("name",),
                )
            },
        )
        second = replace(
            _canonical_row(
                "6",
                3,
                dataset="categories",
                source_identity=("B",),
                target_identity=("B",),
                physical_dataset_id="dataset:categories",
            ),
            references={
                "parent_id": LogicalReference(
                    origin="incoming",
                    key=("A",),
                    dataset="categories",
                    target_fields=("name",),
                )
            },
        )
        rows = (first, second)
        staging = _staging(self.project.project_id, rows)
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories",),
        )
        expected = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:categories": (2, 3)},
            ruleset=ruleset,
        )
        bounded = build_bounded_quality_run(
            project=self.project,
            staging=_stored_staging(staging),
            physical_rows={"dataset:categories": (2, 3)},
            ruleset=ruleset,
            published_staging_content_hash=staging.content_hash,
        )

        self.assertEqual(
            _materialized_bounded_quality(bounded).to_json(),
            expected.to_json(),
        )

    def test_relationship_fan_out_and_duplicate_reference_are_processed_once(
        self,
    ) -> None:
        rows = tuple(
            _canonical_row(
                token,
                index + 2,
                dataset="categories",
                source_identity=(identity,),
                target_identity=(identity,),
                physical_dataset_id="dataset:categories",
            )
            for index, (token, identity) in enumerate(
                (("5", "ROOT"), ("6", "LEFT"), ("7", "RIGHT"))
            )
        )
        root_issue = CanonicalIssue(
            code="SOURCE_TYPE_INVALID",
            message="invalid value",
            severity="error",
            dataset="categories",
            source_row=2,
        )
        rows = (
            replace(
                rows[0],
                disposition=StagingDisposition.BLOCKED,
                issues=(root_issue,),
            ),
            *rows[1:],
        )
        root_reference = LogicalReference(
            origin="incoming",
            key=("ROOT",),
            dataset="categories",
            target_fields=("name",),
        )
        rows = (
            rows[0],
            replace(
                rows[1],
                references={"parents": (root_reference, root_reference)},
            ),
            replace(rows[2], references={"parent_id": root_reference}),
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories",),
        )

        run = evaluate_quality(
            project=self.project,
            staging=_staging(self.project.project_id, rows),
            physical_rows={"dataset:categories": (2, 3, 4)},
            ruleset=ruleset,
        )

        self.assertEqual(run.quarantined_count, 3)
        relationship_issues = tuple(
            issue
            for issue in run.issues
            if issue.reason_code == "INCOMING_RELATIONSHIP_NOT_READY"
        )
        self.assertEqual(len(relationship_issues), 2)
        self.assertEqual(
            {issue.source_row for issue in relationship_issues},
            {3, 4},
        )

    def test_relationship_warning_does_not_propagate_as_unsafe(self) -> None:
        rows = tuple(
            _canonical_row(
                token,
                index + 2,
                dataset="categories",
                source_identity=(identity,),
                target_identity=(identity,),
                physical_dataset_id="dataset:categories",
            )
            for index, (token, identity) in enumerate(
                (("5", "ROOT"), ("6", "CHILD"), ("7", "GRANDCHILD"))
            )
        )
        root_issue = CanonicalIssue(
            code="SOURCE_TYPE_INVALID",
            message="invalid value",
            severity="error",
            dataset="categories",
            source_row=2,
        )
        rows = (
            replace(
                rows[0],
                disposition=StagingDisposition.BLOCKED,
                issues=(root_issue,),
            ),
            *rows[1:],
        )
        rows = tuple(
            replace(
                row,
                references=(
                    {}
                    if index == 0
                    else {
                        "parent_id": LogicalReference(
                            origin="incoming",
                            key=(rows[index - 1].source_identity[0],),
                            dataset="categories",
                            target_fields=("name",),
                        )
                    }
                ),
            )
            for index, row in enumerate(rows)
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories",),
        )
        ruleset = replace(
            ruleset,
            rules=tuple(
                replace(rule, outcome=QualityOutcomePolicy.WARNING)
                if rule.family is QualityRuleFamily.RELATIONSHIP_READINESS
                else rule
                for rule in ruleset.rules
            ),
        )

        run = evaluate_quality(
            project=self.project,
            staging=_staging(self.project.project_id, rows),
            physical_rows={"dataset:categories": (2, 3, 4)},
            ruleset=ruleset,
        )

        by_source_row = {item.source_row: item for item in run.row_results}
        self.assertEqual(
            by_source_row[3].effective_disposition,
            QualityDisposition.CANDIDATE,
        )
        self.assertTrue(by_source_row[3].requires_review)
        self.assertEqual(by_source_row[4].issue_ids, ())
        self.assertFalse(by_source_row[4].requires_review)

    def test_guided_warning_remains_review_without_removing_record(self) -> None:
        row = replace(
            _canonical_row("5", 2),
            proposed_values={"start": 10, "end": 5},
        )
        staging = _staging(self.project.project_id, (row,))
        business_rule = manager_quality_rule(
            project_id=self.project.project_id,
            dataset="contacts",
            family=QualityRuleFamily.ORDERED_COMPARISON,
            name="Start before end",
            input_fields=("start", "end"),
            outcome=QualityOutcomePolicy.WARNING,
            owner_role=QualityOwnerRole.FUNCTIONAL_OWNER,
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
            manager_rules=(business_rule,),
        )

        run = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )

        self.assertEqual(run.review_count, 1)
        self.assertEqual(run.ready_count, 0)
        self.assertEqual(
            run.row_results[0].effective_disposition,
            QualityDisposition.CANDIDATE,
        )
        self.assertEqual(run.issues[0].owner_label, "Functional Owner")
        self.assertEqual(
            len(
                canonical_rows_to_prepared_bundle(
                    staging,
                    run,
                    source_hashes=_prepared((row,)).source_hashes,
                ).records
            ),
            1,
        )

    def test_set_aside_row_never_enters_odoo_record_request_plan(self) -> None:
        profile = compile_profile_document(
            load_profile(ROOT / "profiles/examples/golden_slice.yaml")
        )
        prepared_all = prepare_sources(profile, ROOT / "examples/golden")
        partner_records = tuple(
            item for item in prepared_all.records if item.dataset == "products"
        )[:2]
        self.assertEqual(len(partner_records), 2)
        required_issue = CanonicalIssue(
            code="SOURCE_REQUIRED_VALUE_MISSING",
            message="required name is empty",
            severity="error",
            dataset="products",
            source_row=partner_records[0].source_row,
            field="name",
        )
        rows = tuple(
            replace(
                _canonical_row(
                    str(index + 5),
                    record.source_row,
                    dataset="products",
                    source_identity=record.source_identity,
                    target_identity=record.target_identity,
                    target_scope=record.target_scope,
                    physical_dataset_id="dataset:products",
                ),
                target_model=record.target_model,
                disposition=(
                    StagingDisposition.BLOCKED
                    if index == 0
                    else StagingDisposition.CANDIDATE
                ),
                issues=(required_issue,) if index == 0 else (),
            )
            for index, record in enumerate(partner_records)
        )
        staging = _staging(self.project.project_id, rows)
        prepared = PreparedBundle(
            records=partner_records,
            issues=(),
            source_hashes={"products": SOURCE_HASH},
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("products",),
        )
        run = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={
                "dataset:products": tuple(
                    item.source_row for item in partner_records
                )
            },
            ruleset=ruleset,
        )

        eligible = canonical_rows_to_prepared_bundle(
            staging, run, source_hashes=prepared.source_hashes
        )
        requests = plan_record_requests(profile, eligible.records)

        self.assertNotIn(str(partner_records[0].target_identity[0]), repr(requests))
        self.assertIn(str(partner_records[1].target_identity[0]), repr(requests))

    def test_mixed_fan_out_filters_the_exact_set_aside_record(self) -> None:
        required_issue = CanonicalIssue(
            code="SOURCE_REQUIRED_VALUE_MISSING",
            message="required name is empty",
            severity="error",
            dataset="contacts",
            source_row=2,
            field="name",
        )
        set_aside = replace(
            _canonical_row(
                "5",
                2,
                source_identity=("SET-ASIDE",),
                target_identity=("SET-ASIDE",),
            ),
            disposition=StagingDisposition.BLOCKED,
            issues=(required_issue,),
        )
        eligible_row = _canonical_row(
            "6",
            2,
            source_identity=("ELIGIBLE",),
            target_identity=("ELIGIBLE",),
        )
        staging = _staging(self.project.project_id, (set_aside, eligible_row))
        prepared = _prepared((set_aside, eligible_row))
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )

        run = evaluate_quality(
            project=self.project,
            staging=staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )
        filtered = canonical_rows_to_prepared_bundle(
            staging, run, source_hashes=prepared.source_hashes
        )

        self.assertEqual(
            [record.source_identity for record in filtered.records],
            [("ELIGIBLE",)],
        )


class QualityRelationshipScaleTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("IMPODO_RUN_QUALITY_SCALE") == "1",
        "100,000-row relationship scale probe is opt-in",
    )
    def test_deep_dependency_chain_is_linear(self) -> None:
        import psutil

        row_count = int(os.environ.get("IMPODO_QUALITY_SCALE_ROWS", "100000"))
        self.assertGreaterEqual(row_count, 2)
        fixture_started = perf_counter()
        project = replace(
            _project(),
            project_id="00000000-0000-0000-0000-000000000100",
        )
        base = _canonical_row(
            "5",
            2,
            dataset="categories",
            source_identity=("CAT-000000",),
            target_identity=("CAT-000000",),
            physical_dataset_id="dataset:categories",
        )
        rows = tuple(
            replace(
                base,
                row_id=(
                    "sha256:"
                    + sha256(f"quality-scale:{index}".encode()).hexdigest()
                ),
                source_row=index + 2,
                source_identity=(f"CAT-{index:06d}",),
                target_identity=(f"CAT-{index:06d}",),
                proposed_values={"name": f"CAT-{index:06d}"},
                lineage=replace(
                    base.lineage,
                    source_row=index + 2,
                    physical_source_rows=(index + 2,),
                ),
            )
            for index in range(row_count)
        )
        root_issue = CanonicalIssue(
            code="SOURCE_TYPE_INVALID",
            message="invalid value",
            severity="error",
            dataset="categories",
            source_row=2,
        )
        rows = (
            replace(
                rows[0],
                disposition=StagingDisposition.BLOCKED,
                issues=(root_issue,),
            ),
            *rows[1:],
        )
        rows = tuple(
            replace(
                row,
                references=(
                    {}
                    if index == 0
                    else {
                        "parent_id": LogicalReference(
                            origin="incoming",
                            key=(f"CAT-{index - 1:06d}",),
                            dataset="categories",
                            target_fields=("name",),
                        )
                    }
                ),
            )
            for index, row in enumerate(rows)
        )
        staging = _staging(project.project_id, rows)
        ruleset = default_quality_ruleset(
            project_id=project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("categories",),
        )
        fixture_elapsed = perf_counter() - fixture_started

        evaluation_started = perf_counter()
        run = evaluate_quality(
            project=project,
            staging=staging,
            physical_rows={
                "dataset:categories": tuple(range(2, row_count + 2)),
            },
            ruleset=ruleset,
        )
        evaluation_elapsed = perf_counter() - evaluation_started
        hash_started = perf_counter()
        content_hash = run.content_hash
        hash_elapsed = perf_counter() - hash_started
        memory = psutil.Process().memory_info()
        peak_mib = getattr(memory, "peak_wset", memory.rss) / (1024 * 1024)

        self.assertEqual(run.quarantined_count, row_count)
        self.assertEqual(
            sum(
                issue.reason_code == "INCOMING_RELATIONSHIP_NOT_READY"
                for issue in run.issues
            ),
            row_count - 1,
        )
        self.assertTrue(content_hash.startswith("sha256:"))
        if row_count >= 100_000:
            self.assertLess(evaluation_elapsed, 120)
        print(
            "Quality relationship scale probe: "
            f"{row_count} rows; {row_count - 1} edges; "
            f"fixture {fixture_elapsed:.3f}s; "
            f"evaluation {evaluation_elapsed:.3f}s; "
            f"quality hash {hash_elapsed:.3f}s; "
            f"{peak_mib:.1f} MiB peak working set; "
            f"staging {run.staging_content_hash}; quality {content_hash}"
        )


class QualityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbDatabase(self.temporary.name)
        self.projects = ProjectRepository(database)
        self.staging = StagingRepository(database)
        self.quality = QualityRepository(database, self.projects)
        self.project = _project()
        self.projects.create(self.project, actor=LOCAL_ACTOR)
        now = datetime.now(timezone.utc)
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=self.project.project_id,
            created_at=now,
            created_by=LOCAL_ACTOR.identity.display_name,
            datasets=(
                SourceDataset(
                    dataset_id="dataset:contacts",
                    name="contacts",
                    file_id=str(uuid4()),
                    table_key="csv",
                    source_sha256=SOURCE_HASH,
                    catalog_hash="sha256:" + "a" * 64,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                    row_count=1,
                    columns=(SourceDatasetColumn(1, "Reference", "column:reference", "string"),),
                ),
            ),
            content_hash=PHYSICAL_HASH,
        )
        database_path = self.projects.project_directory(self.project.project_id) / "project.duckdb"
        with self.projects._connect(database_path) as connection:
            connection.execute("INSERT INTO source_selection VALUES (1, ?)", [selection.to_json()])
            connection.execute(
                "INSERT INTO mapping_revision VALUES ('mapping:contacts', 1, NULL, ?, ?, ?, ?, '{}')",
                [MAPPING_HASH, PHYSICAL_HASH, SCHEMA_HASH, now.isoformat()],
            )
            connection.execute("INSERT INTO mapping_current VALUES (1, 'mapping:contacts', 1)")
            connection.execute(
                "INSERT INTO mapping_submission VALUES (?, 'mapping:contacts', 1, ?, ?, ?, '{}')",
                [str(uuid4()), MAPPING_HASH, "sha256:" + "b" * 64, now.isoformat()],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quality_rules_and_run_round_trip_idempotently(self) -> None:
        row = _canonical_row("5", 2)
        staging_run = _staging(self.project.project_id, (row,))
        staging = self.staging.publish_canonical_staging(
            self.project.project_id,
            staging_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        self.quality.publish_quality_ruleset(
            self.project.project_id,
            ruleset,
            actor=LOCAL_ACTOR,
        )
        run = evaluate_quality(
            project=self.project,
            staging=staging_run,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )

        first = self.quality.publish_quality_run(
            self.project.project_id,
            run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )
        repeated = self.quality.publish_quality_run(
            self.project.project_id,
            run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )
        restored = self.quality.get_quality_run(
            self.project.project_id,
            first.run_id,
        )

        self.assertEqual(first.run_id, repeated.run_id)
        self.assertEqual(first.status, QualityRunStatus.PUBLISHED)
        self.assertEqual(restored.to_json(), run.to_json())

    def test_quality_review_reads_deterministic_50_row_pages(self) -> None:
        rows = tuple(
            replace(
                _canonical_row(
                    "a",
                    source_row,
                    source_identity=(f"C{source_row:03d}",),
                    target_identity=(f"C{source_row:03d}",),
                ),
                row_id=f"sha256:{source_row:064x}",
            )
            for source_row in range(2, 53)
        )
        staging_run = _staging(self.project.project_id, rows)
        staging = self.staging.publish_canonical_staging(
            self.project.project_id,
            staging_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        self.quality.publish_quality_ruleset(
            self.project.project_id,
            ruleset,
            actor=LOCAL_ACTOR,
        )
        run = evaluate_quality(
            project=self.project,
            staging=staging_run,
            physical_rows={
                "dataset:contacts": tuple(range(2, 53)),
            },
            ruleset=ruleset,
        )
        published = self.quality.publish_quality_run(
            self.project.project_id,
            run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )

        first = self.quality.get_quality_review_page(
            self.project.project_id,
            published.run_id,
            page=1,
            page_size=50,
        )
        second = self.quality.get_quality_review_page(
            self.project.project_id,
            published.run_id,
            page=2,
            page_size=50,
        )

        self.assertEqual(first.matching_count, 51)
        self.assertEqual(first.page_count, 2)
        self.assertEqual(len(first.items), 50)
        self.assertEqual(first.items[0].row.source_row, 2)
        self.assertEqual(first.items[-1].row.source_row, 51)
        self.assertEqual(second.matching_count, 51)
        self.assertEqual(second.page, 2)
        self.assertEqual(len(second.items), 1)
        self.assertEqual(second.items[0].row.source_row, 52)

    def test_failed_quality_batch_preserves_previous_current_run(self) -> None:
        row = _canonical_row("5", 2)
        staging_run = _staging(self.project.project_id, (row,))
        staging = self.staging.publish_canonical_staging(
            self.project.project_id,
            staging_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        self.quality.publish_quality_ruleset(self.project.project_id, ruleset, actor=LOCAL_ACTOR)
        run = evaluate_quality(
            project=self.project,
            staging=staging_run,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )
        first = self.quality.publish_quality_run(
            self.project.project_id,
            run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )
        changed = replace(run, retention_context_hash="sha256:" + "9" * 64)

        with patch.object(self.quality, "_insert_quality_evidence", side_effect=RuntimeError("injected quality failure")):
            with self.assertRaisesRegex(RuntimeError, "injected quality failure"):
                # Keep validation legitimate while forcing a different content hash.
                with patch(
                    "impodo.adapters.duckdb.quality_repository.retention_context_hash",
                    return_value=changed.retention_context_hash,
                ):
                    self.quality.publish_quality_run(
                        self.project.project_id,
                        changed,
                        staging_run_id=staging.run_id,
                        actor=LOCAL_ACTOR,
                    )

        self.assertEqual(
            self.quality.get_current_quality_summary(self.project.project_id).run_id,
            first.run_id,
        )

    def test_owner_or_retention_change_invalidates_quality_not_staging(self) -> None:
        row = _canonical_row("5", 2)
        staging_run = _staging(self.project.project_id, (row,))
        staging = self.staging.publish_canonical_staging(
            self.project.project_id,
            staging_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset = default_quality_ruleset(
            project_id=self.project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        self.quality.publish_quality_ruleset(
            self.project.project_id,
            ruleset,
            actor=LOCAL_ACTOR,
        )
        run = evaluate_quality(
            project=self.project,
            staging=staging_run,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )
        published = self.quality.publish_quality_run(
            self.project.project_id,
            run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )
        current = self.projects.get(self.project.project_id)
        changed = replace(
            current,
            data_manager="New Data Manager",
            retention_days=60,
            revision=current.revision + 1,
            updated_at=datetime.now(timezone.utc),
        )

        self.projects.save(
            changed,
            expected_revision=current.revision,
            event_type="PROJECT_GOVERNANCE_UPDATED",
            event_detail="",
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(
            self.quality.get_current_quality_summary(self.project.project_id)
        )
        self.assertEqual(
            self.staging.get_current_staging_summary(self.project.project_id).run_id,
            staging.run_id,
        )
        database_path = self.projects.project_directory(self.project.project_id) / "project.duckdb"
        with self.projects._connect(database_path) as connection:
            lifecycle = connection.execute(
                "SELECT status, retired_reason FROM quality_run WHERE run_id = ?",
                [published.run_id],
            ).fetchone()
        self.assertEqual(
            lifecycle,
            (
                QualityRunStatus.INVALIDATED.value,
                "PROJECT_GOVERNANCE_CHANGED",
            ),
        )



def _project() -> MigrationProject:
    now = datetime.now(timezone.utc)
    return MigrationProject(
        project_id=str(uuid4()),
        name="Quality contacts",
        source_system="CSV",
        data_manager="Data Manager",
        functional_owner="Functional Owner",
        business_unit="Operations",
        odoo_connection_mode=OdooConnectionMode.LOCAL,
        odoo_base_url="http://127.0.0.1:8069",
        odoo_database="odoo19_local",
        intended_models=("res.partner",),
        status=ProjectStatus.REGISTERED,
        registered_at=now,
    )


def _canonical_row(
    token: str,
    source_row: int,
    *,
    dataset: str = "contacts",
    source_identity: tuple[object, ...] = ("C001",),
    target_identity: tuple[object, ...] = ("C001",),
    target_scope: tuple[object, ...] = (),
    physical_dataset_id: str = "dataset:contacts",
) -> CanonicalRow:
    lineage = CanonicalLineage(
        source_selection_hash=PHYSICAL_HASH,
        source_hash=SOURCE_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        derived_plan_hash=None,
        dataset=dataset,
        source_row=source_row,
        physical_dataset_id=physical_dataset_id,
        physical_source_rows=(source_row,),
        field_sources={"name": ("column:reference",)},
    )
    return CanonicalRow(
        row_id="sha256:" + token * 64,
        dataset=dataset,
        source_row=source_row,
        target_model="res.partner",
        disposition=StagingDisposition.CANDIDATE,
        source_identity=source_identity,
        target_identity=target_identity,
        target_scope=target_scope,
        proposed_values={"name": source_identity[0]},
        references={},
        issues=(),
        lineage=lineage,
    )


def _staging(project_id: str, rows: tuple[CanonicalRow, ...]) -> CanonicalStagingRun:
    datasets = []
    for dataset in sorted({item.dataset for item in rows}):
        items = tuple(item for item in rows if item.dataset == dataset)
        datasets.append(
            StagingDatasetReconciliation.from_rows(
                dataset=dataset,
                target_model=items[0].target_model,
                physical_dataset_id=items[0].lineage.physical_dataset_id,
                role=StagingDatasetRole.DIRECT,
                input_rows=len(items),
                source_rows=(item.source_row for item in items),
                lineage_links=len(items),
                rows=items,
            )
        )
    ordered = tuple(sorted(rows, key=lambda item: (item.dataset, item.source_row, item.row_id)))
    return CanonicalStagingRun(
        project_id=project_id,
        mapping_id="mapping:contacts",
        physical_selection_hash=PHYSICAL_HASH,
        source_selection_hash=PHYSICAL_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        derived_plan_hash=None,
        datasets=tuple(datasets),
        rows=ordered,
        issues=(),
        reconciliation=StagingReconciliation.from_rows(ordered),
        compiled_plan_hash=MAPPING_HASH,
    )


def _stored_staging(staging: CanonicalStagingRun) -> StoredCanonicalStagingRun:
    return StoredCanonicalStagingRun(
        project_id=staging.project_id,
        mapping_id=staging.mapping_id,
        physical_selection_hash=staging.physical_selection_hash,
        source_selection_hash=staging.source_selection_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        derived_plan_hash=staging.derived_plan_hash,
        datasets=staging.datasets,
        rows=staging.rows,
        issues=staging.issues,
        reconciliation=staging.reconciliation,
        compiled_plan_hash=staging.compiled_plan_hash,
        control_totals=staging.control_totals,
        evaluator_version=staging.evaluator_version,
        contract_version=staging.contract_version,
    )


def _materialized_bounded_quality(run) -> QualityRun:
    return QualityRun(
        project_id=run.project_id,
        staging_content_hash=run.staging_content_hash,
        ruleset_hash=run.ruleset_hash,
        mapping_hash=run.mapping_hash,
        schema_hash=run.schema_hash,
        retention_context_hash=run.retention_context_hash,
        row_results=tuple(run.row_results),
        source_accounting=tuple(run.source_accounting),
        issues=tuple(run.issues),
        quarantine=tuple(run.quarantine),
        effective_dataset_hash=run.effective_dataset_hash,
        evaluator_version=run.evaluator_version,
        contract_version=run.contract_version,
    )


def _prepared(rows: tuple[CanonicalRow, ...]) -> PreparedBundle:
    return PreparedBundle(
        records=tuple(_prepared_record(item) for item in rows),
        issues=(),
        source_hashes={item.dataset: SOURCE_HASH for item in rows},
    )


def _prepared_record(row: CanonicalRow) -> PreparedRecord:
    return PreparedRecord(
        dataset=row.dataset,
        source_row=row.source_row,
        target_model=row.target_model,
        source_identity=row.source_identity,
        target_identity=row.target_identity,
        target_scope=row.target_scope,
        scalar_values=row.proposed_values,
        references=row.references,
        issues=tuple(
            Issue(
                code=item.code,
                message=item.message,
                dataset=item.dataset,
                row=item.source_row,
                field=item.field,
            )
            for item in row.issues
        ),
    )


if __name__ == "__main__":
    unittest.main()
