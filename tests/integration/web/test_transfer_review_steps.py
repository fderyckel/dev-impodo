"""Keep transfer presentation steps separate from exact package approval."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.application.transfer_order_service import TransferOrderService
from impodo.domain.shared.access import CapabilityAuthorizationPolicy
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.workbench import OdooConnectionMode, WorkspaceStateService
from impodo.web.routers.transfer_review import build_transfer_review_router
from tests.application.workspace.test_destination_matching import (
    _selection,
    _source_schema,
)
from tests.application.workspace.test_transfer_order import (
    _match_plan,
    _model,
    _workspace,
)
from tests.application.workspace.test_transfer_review import _WorkspaceRepository
from tests.integration.web.test_transfer_review_routes import (
    _Access,
    _endpoint,
    _Queries,
    _request,
)
from tests.support.identity import MutableLocalRequestIdentityResolver
from tests.support.paths import REPOSITORY_ROOT


class TransferReviewStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                    FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
                ]
            ),
            autoescape=True,
        )
        self.render_patch = patch(
            "impodo.web.routers.transfer_review._render", side_effect=self.render
        )
        self.render_patch.start()
        self.addCleanup(self.render_patch.stop)

    def render(self, request, template, **values):
        return HTMLResponse(
            self.templates.get_template(template).render(
                migration_project=SimpleNamespace(display_name="Test transfer"),
                workspace_id=self.repository.workspace.workspace_id,
                csrf_token="csrf",
                **values,
            ),
            status_code=values.get("status_code", 200),
        )

    def fixture(self, major, mode=OdooConnectionMode.REMOTE):
        now = datetime.now(UTC)
        selection = _selection(now)
        match = _match_plan((_model("product.template", "Product", create=2),), ())
        workspace = _workspace(match)
        schema = replace(_source_schema(workspace, now), odoo_version=f"{major}.0")
        match = replace(
            match,
            source_selection_hash=selection.content_hash,
            source_schema_hash=schema.content_hash,
            destination_target_hash=target_identity_hash(
                connection_mode=mode.value,
                base_url=workspace.destination_odoo_base_url,
                database=workspace.destination_odoo_database,
            ),
        )
        workspace = replace(
            _workspace(match),
            destination_verified_odoo_version=f"{major}.0",
            destination_odoo_connection_mode=mode,
        )
        workspace = replace(
            workspace,
            transfer_order_plan=TransferOrderService().build(
                workspace, match, recorded_by="Data manager"
            ),
        )
        self.repository = _WorkspaceRepository(workspace)
        context = SimpleNamespace(
            identity_resolver=MutableLocalRequestIdentityResolver(),
            queries=_Queries(self.repository, selection, schema),
            workspace_states=WorkspaceStateService(
                self.repository, CapabilityAuthorizationPolicy()
            ),
            workspace_access=_Access(),
        )
        self.router = build_transfer_review_router(context)

    def get(self, query=""):
        request = _request("/transfer-review", {})
        request.scope["method"] = "GET"
        request.scope["query_string"] = query.encode("ascii")
        return asyncio.run(
            _endpoint(self.router, "transfer_review_form")(
                request, self.repository.workspace.workspace_id
            )
        )

    def post(self, action, values=None):
        return asyncio.run(
            _endpoint(self.router, f"{action}_transfer_review")(
                _request(
                    f"/transfer-review/{action}",
                    {
                        "csrf_token": "csrf",
                        "revision": str(self.repository.workspace.revision),
                        **(values or {}),
                    },
                ),
                self.repository.workspace.workspace_id,
            )
        )

    def test_contents_and_approval_keep_exact_package_and_local_navigation(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.fixture(major, mode)
                    first = self.get().body.decode()
                    self.assertIn("Review the Odoo transfer contents", first)
                    self.assertIn('id="model-policies"', first)
                    self.assertNotIn('name="confirmation"', first)
                    unavailable = self.get("step=approve").body.decode()
                    self.assertIn("Build the current review package first", unavailable)
                    self.assertNotIn('name="confirmation"', unavailable)
                    self.assertEqual(self.repository.events, [])

                    built = self.post("build")
                    self.assertEqual(built.status_code, 303)
                    self.assertIn(
                        "?step=contents#review-package", built.headers["location"]
                    )
                    package = self.repository.workspace.transfer_review_package
                    contents = self.get("step=contents").body.decode()
                    self.assertIn("Fields and actions by Odoo model", contents)
                    self.assertIn("Control totals reconcile", contents)
                    self.assertIn("Continue to approval", contents)
                    self.assertNotIn('name="confirmation"', contents)
                    approval = self.get("step=approve").body.decode()
                    self.assertIn("Approve the exact Odoo transfer package", approval)
                    self.assertIn(
                        'name="confirmation" value="approve" required', approval
                    )
                    self.assertIn("Approval note (optional)", approval)
                    self.assertNotIn('id="model-policies"', approval)
                    self.assertIs(
                        self.repository.workspace.transfer_review_package, package
                    )
                    self.assertIsNone(
                        self.repository.workspace.transfer_review_approval
                    )
                    self.assertEqual(
                        self.repository.events, ["WORKSPACE_TRANSFER_REVIEW_FROZEN"]
                    )

                    approved = self.post("approve", {"confirmation": "approve"})
                    self.assertEqual(approved.status_code, 303)
                    self.assertIn(
                        "?step=approve#review-package", approved.headers["location"]
                    )
                    completed = self.get("step=approve").body.decode()
                    self.assertIn("Stage 5 complete", completed)
                    self.assertIn("Continue to destination preflight", completed)
                    self.assertNotIn('name="confirmation"', completed)
                    self.assertIs(
                        self.repository.workspace.transfer_review_package, package
                    )

    def test_failed_forms_keep_the_owning_step_and_do_not_approve(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self.fixture(major)
                dataset = (
                    self.repository.workspace.destination_match_plan.model_matches[0]
                )
                failed_build = self.post(
                    "build", {f"policy_{dataset.dataset_id}": "reuse_only"}
                )
                self.assertEqual(failed_build.status_code, 422)
                self.assertIn(
                    "Review the Odoo transfer contents", failed_build.body.decode()
                )
                self.assertIn('id="model-policies"', failed_build.body.decode())
                self.assertEqual(self.repository.events, [])
                self.post("build")
                failed_approval = self.post("approve")
                self.assertEqual(failed_approval.status_code, 422)
                self.assertIn(
                    "Approve the exact Odoo transfer package",
                    failed_approval.body.decode(),
                )
                self.assertIn('name="confirmation"', failed_approval.body.decode())
                self.assertNotIn('id="model-policies"', failed_approval.body.decode())
                self.assertIsNone(self.repository.workspace.transfer_review_approval)

    def test_unknown_step_and_legacy_links_open_contents_without_mutation(self):
        self.fixture(20)
        self.post("build")
        before = self.repository.workspace
        for query in ("", "step=unknown", "step=contents"):
            with self.subTest(query=query):
                html = self.get(query).body.decode()
                self.assertIn('id="review-package"', html)
                self.assertIn("Review the Odoo transfer contents", html)
                self.assertNotIn('name="confirmation"', html)
                self.assertIs(self.repository.workspace, before)

    def test_stale_package_cannot_show_or_receive_an_approval(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self.fixture(major)
                self.post("build")
                order = self.repository.workspace.transfer_order_plan
                self.repository.workspace = replace(
                    self.repository.workspace,
                    transfer_order_plan=replace(
                        order, recorded_at=order.recorded_at + timedelta(seconds=1)
                    ),
                )
                html = self.get("step=approve").body.decode()
                self.assertIn("The saved review package is no longer current", html)
                self.assertIn("Build the current review package first", html)
                self.assertNotIn('name="confirmation"', html)
                rejected = self.post("approve", {"confirmation": "approve"})
                self.assertEqual(rejected.status_code, 422)
                self.assertIsNone(self.repository.workspace.transfer_review_approval)


if __name__ == "__main__":
    unittest.main()
