"""Prove governed transfer populations through matching, review, and loading."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import polars as pl

from impodo.application.destination_matching_service import (
    DestinationMatchingService,
    DestinationMatchKeyChoice,
)
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.mapping.contracts import (
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    RetiredDestinationMatchPlanError,
    confirm_destination_row_inclusion,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from tests.application.workspace import test_transfer_execution as execution_fixtures
from tests.application.workspace.mapping import (
    test_categorical_coverage as coverage_fixtures,
)
from tests.application.workspace.test_destination_matching import (
    BINDING_HASH,
    _identity,
)


class TransferRowInclusionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = execution_fixtures.TransferExecutionCompilerTests("runTest")
        self.fixture.setUp()
        self.product, self.uom = self.fixture.selection.datasets
        self.values = self.fixture.source_values
        for dataset in self.fixture.selection.datasets:
            for column in dataset.columns:
                self.values.rows[(dataset.dataset_id, (column.stable_key,))] = tuple(
                    (row.values.get(column.source_name),)
                    for row in self.fixture.rows[dataset.dataset_id]
                )

    def policy(self, value="P001", *, column="product-code", value_type="string"):
        return RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            conditions=(
                RowInclusionCondition(
                    condition_id=str(uuid4()),
                    source_column_key=column,
                    operator=SelectionConditionOperator.EQUALS,
                    comparison_value=value,
                    value_type=value_type,
                ),
            ),
        )

    def check(
        self,
        policy=None,
        *,
        uom_policy=None,
        confirmation=None,
        exclusions=None,
        reader=None,
    ):
        return DestinationMatchingService(self.values).check(
            self.fixture.workspace,
            self.fixture.selection,
            self.fixture.schema,
            (
                DestinationMatchKeyChoice(
                    self.product.dataset_id,
                    "product-code",
                    row_inclusion=policy or RowInclusionPolicy(),
                ),
                DestinationMatchKeyChoice(
                    self.uom.dataset_id,
                    "uom-name",
                    row_inclusion=uom_policy or RowInclusionPolicy(),
                ),
            ),
            api_key="test-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.fixture.workspace),
            reader=reader or self.fixture.reader,
            recorded_by="Data manager",
            source_origins=self.fixture.origins,
            excluded_source_rows=exclusions,
            row_inclusion_confirmation_hash=confirmation,
        )

    def approve(self, match):
        workspace = replace(self.fixture.workspace, destination_match_plan=match)
        order = TransferOrderService().build(
            workspace, match, recorded_by="Data manager"
        )
        workspace = replace(workspace, transfer_order_plan=order)
        package = TransferReviewService().build(
            workspace,
            match,
            order,
            run_id=str(uuid4()),
            data_version_id=self.fixture.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
        )
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        workspace = replace(
            workspace,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        return replace(workspace, transfer_preflight_report=report), package, report

    def test_rule_filters_before_destination_matching_and_needs_exact_confirmation(
        self,
    ):
        reader = Mock(wraps=self.fixture.reader)
        policy = self.policy()
        plan = self.check(policy, reader=reader)
        product = next(
            item
            for item in plan.model_matches
            if item.dataset_id == self.product.dataset_id
        )
        self.assertEqual(product.frozen_source_row_count, 2)
        self.assertEqual(product.source_row_count, 1)
        self.assertEqual(product.rule_excluded_source_row_numbers, (2,))
        self.assertEqual(product.excluded_source_row_numbers, ())
        self.assertEqual(product.destination_create_key_count, 0)
        self.assertEqual(product.destination_existing_key_count, 1)
        self.assertFalse(plan.ready)
        self.assertNotIn("P002", repr(reader.call_args.args[-1]))
        with self.assertRaises(WorkspaceError):
            self.approve(plan)
        with self.assertRaisesRegex(ValueError, "changed"):
            confirm_destination_row_inclusion(plan, expected_plan_hash=BINDING_HASH)
        confirmed = confirm_destination_row_inclusion(
            plan, expected_plan_hash=plan.content_hash
        )
        self.assertTrue(confirmed.ready)
        self.assertEqual(DestinationMatchPlan.from_json(confirmed.to_json()), confirmed)
        repeated = self.check(
            policy, confirmation=confirmed.row_inclusion_confirmation_hash
        )
        self.assertTrue(repeated.row_inclusion_confirmed)
        edited = self.check(
            replace(
                policy,
                conditions=(replace(policy.conditions[0], comparison_value="P002"),),
            ),
            confirmation=confirmed.row_inclusion_confirmation_hash,
        )
        self.assertFalse(edited.row_inclusion_confirmed)

    def test_invalid_column_or_literal_stops_before_remote_access(self):
        reader = Mock()
        for policy in (
            self.policy(column="missing"),
            self.policy("bad", value_type="integer"),
        ):
            with self.subTest(policy=policy), self.assertRaises(WorkspaceError):
                self.check(policy, reader=reader)
        reader.assert_not_called()

    def test_zero_matches_and_unreadable_values_cannot_be_confirmed(self):
        for policy, blocker in (
            (self.policy("absent"), "ROW_INCLUSION_ZERO_INCLUDED"),
            (self.policy("1", value_type="integer"), "ROW_INCLUSION_CANNOT_EVALUATE"),
        ):
            with self.subTest(blocker=blocker):
                plan = self.check(policy)
                product = next(
                    item
                    for item in plan.model_matches
                    if item.dataset_id == self.product.dataset_id
                )
                self.assertIn(blocker, product.blocking_reasons)
                self.assertFalse(plan.ready)
                with self.assertRaisesRegex(ValueError, "Correct"):
                    confirm_destination_row_inclusion(
                        plan, expected_plan_hash=plan.content_hash
                    )

    def test_excluded_dependency_blocks_an_included_owner(self):
        plan = self.check(uom_policy=self.policy("Unit", column="uom-name"))
        required = next(
            item for item in plan.relationship_matches if item.field_name == "uom_id"
        )
        self.assertEqual(required.missing_related_record_count, 1)
        confirmed = confirm_destination_row_inclusion(
            plan, expected_plan_hash=plan.content_hash
        )
        self.assertFalse(confirmed.ready)
        with self.assertRaises(WorkspaceError):
            self.approve(confirmed)

    def test_execution_omits_rule_exclusions_and_rechecks_frozen_values(self):
        match = self.check(self.policy())
        match = confirm_destination_row_inclusion(
            match, expected_plan_hash=match.content_hash
        )
        workspace, package, report = self.approve(match)

        def compile_match(current=match, rows=None):
            return compile_transfer_execution_snapshot(
                workspace,
                self.fixture.selection,
                self.fixture.schema,
                package,
                report,
                current,
                self.fixture.records,
                source_rows=rows or self.fixture.rows,
                source_origins=self.fixture.origins,
                source_snapshots=self.fixture.snapshots,
                source_manifest_hashes=self.fixture.manifests,
            )

        snapshot = compile_match()
        product_rows = tuple(
            row for row in snapshot.rows if row.dataset == self.product.name
        )
        self.assertEqual(tuple(row.source_row for row in product_rows), (1,))
        reviewed = next(
            item
            for item in package.datasets
            if item.dataset_id == self.product.dataset_id
        )
        self.assertEqual(reviewed.excluded_source_record_count, 1)
        self.assertEqual(
            reviewed.source_row_count + reviewed.excluded_source_record_count, 2
        )
        with self.assertRaisesRegex(WorkspaceError, "confirm"):
            compile_match(replace(match, row_inclusion_confirmation_hash=None))
        changed = dict(self.fixture.rows)
        changed[self.product.dataset_id] = tuple(
            replace(row, values={**row.values, "default_code": "P001"})
            for row in changed[self.product.dataset_id]
        )
        with self.assertRaisesRegex(WorkspaceError, "rows to use changed"):
            compile_match(rows=changed)

    def test_preflight_rejects_same_count_but_changed_rule_meaning(self):
        policy = self.policy()
        match = self.check(policy)
        match = confirm_destination_row_inclusion(
            match, expected_plan_hash=match.content_hash
        )
        workspace, package, _report = self.approve(match)
        changed = replace(
            policy,
            conditions=(
                replace(
                    policy.conditions[0],
                    operator=SelectionConditionOperator.NOT_EQUALS,
                    comparison_value="P002",
                ),
            ),
        )
        fresh = self.check(changed)
        report = TransferPreflightService().build(
            workspace,
            package,
            workspace.transfer_review_approval,
            match,
            fresh,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.assertIn("ROW_INCLUSION_SCOPE_DRIFT", report.blocker_codes)
        self.assertFalse(report.ready)

    def test_retired_plan_requires_fresh_destination_check(self):
        with self.assertRaises(RetiredDestinationMatchPlanError):
            replace(self.check(), contract_version=15)
        with self.assertRaisesRegex(
            RetiredDestinationMatchPlanError, "Check the destination again"
        ):
            DestinationMatchPlan.from_json(json.dumps({"contract_version": 15}))

    def test_final_odoo20_pair_accepts_rules_and_cross_major_pair_stays_blocked(self):
        self.fixture.workspace = replace(
            self.fixture.workspace, destination_verified_odoo_version="20.0+e"
        )
        self.fixture.schema = replace(self.fixture.schema, odoo_version="20.0+e")

        def odoo20_reader(*args):
            metadata, records = self.fixture.reader(*args)
            fingerprint = replace(metadata.fingerprint, odoo_version="20.0+e")
            return replace(metadata, fingerprint=fingerprint), replace(
                records, fingerprint=fingerprint
            )

        plan = self.check(self.policy(), reader=odoo20_reader)
        confirmed = confirm_destination_row_inclusion(
            plan, expected_plan_hash=plan.content_hash
        )
        self.assertTrue(confirmed.ready)
        self.fixture.schema = replace(self.fixture.schema, odoo_version="19.0")
        with self.assertRaisesRegex(WorkspaceError, "same major version"):
            self.check(self.policy(), reader=odoo20_reader)

    def test_rule_exclusions_do_not_erase_independent_manual_choices(self):
        self.values.rows[(self.product.dataset_id, ("product-code",))] = (
            ("P001",),
            ("P001",),
        )
        policy = self.policy("Product 1", column="product-name")
        plan = self.check(policy, exclusions={self.product.dataset_id: (2,)})
        product = next(
            item
            for item in plan.model_matches
            if item.dataset_id == self.product.dataset_id
        )
        self.assertEqual(product.rule_excluded_source_row_numbers, (2,))
        self.assertEqual(product.excluded_source_row_numbers, (2,))
        self.assertEqual(product.omitted_source_row_numbers, (2,))
        self.assertEqual(product.frozen_source_row_count, 2)
        self.assertEqual(product.source_row_count, 1)
        changed = self.check(
            self.policy("Product 2", column="product-name"),
            exclusions={self.product.dataset_id: (2,)},
        )
        self.assertIn(
            "ROW_INCLUSION_ZERO_INCLUDED",
            next(
                item
                for item in changed.model_matches
                if item.dataset_id == self.product.dataset_id
            ).blocking_reasons,
        )

    def test_text_and_boolean_operands_retain_their_original_meaning(self):
        self.values.rows[(self.product.dataset_id, ("product-name",))] = (
            (" Product 1 ",),
            (False,),
        )
        exact = self.check(self.policy("Product 1", column="product-name"))
        self.assertIn(
            "ROW_INCLUSION_ZERO_INCLUDED", exact.model_matches[0].blocking_reasons
        )
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS,
            conditions=(
                RowInclusionCondition(
                    condition_id=str(uuid4()),
                    source_column_key="product-name",
                    operator=SelectionConditionOperator.IS_FALSE,
                    value_type="boolean",
                ),
            ),
        )
        false_only = self.check(policy)
        product = next(
            item
            for item in false_only.model_matches
            if item.dataset_id == self.product.dataset_id
        )
        self.assertEqual(product.rule_blocked_source_row_numbers, (1,))
        self.assertEqual(product.source_row_count, 1)


class ExactTransferRuleProjectionTests(unittest.TestCase):
    def test_raw_projection_preserves_whitespace_false_and_empty_cells(self):
        fixture = coverage_fixtures.CategoricalCoverageTests("runTest")
        fixture.setUp()
        frame = pl.DataFrame(
            {"language": [" Alice ", "False", ""], "country": [False, True, None]}
        )
        service = coverage_fixtures._RecordingCoverageService(
            coverage_fixtures._Sources(fixture.selection), frame
        )
        values = service.source_condition_rows(
            fixture.workspace_id, "dataset:customers", ("language", "country")
        )
        self.assertEqual(values, ((" Alice ", False), ("False", True), ("", None)))
        self.assertEqual(
            service.scan_calls, [("dataset:customers", ("language", "country"))]
        )


if __name__ == "__main__":
    unittest.main()
