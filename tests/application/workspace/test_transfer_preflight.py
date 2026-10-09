from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest
from uuid import uuid4

from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.workspace.transfer_preflight import TransferPreflightReport
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from impodo.domain.workspace.workbench import WorkspaceStateService
from tests.application.workspace.test_transfer_order import (
    _build,
    _match_plan,
    _model,
    _relation,
    _workspace,
)
from tests.application.workspace.test_transfer_review import _WorkspaceRepository


class TransferPreflightTests(unittest.TestCase):
    def test_unchanged_fresh_match_produces_portable_ready_report(self) -> None:
        workspace, package, approval, match = _approved_state()
        fresh = _fresh(match)

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            fresh,
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertTrue(report.ready)
        self.assertEqual(report.datasets[0].approved_create_record_count, 1)
        self.assertEqual(report.datasets[0].observed_create_record_count, 1)
        self.assertEqual(TransferPreflightReport.from_json(report.to_json()), report)
        self.assertNotIn("destination-secret", report.to_json())
        self.assertNotIn("odoo_id", report.to_json())

    def test_changed_create_update_classification_blocks_preflight(self) -> None:
        workspace, package, approval, match = _approved_state(existing=1, create=1)
        changed_model = replace(
            match.model_matches[0],
            destination_existing_key_count=2,
            destination_create_key_count=0,
        )
        fresh = _fresh(match, model_matches=(changed_model,))

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            fresh,
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertFalse(report.ready)
        self.assertIn(
            "DESTINATION_RECORD_CLASSIFICATION_DRIFT",
            report.datasets[0].blocker_codes,
        )

    def test_changed_key_to_record_binding_blocks_even_when_counts_match(self) -> None:
        workspace, package, approval, match = _approved_state(existing=1, create=1)
        changed_model = replace(
            match.model_matches[0],
            destination_key_binding_hash="sha256:" + "9" * 64,
        )

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match, model_matches=(changed_model,)),
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertFalse(report.ready)
        self.assertNotIn(
            "DESTINATION_RECORD_CLASSIFICATION_DRIFT",
            report.datasets[0].blocker_codes,
        )
        self.assertIn(
            "DESTINATION_RECORD_IDENTITY_DRIFT",
            report.datasets[0].blocker_codes,
        )

    def test_minimum_create_ignores_optional_compatible_fields(self) -> None:
        product = replace(
            _model("product.template", "Product", existing=1, create=1),
            compatible_fields=("default_code", "name", "note"),
            minimum_create_fields=("default_code", "name"),
            destination_handling="reuse_or_create",
        )
        match = _match_plan((product,), ())
        workspace, package, approval = _approve(match)

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match),
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertTrue(report.ready)
        self.assertEqual(
            report.datasets[0].observed_compatible_fields,
            ("default_code", "name"),
        )
        self.assertNotIn(
            "DESTINATION_FIELD_SCOPE_DRIFT",
            report.datasets[0].blocker_codes,
        )

    def test_minimum_create_includes_governed_relationship_field(self) -> None:
        category = replace(
            _model("product.category", "Product Category", existing=1, create=1),
            compatible_fields=("name", "note", "parent_id"),
            minimum_create_fields=("name",),
            minimum_create_relationship_fields=("parent_id",),
            destination_handling="reuse_or_create",
        )
        parent = replace(
            _relation(category, category, "parent_id"),
            destination_reused_link_count=1,
            incoming_link_count=0,
        )
        match = _match_plan((category,), (parent,))
        workspace, package, approval = _approve(match)

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match),
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertTrue(report.ready)
        self.assertEqual(
            report.datasets[0].approved_write_fields,
            ("name", "parent_id"),
        )
        self.assertEqual(
            report.datasets[0].observed_compatible_fields,
            ("name", "parent_id"),
        )

    def test_reuse_or_create_with_no_creates_has_no_observed_write_scope(
        self,
    ) -> None:
        owner = replace(
            _model("x.owner", "Owner", existing=1),
            compatible_fields=("name",),
            incompatible_fields=("required_id",),
            minimum_create_fields=("name",),
            destination_handling="reuse_or_create",
        )
        related = replace(
            _model("x.related", "Related", existing=1),
            destination_handling="reference_only",
        )
        required = replace(
            _relation(owner, related, "required_id", required=True),
            destination_reused_link_count=1,
            incoming_link_count=0,
        )
        match = _match_plan((owner, related), (required,))
        workspace, package, approval = _approve(match)

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match),
            recorded_by=LOCAL_ACTOR.identity,
        )

        reviewed = next(item for item in report.datasets if item.model == owner.model)
        self.assertTrue(report.ready)
        self.assertFalse(reviewed.approved_write_fields)
        self.assertFalse(reviewed.observed_compatible_fields)
        self.assertNotIn(
            "DESTINATION_FIELD_SCOPE_DRIFT",
            reviewed.blocker_codes,
        )

    def test_new_required_create_field_blocks_after_approval(self) -> None:
        workspace, package, approval, match = _approved_state()
        changed_model = replace(
            match.model_matches[0],
            unresolved_create_fields=("company_id",),
        )
        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match, model_matches=(changed_model,)),
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.assertFalse(report.ready)
        self.assertIn(
            "DESTINATION_CREATE_FIELDS_UNRESOLVED",
            report.datasets[0].blocker_codes,
        )
        self.assertIn(
            "DESTINATION_CREATE_FIELD_DRIFT",
            report.datasets[0].blocker_codes,
        )

    def test_permission_and_relationship_resolution_drift_both_block(self) -> None:
        product = replace(
            _model("product.template", "Product", create=1),
            compatible_fields=("name", "uom_id"),
        )
        uom = _model("uom.uom", "Unit of Measure", existing=1)
        relation = replace(
            _relation(product, uom, "uom_id"),
            destination_reused_link_count=1,
            incoming_link_count=0,
        )
        match = _match_plan((product, uom), (relation,))
        workspace, package, approval = _approve(match)
        fresh_relation = replace(
            relation,
            destination_reused_link_count=0,
            incoming_link_count=1,
        )
        fresh = _fresh(
            match,
            permission_hash="sha256:" + "9" * 64,
            relationship_matches=(fresh_relation,),
        )

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            fresh,
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertFalse(report.ready)
        self.assertIn("DESTINATION_PERMISSION_DRIFT", report.blocker_codes)
        self.assertIn(
            "DESTINATION_RELATIONSHIP_RESOLUTION_DRIFT",
            report.relationships[0].blocker_codes,
        )

    def test_full_transfer_still_blocks_an_unapproved_relationship(self) -> None:
        product = replace(
            _model("product.template", "Product", create=1),
            compatible_fields=("name", "uom_id"),
        )
        uom = _model("uom.uom", "Unit of Measure", existing=1)
        match = _match_plan((product, uom), ())
        workspace, package, approval = _approve(match)
        new_relation = replace(
            _relation(product, uom, "uom_id"),
            destination_reused_link_count=1,
            incoming_link_count=0,
        )

        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match, relationship_matches=(new_relation,)),
            recorded_by=LOCAL_ACTOR.identity,
        )

        self.assertFalse(report.ready)
        self.assertIn(
            "DESTINATION_RELATIONSHIP_SCOPE_DRIFT",
            report.blocker_codes,
        )

    def test_workspace_saves_preflight_and_reapproval_invalidates_it(self) -> None:
        workspace, package, approval, match = _approved_state()
        report = TransferPreflightService().build(
            workspace,
            package,
            approval,
            match,
            _fresh(match),
            recorded_by=LOCAL_ACTOR.identity,
        )
        repository = _WorkspaceRepository(workspace)
        states = WorkspaceStateService(repository, CapabilityAuthorizationPolicy())

        checked = states.save_transfer_preflight_report(
            workspace.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=workspace.revision,
            report=report,
        )

        self.assertTrue(
            checked.transfer_preflight_ready(
                source_selection_hash=match.source_selection_hash,
                source_schema_hash=match.source_schema_hash,
            )
        )
        replacement_approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        reapproved = states.approve_transfer_review(
            workspace.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=checked.revision,
            approval=replacement_approval,
        )
        self.assertIsNone(reapproved.transfer_preflight_report)


def _approved_state(*, existing: int = 0, create: int = 1):
    match = _match_plan(
        (_model("product.template", "Product", existing=existing, create=create),),
        (),
    )
    workspace, package, approval = _approve(match)
    return workspace, package, approval, match


def _approve(match):
    order = _build(match)
    workspace = replace(_workspace(match), transfer_order_plan=order)
    package = TransferReviewService().build(
        workspace,
        match,
        order,
        run_id=str(uuid4()),
        data_version_id=str(uuid4()),
        built_by=LOCAL_ACTOR.identity,
    )
    approval = TransferReviewApproval.approve(
        package,
        approval_id=str(uuid4()),
        actor=LOCAL_ACTOR,
        approved_at=datetime.now(UTC),
    )
    return (
        replace(
            workspace,
            transfer_review_package=package,
            transfer_review_approval=approval,
        ),
        package,
        approval,
    )


def _fresh(
    match,
    *,
    model_matches=None,
    relationship_matches=None,
    permission_hash=None,
):
    return replace(
        match,
        model_matches=model_matches or match.model_matches,
        relationship_matches=(
            relationship_matches
            if relationship_matches is not None
            else match.relationship_matches
        ),
        destination_read_permission_hash=(
            permission_hash or match.destination_read_permission_hash
        ),
        destination_schema_snapshot_hash="sha256:" + "7" * 64,
        destination_record_snapshot_hash="sha256:" + "8" * 64,
        recorded_at=datetime.now(UTC),
        recorded_by="Stage 6 preflight",
    )


if __name__ == "__main__":
    unittest.main()
