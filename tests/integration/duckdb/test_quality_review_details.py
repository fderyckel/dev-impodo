"""Prove missing-match explanations from saved joins and complete membership."""

from dataclasses import replace
from datetime import UTC, datetime
import json
import unittest
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.quality_review_details import explain_source_match_causes
from impodo.domain.preparation.quality import (
    QualityCauseCount,
    QualityOutcomePolicy,
    QualityRuleFamily,
)
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    StructuralOutputColumn,
    StructuralProjection,
    structural_dataset_id,
)
from impodo.domain.workspace.derived_entities import DerivedEntityPlan


class SourceMatchExplanationTests(unittest.TestCase):
    def setUp(self):
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        for sql in (
            "CREATE TABLE canonical_staging_run(run_id VARCHAR, derived_plan_hash VARCHAR)",
            "CREATE TABLE derived_entity_plan_revision(content_hash VARCHAR, version INT, plan_json VARCHAR)",
            "CREATE TABLE source_selection(selection_json VARCHAR)",
            "CREATE TABLE canonical_staging_row(run_id VARCHAR, row_id VARCHAR, row_json VARCHAR)",
            "CREATE TABLE quality_cause_member(run_id VARCHAR, cause_key VARCHAR, row_id VARCHAR, impact_kind VARCHAR)",
        ):
            self.connection.execute(sql)

    def fixture(self, dataset="suppliers", *, matched=False, extra_duplicate=False):
        first = ExactJoinRule(
            rule_id=str(uuid4()),
            output_dataset_name="joined_base",
            left_dataset_id="dataset:base",
            right_dataset_id="dataset:related",
            keys=(JoinKey("party", "party", "string"),),
            output_columns=(StructuralOutputColumn("key", "Account", "string"),),
            projections=(StructuralProjection("key", "dataset:related", "account"),),
            require_all_right_rows=False,
        )
        second = ExactJoinRule(
            rule_id=str(uuid4()),
            output_dataset_name=dataset,
            left_dataset_id=structural_dataset_id(first),
            right_dataset_id="dataset:names",
            keys=(JoinKey("key", "account", "string"),),
            output_columns=(StructuralOutputColumn("key", "Account", "string"),),
            projections=(
                StructuralProjection("key", structural_dataset_id(first), "key"),
            ),
            require_all_right_rows=False,
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=str(uuid4()),
            source_selection_hash="sha256:" + "1" * 64,
            rules=(first, second),
            updated_at=datetime.now(UTC),
            updated_by="test",
        )
        self.connection.execute(
            "INSERT INTO canonical_staging_run VALUES ('staging', ?)",
            [plan.content_hash],
        )
        self.connection.execute(
            "INSERT INTO derived_entity_plan_revision VALUES (?, 1, ?)",
            [plan.content_hash, json.dumps(plan.to_dict())],
        )
        self.connection.execute(
            "INSERT INTO source_selection VALUES (?)",
            [
                json.dumps(
                    {
                        "content_hash": plan.source_selection_hash,
                        "datasets": [
                            {
                                "dataset_id": "dataset:related",
                                "name": "related_accounts",
                            }
                        ],
                    }
                )
            ],
        )
        invalid_key, duplicate_key = "sha256:" + "2" * 64, "sha256:" + "3" * 64
        for index in range(3 if extra_duplicate else 2):
            lineage = {"dataset:base": [index + 2]}
            if matched:
                lineage["dataset:related"] = [index + 2]
            payload = {
                "source_identity": [None],
                "lineage": {
                    "physical_sources": lineage,
                    "field_sources": {"$source_identity": ["key"]},
                },
            }
            self.connection.execute(
                "INSERT INTO canonical_staging_row VALUES ('staging', ?, ?)",
                [str(index), json.dumps(payload)],
            )
            keys = (invalid_key, duplicate_key) if index < 2 else (duplicate_key,)
            for key in keys:
                self.connection.execute(
                    "INSERT INTO quality_cause_member VALUES ('quality', ?, ?, 'DIRECT')",
                    [key, str(index)],
                )
        return (
            QualityCauseCount(
                dataset,
                "SOURCE_IDENTITY_INVALID",
                "required value is empty",
                ("key",),
                2,
                family=QualityRuleFamily.BOUNDED_VALUES,
                policy=QualityOutcomePolicy.QUARANTINE,
                cause_key=invalid_key,
            ),
            QualityCauseCount(
                dataset,
                "SOURCE_IDENTITY_DUPLICATE",
                "empty key occurs twice",
                (),
                3 if extra_duplicate else 2,
                family=QualityRuleFamily.BOUNDED_VALUES,
                policy=QualityOutcomePolicy.QUARANTINE,
                cause_key=duplicate_key,
            ),
        )

    def test_missing_match_explains_both_findings_without_changing_evidence(self):
        causes = self.fixture()
        before = [cause.to_portable_dict() for cause in causes]
        explained = explain_source_match_causes(
            self.connection, "quality", "staging", causes
        )
        self.assertEqual(explained[0].missing_match_table, "related_accounts")
        self.assertEqual(explained[0].missing_match_field_label, "Account")
        self.assertEqual(explained[1].explained_by_cause_key, causes[0].cause_key)
        self.assertEqual([cause.to_portable_dict() for cause in explained], before)

    def test_matched_but_empty_column_does_not_claim_a_missing_match(self):
        causes = self.fixture(matched=True)
        self.assertEqual(
            explain_source_match_causes(self.connection, "quality", "staging", causes),
            causes,
        )

    def test_other_duplicate_rows_keep_the_duplicate_warning(self):
        causes = self.fixture(extra_duplicate=True)
        explained = explain_source_match_causes(
            self.connection, "quality", "staging", causes
        )
        self.assertTrue(explained[0].missing_match_table)
        self.assertFalse(explained[1].explained_by_cause_key)

    def test_equal_counts_with_different_members_do_not_hide_duplicates(self):
        causes = self.fixture()
        self.connection.execute(
            "UPDATE quality_cause_member SET row_id='other' WHERE cause_key=? AND row_id='1'",
            [causes[1].cause_key],
        )
        self.connection.execute(
            "INSERT INTO canonical_staging_row SELECT run_id, 'other', row_json FROM canonical_staging_row LIMIT 1"
        )
        explained = explain_source_match_causes(
            self.connection, "quality", "staging", causes
        )
        self.assertFalse(explained[1].explained_by_cause_key)

    def test_valid_duplicate_identity_is_not_hidden(self):
        causes = self.fixture()
        self.connection.execute(
            "UPDATE canonical_staging_row SET row_json=json_merge_patch(row_json, ?)",
            [json.dumps({"source_identity": ["real-key"]})],
        )
        explained = explain_source_match_causes(
            self.connection, "quality", "staging", causes
        )
        self.assertFalse(explained[1].explained_by_cause_key)

    def test_partial_missing_matches_keep_the_general_cause(self):
        causes = self.fixture()
        self.connection.execute(
            "UPDATE canonical_staging_row SET row_json=json_merge_patch(row_json, ?) WHERE row_id='1'",
            [json.dumps({"lineage": {"physical_sources": {"dataset:related": [3]}}})],
        )
        explained = explain_source_match_causes(
            self.connection, "quality", "staging", causes
        )
        self.assertEqual(explained, causes)

    def test_another_record_type_uses_the_same_explanation(self):
        causes = self.fixture(dataset="products")
        explained = explain_source_match_causes(
            self.connection, "quality", "staging", causes
        )
        self.assertEqual(explained[0].dataset, "products")
        self.assertEqual(explained[0].missing_match_table, "related_accounts")

    def test_changed_source_binding_keeps_the_original_findings(self):
        causes = self.fixture()
        self.connection.execute(
            "UPDATE source_selection SET selection_json=?",
            [json.dumps({"content_hash": "sha256:" + "9" * 64})],
        )
        self.assertEqual(
            explain_source_match_causes(self.connection, "quality", "staging", causes),
            causes,
        )

    def test_no_identity_findings_requires_no_metadata(self):
        cause = replace(
            QualityCauseCount("products", "SOURCE_TYPE_INVALID", "invalid", (), 1),
            cause_key="sha256:" + "4" * 64,
        )
        self.assertEqual(
            explain_source_match_causes(
                self.connection, "quality", "staging", (cause,)
            ),
            (cause,),
        )
