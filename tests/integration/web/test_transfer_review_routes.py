from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode
from uuid import uuid4

from starlette.requests import Request

from impodo.domain.shared.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.domain.workspace.workbench import WorkspaceStateService
from impodo.web.routers.transfer_review import build_transfer_review_router
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
from tests.application.workspace.test_transfer_review import _WorkspaceRepository


class TransferReviewRouteTests(unittest.TestCase):
    def test_build_then_approve_remain_local_and_revision_bound(self) -> None:
        now = datetime.now(UTC)
        selection = _selection(now)
        match = _match_plan(
            (_model("product.template", "Product", create=2),),
            (),
        )
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
        repository = _WorkspaceRepository(workspace)
        context = SimpleNamespace(
            actor=LOCAL_ACTOR,
            queries=_Queries(repository, selection, schema),
            workspace_states=WorkspaceStateService(
                repository,
                CapabilityAuthorizationPolicy(),
            ),
            workspace_access=_Access(),
        )
        router = build_transfer_review_router(context)

        built = asyncio.run(
            _endpoint(router, "build_transfer_review")(
                _request(
                    "/transfer-review/build",
                    {
                        "csrf_token": "csrf",
                        "revision": str(workspace.revision),
                    },
                ),
                workspace.workspace_id,
            )
        )

        self.assertEqual(built.status_code, 303)
        reviewed = repository.workspace
        self.assertIsNotNone(reviewed.transfer_review_package)
        self.assertIsNone(reviewed.transfer_review_approval)

        approved = asyncio.run(
            _endpoint(router, "approve_transfer_review")(
                _request(
                    "/transfer-review/approve",
                    {
                        "csrf_token": "csrf",
                        "revision": str(reviewed.revision),
                        "confirmation": "approve",
                        "reason": "Reviewed counts and write scope.",
                    },
                ),
                workspace.workspace_id,
            )
        )

        self.assertEqual(approved.status_code, 303)
        final = repository.workspace
        self.assertIsNotNone(final.transfer_review_approval)
        self.assertTrue(
            final.transfer_review_approved(
                source_selection_hash=selection.content_hash,
                source_schema_hash=schema.content_hash,
            )
        )
        self.assertEqual(
            repository.events,
            [
                "WORKSPACE_TRANSFER_REVIEW_FROZEN",
                "WORKSPACE_TRANSFER_REVIEW_APPROVED",
            ],
        )


class _Queries:
    def __init__(self, repository, selection, schema) -> None:
        self.repository = repository
        self.selection = selection
        self.schema = schema

    def get(self, _workspace_id):
        return self.repository.workspace

    def get_source_selection(self, _workspace_id):
        return self.selection

    def get_odoo_schema_catalog(self, _workspace_id):
        return self.schema


class _Access:
    def __init__(self) -> None:
        self.value = SimpleNamespace(
            migration_run_id=str(uuid4()),
            data_version_id=str(uuid4()),
        )

    def resolve(self, *_args, **_kwargs):
        return self.value


def _endpoint(router, name):
    return next(route.endpoint for route in router.routes if route.name == name)


def _request(path: str, values: dict[str, str]) -> Request:
    body = urlencode(values).encode("ascii")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": (
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode("ascii")),
            ),
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "session": {
                "authenticated": True,
                "csrf_token": "csrf",
            },
        },
        receive,
    )


if __name__ == "__main__":
    unittest.main()
