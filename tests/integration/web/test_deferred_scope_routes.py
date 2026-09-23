from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import PlainTextResponse

from impodo.domain.preflight.deferred_scope import DeferredScopeEvidenceError
from impodo.domain.shared.access import Capability, LOCAL_ACTOR
from impodo.web.routers.preflight import build_preflight_router


class DeferredScopeRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = "workspace-1"
        self.issue_id = "sha256:" + "1" * 64
        self.preview = SimpleNamespace(
            semantic_hash="sha256:" + "2" * 64,
            newly_set_aside_count=3,
            remaining_write_count=7,
        )
        self.review = SimpleNamespace(
            comparison_id="comparison-1",
            preview=self.preview,
        )
        self.context = SimpleNamespace(
            actor=LOCAL_ACTOR,
            workspace_access=MagicMock(),
            preflight=MagicMock(),
        )
        self.context.preflight.deferred_scope_review.return_value = self.review
        self.router = build_preflight_router(self.context)

    def test_preview_uses_saved_service_and_protected_read_access(self) -> None:
        with patch(
            "impodo.web.routers.preflight._render_deferred_scope",
            return_value=PlainTextResponse("preview"),
        ):
            response = asyncio.run(
                _endpoint(self.router, "preview_deferred_groups")(
                    _request(
                        "/summary/deferred-groups/preview",
                        (("csrf_token", "csrf"), ("issue_id", self.issue_id)),
                    ),
                    self.workspace_id,
                )
            )

        self.assertEqual(response.status_code, 200)
        self.context.preflight.deferred_scope_review.assert_called_once_with(
            self.workspace_id,
            selected_issue_ids=(self.issue_id,),
        )
        self.context.workspace_access.resolve.assert_called_once_with(
            self.workspace_id,
            actor=LOCAL_ACTOR,
            capability=Capability.PROTECTED_EVIDENCE_READ,
        )

    def test_changed_preview_is_not_accepted(self) -> None:
        with patch(
            "impodo.web.routers.preflight._render_deferred_scope",
            return_value=PlainTextResponse("changed", status_code=422),
        ):
            response = asyncio.run(
                _endpoint(self.router, "accept_deferred_groups")(
                    _request(
                        "/summary/deferred-groups/accept",
                        (
                            ("csrf_token", "csrf"),
                            ("issue_id", self.issue_id),
                            ("comparison_id", self.review.comparison_id),
                            ("preview_hash", "sha256:" + "9" * 64),
                        ),
                    ),
                    self.workspace_id,
                )
            )

        self.assertEqual(response.status_code, 422)
        self.context.preflight.accept_deferred_scope.assert_not_called()

    def test_broken_saved_evidence_renders_original_preview_error(self) -> None:
        self.context.preflight.deferred_scope_review.side_effect = (
            DeferredScopeEvidenceError("Saved comparison evidence is invalid")
        )
        with patch(
            "impodo.web.routers.preflight._render_deferred_scope",
            return_value=PlainTextResponse("invalid", status_code=422),
        ) as render:
            response = asyncio.run(
                _endpoint(self.router, "preview_deferred_groups")(
                    _request(
                        "/summary/deferred-groups/preview",
                        (("csrf_token", "csrf"), ("issue_id", self.issue_id)),
                    ),
                    self.workspace_id,
                )
            )

        self.assertEqual(response.status_code, 422)
        self.assertIsNone(render.call_args.kwargs["review"])
        self.assertEqual(
            render.call_args.kwargs["error"],
            "Saved comparison evidence is invalid",
        )

    def test_current_preview_acceptance_redirects_to_final_review(self) -> None:
        self.context.preflight.accept_deferred_scope.return_value = SimpleNamespace(
            preview=self.preview
        )

        response = asyncio.run(
            _endpoint(self.router, "accept_deferred_groups")(
                _request(
                    "/summary/deferred-groups/accept",
                    (
                        ("csrf_token", "csrf"),
                        ("issue_id", self.issue_id),
                        ("comparison_id", self.review.comparison_id),
                        ("preview_hash", self.preview.semantic_hash),
                    ),
                ),
                self.workspace_id,
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/workspaces/{self.workspace_id}/summary",
        )
        self.context.preflight.accept_deferred_scope.assert_called_once_with(
            self.workspace_id,
            selected_issue_ids=(self.issue_id,),
            actor=LOCAL_ACTOR,
        )
        self.context.workspace_access.resolve.assert_called_once_with(
            self.workspace_id,
            actor=LOCAL_ACTOR,
            capability=Capability.PREFLIGHT_RUN,
        )


def _endpoint(router, name: str):
    return next(route.endpoint for route in router.routes if route.name == name)


def _request(path: str, values: tuple[tuple[str, str], ...]) -> Request:
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
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "session": {"authenticated": True, "csrf_token": "csrf"},
        },
        receive,
    )


if __name__ == "__main__":
    unittest.main()
