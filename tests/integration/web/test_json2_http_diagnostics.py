"""Correlate bounded JSON-2 HTTP failures with requests and exported bundles."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from starlette.concurrency import run_in_threadpool

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.odoo.contracts import ConnectorTransportError, RecordRequest
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    LocalDiagnosticRecorder,
    RequestDiagnosticsMiddleware,
    create_diagnostic_bundle,
)


class Json2HttpRequestDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_survives_request_and_bundle_redaction(self):
        for version in ("19.0", "20.0"):
            with self.subTest(version=version), TemporaryDirectory() as temporary:
                directory = Path(temporary).resolve()
                recorder = LocalDiagnosticRecorder(directory)

                def transport(url, _headers, _body, _timeout, _method):
                    if url.endswith("/web/version"):
                        return 200, {"version": version}
                    return 404, {"message": "private server body"}

                connector = Json2ReadConnector(
                    Json2Config(
                        base_url="https://private-host.example.test",
                        database="private-database",
                        api_key="private-api-key",
                    ),
                    transport=transport,
                )

                async def application(scope, _receive, send):
                    scope["route"] = SimpleNamespace(
                        path="/workspaces/{workspace_id}/destination-matching"
                    )
                    try:
                        await run_in_threadpool(
                            connector.get_records,
                            [RecordRequest("x.missing.model", ("name",))],
                        )
                    except ConnectorTransportError:
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 422,
                                "headers": [],
                            }
                        )
                        await send({"type": "http.response.body", "body": b"failed"})

                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/workspaces/private-workspace/destination-matching",
                    "query_string": b"token=private-token",
                    "headers": [],
                    "state": {},
                }

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(_message):
                    return None

                await RequestDiagnosticsMiddleware(application, recorder=recorder)(
                    scope, receive, send
                )
                recorder.close()
                records = [
                    json.loads(line)
                    for line in (directory / DIAGNOSTIC_LOG_NAME)
                    .read_text()
                    .splitlines()
                ]
                failure = next(
                    item
                    for item in records
                    if item["event"] == "odoo_json2_read_failed"
                )
                request = next(
                    item for item in records if item["event"] == "request_completed"
                )
                self.assertEqual(failure["request_id"], request["request_id"])
                self.assertEqual(failure["odoo_model"], "x.missing.model")
                self.assertEqual(failure["json2_method"], "search_read")
                self.assertEqual(failure["failure_category"], "http")
                self.assertEqual(failure["odoo_status_code"], 404)
                self.assertEqual(failure["attempt_count"], 1)
                self.assertEqual(request["status_code"], 422)

                payload = create_diagnostic_bundle(
                    directory, build_contract=PROCESS_BUILD_CONTRACT
                )
                with ZipFile(BytesIO(payload)) as archive:
                    for name in ("diagnostics.jsonl", "recent-failures.json"):
                        encoded = archive.read(name).decode()
                        bundled = (
                            [json.loads(line) for line in encoded.splitlines()]
                            if name.endswith("jsonl")
                            else json.loads(encoded)
                        )
                        self.assertIn(failure, bundled)
                        for private in (
                            "private server body",
                            "private-host",
                            "private-database",
                            "private-api-key",
                            "private-workspace",
                            "private-token",
                        ):
                            self.assertNotIn(private, encoded)


if __name__ == "__main__":
    unittest.main()
