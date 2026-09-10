"""Browser evidence for mapping-formula authoring validation."""

from __future__ import annotations

import asyncio

from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
)


class MappingFormulaAuthoringBrowserTests(ProjectSetupBrowserTestCase):
    def test_client_disconnect_during_formula_body_is_expected(self) -> None:
        workspace_id, _dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )
        context = self.app.state.context
        working_draft_before = (
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id)
        )
        session_cookie = self.client.cookies.get("impodo_session")
        self.assertIsNotNone(session_cookie)
        received = 0
        sent: list[dict] = []

        async def receive():
            nonlocal received
            received += 1
            if received == 1:
                return {
                    "type": "http.request",
                    "body": b'{"entries":[',
                    "more_body": True,
                }
            return {"type": "http.disconnect"}

        async def send(message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/workspaces/{workspace_id}/mapping/formula-validation",
            "raw_path": (
                f"/workspaces/{workspace_id}/mapping/formula-validation".encode()
            ),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"origin", b"http://testserver"),
                (b"sec-fetch-site", b"same-origin"),
                (b"content-type", b"application/json"),
                (
                    b"cookie",
                    f"impodo_session={session_cookie}".encode("ascii"),
                ),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
        }

        asyncio.run(self.app(scope, receive, send))

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        self.assertEqual(response_start["status"], 499)
        self.assertIs(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            working_draft_before,
        )

    def test_formula_authoring_check_is_read_only_and_formula_free(self) -> None:
        workspace_id, dataset, _business_key = self._mapping_ready_workspace(
            scalar_field_count=1
        )
        context = self.app.state.context
        endpoint = f"/workspaces/{workspace_id}/mapping/formula-validation"

        valid = self.client.post(
            endpoint,
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["dataset_id", dataset.dataset_id],
                    ["formula", "column_1 == column_2"],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        invalid_formula = 'value 1= "UNI"'
        invalid = self.client.post(
            endpoint,
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["dataset_id", dataset.dataset_id],
                    ["formula", invalid_formula],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertTrue(valid.json()["valid"])
        self.assertIsNone(valid.json()["issue"])
        self.assertEqual(invalid.status_code, 200, invalid.text)
        self.assertFalse(invalid.json()["valid"])
        issue = invalid.json()["issue"]
        self.assertEqual(issue["code"], "MAPPING_FORMULA_INVALID")
        self.assertEqual(issue["severity"], "error")
        self.assertGreater(issue["position"], 0)
        self.assertNotIn(invalid_formula, invalid.text)
        self.assertEqual(invalid.headers["cache-control"], "no-store")
        self.assertIsNone(
            context.mapping_workspace.mappings.get_mapping_working_draft(
                workspace_id
            )
        )
        self.assertIsNone(
            context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
        )

        wrong_csrf = self.client.post(
            endpoint,
            json={
                "entries": [
                    ["csrf_token", "wrong"],
                    ["dataset_id", dataset.dataset_id],
                    ["formula", "value"],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": "wrong"},
        )
        stale_dataset = self.client.post(
            endpoint,
            json={
                "entries": [
                    ["csrf_token", self.csrf],
                    ["dataset_id", "not-current"],
                    ["formula", "value"],
                ]
            },
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

        self.assertEqual(wrong_csrf.status_code, 403, wrong_csrf.text)
        self.assertEqual(stale_dataset.status_code, 422, stale_dataset.text)


if __name__ == "__main__":
    import unittest

    unittest.main()
