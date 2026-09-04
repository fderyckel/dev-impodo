from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.application.transfer_review_service import TransferReviewService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from impodo.web.presenters.navigation import build_workspace_navigation
from tests.application.workspace.test_destination_matching import (
    _selection,
    _source_schema,
)
from tests.application.workspace.test_transfer_order import (
    _build,
    _match_plan,
    _model,
    _workspace,
)
from tests.application.workspace.test_transfer_preflight import _fresh


class TransferOrderNavigationTests(unittest.TestCase):
    def test_stage_six_completion_unlocks_the_next_work_boundary(self) -> None:
        workspace, selection, schema = _stage_six_state()
        queries = _Queries(workspace, selection, schema)
        context = SimpleNamespace(queries=queries)

        navigation = build_workspace_navigation(
            context,
            workspace,
            "workspace_transfer_order.html",
        )

        by_id = {stage.stage_id: stage for stage in navigation.stages}
        self.assertEqual(by_id["destination-match"].status, "complete")
        self.assertEqual(by_id["transfer-order"].status, "complete")
        self.assertEqual(
            by_id["transfer-order"].href,
            f"/workspaces/{workspace.workspace_id}/transfer-order",
        )
        self.assertEqual(by_id["transfer-review"].status, "current")
        self.assertEqual(
            by_id["transfer-review"].href,
            f"/workspaces/{workspace.workspace_id}/transfer-review",
        )
        self.assertEqual(
            by_id["destination-load"].status_label,
            "Transfer approval required",
        )
        self.assertEqual(navigation.viewed_stage_id, "transfer-order")

    def test_exact_approval_completes_stage_seven(self) -> None:
        workspace, selection, schema = _stage_six_state()
        assert workspace.destination_match_plan is not None
        assert workspace.transfer_order_plan is not None
        package = TransferReviewService().build(
            workspace,
            workspace.destination_match_plan,
            workspace.transfer_order_plan,
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
        workspace = replace(
            workspace,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        context = SimpleNamespace(queries=_Queries(workspace, selection, schema))

        navigation = build_workspace_navigation(
            context,
            workspace,
            "workspace_transfer_review.html",
        )

        by_id = {stage.stage_id: stage for stage in navigation.stages}
        self.assertEqual(by_id["transfer-review"].status, "complete")
        self.assertEqual(by_id["transfer-review"].status_label, "Transfer approved")
        self.assertEqual(by_id["destination-load"].status, "current")
        self.assertEqual(
            by_id["destination-load"].status_label,
            "Run read-only preflight",
        )
        self.assertEqual(
            by_id["destination-load"].href,
            f"/workspaces/{workspace.workspace_id}/transfer-preflight",
        )
        self.assertEqual(navigation.viewed_stage_id, "transfer-review")

    def test_passed_preflight_completes_8a_without_claiming_a_load(self) -> None:
        workspace, selection, schema = _stage_six_state()
        match = workspace.destination_match_plan
        order = workspace.transfer_order_plan
        assert match is not None and order is not None
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
            _fresh(match),
            recorded_by=LOCAL_ACTOR.identity,
        )
        workspace = replace(workspace, transfer_preflight_report=report)
        context = SimpleNamespace(queries=_Queries(workspace, selection, schema))

        navigation = build_workspace_navigation(
            context,
            workspace,
            "workspace_transfer_preflight.html",
        )

        stage = next(
            item for item in navigation.stages if item.stage_id == "destination-load"
        )
        self.assertEqual(stage.status, "current")
        self.assertEqual(stage.status_label, "Ready to prepare and load")
        self.assertEqual(stage.href, f"/workspaces/{workspace.workspace_id}/transfer-load")
        self.assertEqual(
            tuple(page.page_id for page in stage.pages),
            ("transfer-preflight", "transfer-load"),
        )
        self.assertEqual(navigation.viewed_stage_id, "destination-load")


def _stage_six_state():
    now = datetime.now(UTC)
    selection = _selection(now)
    product = _model("product.template", "Product", create=2)
    match = _match_plan((product,), ())
    workspace = _workspace(match)
    schema = _source_schema(workspace, now)
    match = replace(
        match,
        source_selection_hash=selection.content_hash,
        source_schema_hash=schema.content_hash,
    )
    workspace = replace(
        _workspace(match),
        transfer_order_plan=_build(match),
    )
    return workspace, selection, schema


class _Queries:
    def __init__(self, workspace, selection, schema) -> None:
        self.workspace = workspace
        self.selection = selection
        self.schema = schema

    def get(self, _workspace_id):
        return self.workspace

    def get_odoo_model_catalog(self, _workspace_id):
        return None

    def get_odoo_schema_catalog(self, _workspace_id):
        return self.schema

    def get_current_odoo_capture_selections(self, _workspace_id):
        return ()

    def get_source_selection(self, _workspace_id):
        return self.selection


if __name__ == "__main__":
    unittest.main()
