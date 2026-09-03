from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

from impodo.application.transfer_review_service import TransferReviewService
from impodo.application.transfer_order_service import TransferOrderService
from impodo.domain.shared.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import (
    TransferReviewApproval,
    TransferReviewPackage,
)
from impodo.domain.workspace.workbench import WorkspaceStateService
from tests.application.workspace.test_transfer_order import (
    _build,
    _match_plan,
    _model,
    _relation,
    _workspace,
)


class TransferReviewTests(unittest.TestCase):
    def test_package_combines_write_scope_order_relations_and_controls(self) -> None:
        product = replace(
            _model("product.template", "Product", existing=1, create=1),
            compatible_fields=("default_code", "name", "uom_id"),
            key_field="default_code",
            key_field_label="Internal Reference",
        )
        uom = _model("uom.uom", "Unit of Measure", existing=1, create=1)
        relation = replace(
            _relation(product, uom, "uom_id", required=True),
            inverse_field="product_ids",
            source_link_count=2,
            destination_reused_link_count=1,
            incoming_link_count=1,
        )
        match = _match_plan((product, uom), (relation,))

        package = _package(match)

        self.assertEqual(package.totals.dataset_count, 2)
        self.assertEqual(package.totals.wave_count, 2)
        self.assertEqual(package.totals.source_record_count, 4)
        self.assertEqual(package.totals.destination_existing_record_count, 2)
        self.assertEqual(package.totals.destination_create_record_count, 2)
        self.assertEqual(package.totals.source_relationship_link_count, 2)
        self.assertEqual(package.totals.destination_reused_link_count, 1)
        self.assertEqual(package.totals.incoming_link_count, 1)
        self.assertEqual(package.totals.post_create_link_count, 0)
        product_scope = next(
            item for item in package.datasets if item.model == "product.template"
        )
        self.assertEqual(
            product_scope.scalar_write_fields,
            ("default_code", "name"),
        )
        self.assertEqual(product_scope.relationship_write_fields, ("uom_id",))
        self.assertEqual(package.relationships[0].operation, "set")
        self.assertEqual(package.relationships[0].inverse_field, "product_ids")
        self.assertEqual(package.relationships[0].phase, "create_or_update")
        self.assertEqual(
            package.export_plan.source_hashes["destination_matching"],
            match.content_hash,
        )
        self.assertEqual(TransferReviewPackage.from_json(package.to_json()), package)

    def test_optional_cycle_is_disclosed_as_post_create_work(self) -> None:
        alpha = _model("x.alpha", "Alpha", create=1)
        beta = _model("x.beta", "Beta", create=1)
        match = _match_plan(
            (alpha, beta),
            (
                _relation(alpha, beta, "beta_ids", kind="many2many"),
                _relation(beta, alpha, "alpha_id"),
            ),
        )

        package = _package(match)

        deferred = next(
            item for item in package.relationships if item.phase == "post_create"
        )
        self.assertEqual(
            (deferred.owner_model, deferred.field_name, deferred.operation),
            ("x.alpha", "beta_ids", "replace"),
        )
        self.assertEqual(package.totals.post_create_link_count, 1)

    def test_approval_binds_exact_package_and_stable_actor_identity(self) -> None:
        product = _model("product.template", "Product", create=1)
        package = _package(_match_plan((product,), ()))
        approved_at = datetime.now(UTC)

        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=approved_at,
            reason="Reviewed counts and field scope.",
        )

        self.assertTrue(approval.authorizes(package, at=approved_at))
        self.assertEqual(approval.approved_by, LOCAL_ACTOR.identity)
        self.assertEqual(approval.reason, "Reviewed counts and field scope.")
        self.assertEqual(
            TransferReviewApproval.from_json(approval.to_json()),
            approval,
        )
        changed = replace(
            package,
            export_plan=replace(
                package.export_plan,
                frozen_at=package.export_plan.frozen_at + timedelta(seconds=1),
            ),
        )
        self.assertFalse(approval.authorizes(changed, at=approved_at))

    def test_stale_destination_matching_cannot_be_frozen_for_review(self) -> None:
        product = _model("product.template", "Product", create=1)
        match = _match_plan((product,), ())
        order = _build(match)
        workspace = replace(
            _workspace(match),
            transfer_order_plan=order,
            destination_verified_at=match.recorded_at + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "Complete the current transfer order first",
        ):
            TransferReviewService().build(
                workspace,
                match,
                order,
                run_id=str(uuid4()),
                data_version_id=str(uuid4()),
                built_by=LOCAL_ACTOR.identity,
            )

    def test_workspace_service_persists_approval_and_invalidates_it_on_reorder(
        self,
    ) -> None:
        product = _model("product.template", "Product", create=1)
        match = _match_plan((product,), ())
        order = _build(match)
        initial = replace(_workspace(match), transfer_order_plan=order)
        package = TransferReviewService().build(
            initial,
            match,
            order,
            run_id=str(uuid4()),
            data_version_id=str(uuid4()),
            built_by=LOCAL_ACTOR.identity,
        )
        repository = _WorkspaceRepository(initial)
        states = WorkspaceStateService(
            repository,
            CapabilityAuthorizationPolicy(),
        )

        reviewed = states.save_transfer_review_package(
            initial.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=initial.revision,
            package=package,
        )
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        approved = states.approve_transfer_review(
            initial.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=reviewed.revision,
            approval=approval,
        )

        self.assertTrue(
            approved.transfer_review_approved(
                source_selection_hash=match.source_selection_hash,
                source_schema_hash=match.source_schema_hash,
            )
        )
        replacement_order = TransferOrderService().build(
            approved,
            match,
            recorded_by="Data manager",
        )
        reordered = states.save_transfer_order_plan(
            initial.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=approved.revision,
            plan=replacement_order,
        )
        self.assertIsNone(reordered.transfer_review_package)
        self.assertIsNone(reordered.transfer_review_approval)
        self.assertEqual(
            repository.events,
            [
                "WORKSPACE_TRANSFER_REVIEW_FROZEN",
                "WORKSPACE_TRANSFER_REVIEW_APPROVED",
                "WORKSPACE_TRANSFER_ORDER_PLANNED",
            ],
        )


def _package(match):
    order = _build(match)
    workspace = replace(_workspace(match), transfer_order_plan=order)
    return TransferReviewService().build(
        workspace,
        match,
        order,
        run_id=str(uuid4()),
        data_version_id=str(uuid4()),
        built_by=LOCAL_ACTOR.identity,
    )


class _WorkspaceRepository:
    def __init__(self, workspace) -> None:
        self.workspace = workspace
        self.events = []

    def get(self, _workspace_id):
        return self.workspace

    def assert_workspace_mutable(self, _workspace_id):
        return None

    def save(
        self,
        workspace,
        *,
        expected_revision,
        event_type,
        event_detail,
        actor,
    ):
        if self.workspace.revision != expected_revision:
            raise AssertionError("unexpected revision")
        self.workspace = workspace
        self.events.append(event_type)


if __name__ == "__main__":
    unittest.main()
