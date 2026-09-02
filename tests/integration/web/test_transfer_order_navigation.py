from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

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


class TransferOrderNavigationTests(unittest.TestCase):
    def test_stage_six_completion_unlocks_the_next_work_boundary(self) -> None:
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
        self.assertEqual(by_id["transfer-review"].status, "locked")
        self.assertEqual(
            by_id["transfer-review"].status_label,
            "Not yet available",
        )
        self.assertEqual(navigation.viewed_stage_id, "transfer-order")


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
