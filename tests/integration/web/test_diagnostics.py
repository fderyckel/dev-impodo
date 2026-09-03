"""Privacy and lifecycle evidence for local browser diagnostics."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.cookiejar import CookieJar
from io import BytesIO
import json
from pathlib import Path
import shutil
import socket
from time import perf_counter, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient
import h11
from uvicorn.protocols.http.h11_impl import H11Protocol

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.app import create_local_app
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    LocalDiagnosticRecorder,
    REQUEST_ID_HEADER,
    RequestDiagnosticsMiddleware,
    create_diagnostic_bundle,
    diagnostic_directory,
    install_asyncio_exception_diagnostics,
    monitor_event_loop,
    parse_server_timing,
)
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.web.server_supervisor import (
    ClosedConnectionSafeH11Protocol,
    ServerChildSettings,
    ServerSupervisionResult,
    bind_loopback_listener,
    listener_is_owned_loopback,
    spawn_server_process,
    supervise_server,
)
from tests.support.browser_scenarios import _csrf
from tests.support.paths import REPOSITORY_ROOT


@contextmanager
def _diagnostic_test_directory(prefix: str):
    path = REPOSITORY_ROOT / ".tmp" / f"{prefix}-{uuid4()}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


class LocalDiagnosticRecorderTests(unittest.TestCase):
    def test_diagnostic_directory_stays_outside_the_project_database_root(
        self,
    ) -> None:
        project_root = REPOSITORY_ROOT / ".tmp" / "example-app" / "projects"
        self.assertEqual(
            diagnostic_directory(project_root),
            project_root.parent / "diagnostics",
        )

    def test_rotating_records_keep_only_allowlisted_operational_fields(self) -> None:
        with _diagnostic_test_directory("impodo-diagnostics") as directory:
            recorder = LocalDiagnosticRecorder(
                directory,
                max_bytes=500,
                backup_count=2,
            )
            recorder.record_lifecycle(
                "launcher_starting",
                port=60572,
                development_mode=False,
            )
            for index in range(12):
                recorder.record_request(
                    request_id=f"request-{index}",
                    method="POST",
                    route_template="/workspaces/{workspace_id}/mapping/save",
                    status_code=200,
                    duration_ms=25.5,
                    working_draft_version=index,
                    server_timings_ms={
                        "workspace_read": 5.0,
                        "render": float("inf"),
                        "total": float("nan"),
                        "not_allowlisted": 999.0,
                    },
                )
            recorder.record_lifecycle(
                "launcher_stopped",
                reason="server_returned",
                exit_code=0,
            )
            recorder.close()

            paths = sorted(Path(directory).glob(f"{DIAGNOSTIC_LOG_NAME}*"))
            self.assertGreaterEqual(len(paths), 2)
            self.assertLessEqual(len(paths), 3)
            records = [
                json.loads(line)
                for log_path in paths
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(records)
        self.assertNotIn("Infinity", "".join(map(json.dumps, records)))
        self.assertNotIn("NaN", "".join(map(json.dumps, records)))
        for record in records:
            self.assertEqual(record["schema_version"], 1)
            self.assertNotIn("not_allowlisted", json.dumps(record))
            self.assertNotIn("body", record)
            self.assertNotIn("headers", record)
            self.assertNotIn("query", record)

    def test_windows_sharing_violation_uses_bounded_fallback_log(self) -> None:
        with _diagnostic_test_directory("impodo-diagnostics-sharing") as directory:
            recorder = LocalDiagnosticRecorder(
                directory,
                max_bytes=1,
                backup_count=2,
            )
            recorder.record_lifecycle("launcher_starting")
            sharing_violation = PermissionError("diagnostic log is in use")
            sharing_violation.winerror = 32
            with (
                patch.object(
                    recorder._handler,
                    "doRollover",
                    side_effect=sharing_violation,
                ),
                patch(
                    "impodo.web.diagnostics._is_windows_sharing_violation",
                    return_value=True,
                ),
            ):
                recorder.record_lifecycle("server_process_started", port=60572)
            recorder.close()

            fallback = directory / f"{DIAGNOSTIC_LOG_NAME}.concurrent"
            record = json.loads(fallback.read_text(encoding="utf-8"))

        self.assertEqual(record["event"], "server_process_started")
        self.assertEqual(record["port"], 60572)

    def test_server_timing_parser_ignores_unknown_or_malformed_metrics(self) -> None:
        self.assertEqual(
            parse_server_timing(
                "workspace_read;dur=12.5, secret;dur=999, "
                "render;desc=template;dur=4.0, total;dur=not-a-number"
            ),
            {
                "workspace_read": 12.5,
                "render": 4.0,
            },
        )

    def test_operation_stage_records_only_bounded_support_fields(self) -> None:
        with _diagnostic_test_directory("impodo-operation-stage") as directory:
            recorder = LocalDiagnosticRecorder(
                directory,
                slow_request_seconds=0.01,
            )
            recorder.record_operation_stage(
                "odoo_load",
                "correction_origin",
                duration_ms=25.5,
                outcome="warning",
                reason="CORRECTION_ORIGIN_PREPARED_MISSING",
                exception_class="CorrectionOriginError",
            )
            recorder.close()
            record = json.loads(
                (directory / DIAGNOSTIC_LOG_NAME).read_text(encoding="utf-8")
            )

        self.assertEqual(record["event"], "operation_stage_completed")
        self.assertEqual(record["operation"], "odoo_load")
        self.assertEqual(record["stage"], "correction_origin")
        self.assertEqual(record["outcome"], "warning")
        self.assertEqual(record["reason"], "CORRECTION_ORIGIN_PREPARED_MISSING")
        self.assertTrue(record["slow"])
        self.assertNotIn("workspace_id", record)

    def test_bundle_resanitizes_logs_and_contains_only_bounded_support_data(
        self,
    ) -> None:
        with _diagnostic_test_directory("impodo-bundle") as directory:
            recorder = LocalDiagnosticRecorder(directory)
            recorder.record_request(
                request_id="safe-request",
                method="POST",
                route_template="/workspaces/{workspace_id}/mapping/save",
                status_code=503,
                duration_ms=2500,
                exception_class="RuntimeError",
            )
            recorder.close()
            with (directory / DIAGNOSTIC_LOG_NAME).open(
                "a",
                encoding="utf-8",
            ) as stream:
                stream.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "recorded_at": "2026-09-01T12:00:00Z",
                            "event": "request_completed",
                            "route_template": "/private/customer/row",
                            "formula": "private formula contents",
                            "headers": {"authorization": "private token"},
                        }
                    )
                    + "\n"
                )

            payload = create_diagnostic_bundle(
                directory,
                build_contract=PROCESS_BUILD_CONTRACT,
            )
            with ZipFile(BytesIO(payload)) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "diagnostics.jsonl",
                        "manifest.json",
                        "slow-requests.json",
                    },
                )
                encoded = "\n".join(
                    archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                )
                manifest = json.loads(
                    archive.read("manifest.json").decode("utf-8")
                )

        self.assertEqual(manifest["diagnostic_bundle_schema_version"], 1)
        self.assertEqual(
            manifest["application"]["workspace_schema_version"],
            PROCESS_BUILD_CONTRACT.workspace_schema_version,
        )
        self.assertNotIn("private formula", encoded)
        self.assertNotIn("private token", encoded)
        self.assertNotIn("private/customer", encoded)
        self.assertNotIn("authorization", encoded.casefold())


class RequestDiagnosticsMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_record_uses_route_template_and_redacts_request_data(
        self,
    ) -> None:
        with _diagnostic_test_directory("impodo-diagnostics") as directory:
            recorder = LocalDiagnosticRecorder(directory)
            sent: list[dict] = []

            async def application(scope, _receive, send) -> None:
                scope["route"] = SimpleNamespace(
                    path="/workspaces/{workspace_id}/mapping"
                )
                scope["state"]["diagnostic_working_draft_version"] = 4
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (
                                b"server-timing",
                                b"workspace_read;dur=8.0, render;dur=3.5, secret;dur=9",
                            )
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b"ok"})

            middleware = RequestDiagnosticsMiddleware(
                application,
                recorder=recorder,
            )
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/workspaces/private-workspace-id/mapping",
                "query_string": b"field_query=private-formula-value",
                "headers": [],
                "state": {},
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message) -> None:
                sent.append(message)

            await middleware(scope, receive, send)
            recorder.close()
            encoded = (directory / DIAGNOSTIC_LOG_NAME).read_text(
                encoding="utf-8"
            )
            record = json.loads(encoded)

        response_headers = dict(sent[0]["headers"])
        response_request_id = response_headers[REQUEST_ID_HEADER.lower().encode()]
        self.assertEqual(record["request_id"], response_request_id.decode())
        self.assertEqual(
            record["route_template"],
            "/workspaces/{workspace_id}/mapping",
        )
        self.assertEqual(record["working_draft_version"], 4)
        self.assertEqual(
            record["server_timings_ms"],
            {"render": 3.5, "workspace_read": 8.0},
        )
        self.assertNotIn("private-workspace-id", encoded)
        self.assertNotIn("private-formula-value", encoded)
        self.assertNotIn("secret", encoded)

    async def test_unhandled_exception_records_only_its_class(self) -> None:
        with _diagnostic_test_directory("impodo-diagnostics") as directory:
            recorder = LocalDiagnosticRecorder(directory)

            async def application(scope, _receive, _send) -> None:
                scope["route"] = SimpleNamespace(path="/safe/{item_id}")
                raise RuntimeError("private source value must not be logged")

            middleware = RequestDiagnosticsMiddleware(
                application,
                recorder=recorder,
            )
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/safe/private-source-value",
                "query_string": b"token=private-token",
                "headers": [],
                "state": {},
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(_message) -> None:
                return None

            with self.assertRaisesRegex(RuntimeError, "private source value"):
                await middleware(scope, receive, send)
            recorder.close()
            encoded = (directory / DIAGNOSTIC_LOG_NAME).read_text(
                encoding="utf-8"
            )
            record = json.loads(encoded)

        self.assertEqual(record["status_code"], 500)
        self.assertEqual(record["exception_class"], "RuntimeError")
        self.assertNotIn("private source value", encoded)
        self.assertNotIn("private-source-value", encoded)
        self.assertNotIn("private-token", encoded)


class EventLoopDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_windows_proactor_reset_is_downgraded(self) -> None:
        with _diagnostic_test_directory("impodo-proactor-reset") as directory:
            recorder = LocalDiagnosticRecorder(directory)
            prior_handler = Mock()
            loop = Mock()
            loop.get_exception_handler.return_value = prior_handler
            previous = install_asyncio_exception_diagnostics(
                loop,
                recorder,
                platform_name="win32",
            )
            handler = loop.set_exception_handler.call_args.args[0]
            reset = ConnectionResetError("client reset")
            reset.winerror = 10054

            handler(
                loop,
                {
                    "message": (
                        "Exception in callback "
                        "_ProactorBasePipeTransport._call_connection_lost()"
                    ),
                    "exception": reset,
                },
            )
            recorder.close()
            record = json.loads(
                (directory / DIAGNOSTIC_LOG_NAME).read_text(encoding="utf-8")
            )

        self.assertIs(previous, prior_handler)
        prior_handler.assert_not_called()
        self.assertEqual(record["event"], "client_connection_reset")
        self.assertEqual(record["reason"], "windows_proactor_cleanup")

    async def test_macos_and_other_asyncio_errors_keep_the_prior_handler(self) -> None:
        prior_handler = Mock()
        loop = Mock()
        loop.get_exception_handler.return_value = prior_handler
        install_asyncio_exception_diagnostics(
            loop,
            None,
            platform_name="darwin",
        )
        handler = loop.set_exception_handler.call_args.args[0]
        reset = ConnectionResetError("client reset")
        reset.winerror = 10054
        context = {
            "message": (
                "Exception in callback "
                "_ProactorBasePipeTransport._call_connection_lost()"
            ),
            "exception": reset,
        }

        handler(loop, context)

        prior_handler.assert_called_once_with(loop, context)

    async def test_event_loop_delay_is_recorded_without_stack_content(self) -> None:
        with _diagnostic_test_directory("impodo-event-loop") as directory:
            recorder = LocalDiagnosticRecorder(
                directory,
                slow_request_seconds=0.01,
            )
            monitor = asyncio.create_task(
                monitor_event_loop(
                    recorder,
                    interval_seconds=0.01,
                    delay_threshold_seconds=0.01,
                )
            )
            await asyncio.sleep(0.03)
            sleep(0.04)
            await asyncio.sleep(0.03)
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            recorder.close()
            encoded = (directory / DIAGNOSTIC_LOG_NAME).read_text(
                encoding="utf-8"
            )
            records = [json.loads(line) for line in encoded.splitlines()]

        delayed = next(
            record
            for record in records
            if record["event"] == "event_loop_delay_observed"
        )
        self.assertGreaterEqual(delayed["duration_ms"], 10)
        self.assertTrue(delayed["slow"])
        self.assertNotIn("stack", encoded.casefold())


class ApplicationLifecycleDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_records_normal_startup_and_shutdown(self) -> None:
        with _diagnostic_test_directory("impodo-application") as directory:
            root = directory / "projects"
            recorder = LocalDiagnosticRecorder(directory / "diagnostics")
            app = create_local_app(
                root,
                secret_store=MemorySecretStore(),
                preparation_jobs_enabled=False,
                odoo_capture_jobs_enabled=False,
                load_jobs_enabled=False,
                diagnostic_recorder=recorder,
            )

            loop = asyncio.get_running_loop()
            previous_exception_handler = loop.get_exception_handler()
            async with app.router.lifespan_context(app):
                self.assertIsNot(
                    loop.get_exception_handler(),
                    previous_exception_handler,
                )
            self.assertIs(
                loop.get_exception_handler(),
                previous_exception_handler,
            )

            recorder.close()
            encoded = (directory / "diagnostics" / DIAGNOSTIC_LOG_NAME).read_text(
                encoding="utf-8"
            )
            records = [json.loads(line) for line in encoded.splitlines()]

        self.assertEqual(
            [record["event"] for record in records],
            [
                "application_started",
                "application_stopping",
                "application_stopped",
            ],
        )
        self.assertIn("build_version", records[0])


class ApplicationRequestDiagnosticTests(unittest.TestCase):
    def test_registered_route_is_recorded_without_the_raw_launch_url(self) -> None:
        with _diagnostic_test_directory("impodo-request") as directory:
            recorder = LocalDiagnosticRecorder(directory / "diagnostics")
            app = create_local_app(
                directory / "projects",
                launch_token="private-launch-token",
                session_secret="private-session-secret",
                secret_store=MemorySecretStore(),
                preparation_jobs_enabled=False,
                odoo_capture_jobs_enabled=False,
                load_jobs_enabled=False,
                diagnostic_recorder=recorder,
            )

            with TestClient(app) as client:
                response = client.get(
                    "/launch?token=private-launch-token",
                    follow_redirects=False,
                )

            recorder.close()
            encoded = (
                directory / "diagnostics" / DIAGNOSTIC_LOG_NAME
            ).read_text(encoding="utf-8")
            records = [json.loads(line) for line in encoded.splitlines()]

        request = next(
            record for record in records if record["event"] == "request_completed"
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(request["route_template"], "/launch")
        self.assertEqual(
            request["request_id"],
            response.headers[REQUEST_ID_HEADER],
        )
        self.assertNotIn("private-launch-token", encoded)
        self.assertNotIn("private-session-secret", encoded)

    def test_health_and_bundle_require_the_authenticated_local_session(
        self,
    ) -> None:
        with _diagnostic_test_directory("impodo-recovery-routes") as directory:
            recorder = LocalDiagnosticRecorder(directory / "diagnostics")
            app = create_local_app(
                directory / "projects",
                launch_token="recovery-launch-token",
                session_secret="recovery-session-secret",
                secret_store=MemorySecretStore(),
                preparation_jobs_enabled=False,
                odoo_capture_jobs_enabled=False,
                load_jobs_enabled=False,
                diagnostic_recorder=recorder,
            )

            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 401)
                self.assertEqual(
                    client.post(
                        "/diagnostics/bundle",
                        data={"csrf_token": "not-authenticated"},
                        headers={"Origin": "http://testserver"},
                    ).status_code,
                    401,
                )
                client.get(
                    "/launch?token=recovery-launch-token",
                    follow_redirects=False,
                )
                projects = client.get("/projects")
                self.assertIn("/static/server-recovery.js", projects.text)
                self.assertIn("Impodo is not responding", projects.text)
                self.assertIn("Create diagnostic bundle", projects.text)
                health = client.get("/health")
                csrf_token = _csrf(projects.text)
                self.assertEqual(
                    client.post(
                        "/diagnostics/bundle",
                        data={"csrf_token": csrf_token},
                        headers={"Origin": "http://attacker.example"},
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.post(
                        "/diagnostics/bundle",
                        data={
                            "csrf_token": csrf_token,
                            "formula": "must not be accepted",
                        },
                        headers={"Origin": "http://testserver"},
                    ).status_code,
                    422,
                )
                bundle = client.post(
                    "/diagnostics/bundle",
                    data={"csrf_token": csrf_token},
                    headers={"Origin": "http://testserver"},
                )

            recorder.close()

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(bundle.status_code, 200)
        self.assertEqual(bundle.headers["content-type"], "application/zip")
        self.assertIn(
            "impodo-diagnostic-bundle.zip",
            bundle.headers["content-disposition"],
        )
        with ZipFile(BytesIO(bundle.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertGreaterEqual(manifest["record_count"], 1)


class _FakeServerChild:
    def __init__(self, *, process_id: int, exit_code: int) -> None:
        self.pid = process_id
        self.exitcode = exit_code
        self.started = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.joined = True

    def is_alive(self) -> bool:
        return self.started and not self.joined

    def terminate(self) -> None:
        self.exitcode = -15
        self.joined = True


class ClosedConnectionSafeH11ProtocolTests(unittest.TestCase):
    def test_late_bytes_are_ignored_after_h11_connection_closes(self) -> None:
        protocol = object.__new__(ClosedConnectionSafeH11Protocol)
        protocol.conn = h11.Connection(h11.SERVER)
        protocol.conn.send(h11.ConnectionClosed())
        protocol.transport = Mock()
        keepalive = Mock()
        protocol.timeout_keep_alive_task = keepalive

        with patch.object(H11Protocol, "data_received", autospec=True) as receive:
            protocol.data_received(b"GET /health HTTP/1.1\r\n\r\n")

        receive.assert_not_called()
        keepalive.cancel.assert_called_once_with()
        self.assertIsNone(protocol.timeout_keep_alive_task)
        protocol.transport.close.assert_called_once_with()

    def test_live_connection_data_uses_uvicorn_h11_handling(self) -> None:
        protocol = object.__new__(ClosedConnectionSafeH11Protocol)
        protocol.conn = h11.Connection(h11.SERVER)
        protocol.transport = Mock()
        protocol.timeout_keep_alive_task = None
        data = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"

        with patch.object(H11Protocol, "data_received", autospec=True) as receive:
            protocol.data_received(data)

        receive.assert_called_once_with(protocol, data)
        protocol.transport.close.assert_not_called()


class ServerSupervisorTests(unittest.TestCase):
    def test_spawned_server_reuses_listener_and_session_after_restart(self) -> None:
        with _diagnostic_test_directory("impodo-spawn-restart") as directory:
            listener = bind_loopback_listener(0)
            self.assertIsNotNone(listener)
            assert listener is not None
            port = listener.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            settings = ServerChildSettings(
                project_root=directory / "projects",
                expected_host=f"127.0.0.1:{port}",
                launch_token="spawn-launch-token",
                session_secret="same-session-secret-across-restart",
                diagnostics_root=directory / "diagnostics",
                development_mode=True,
            )
            children = []
            opener = build_opener(HTTPCookieProcessor(CookieJar()))

            def wait_for(path: str, status_code: int):
                deadline = perf_counter() + 30
                last_error = None
                while perf_counter() < deadline:
                    try:
                        with opener.open(origin + path, timeout=2) as response:
                            if response.status == status_code:
                                return response.read(), response.status
                        last_error = AssertionError(response.status)
                    except (HTTPError, URLError, TimeoutError) as error:
                        last_error = error
                    sleep(0.05)
                raise AssertionError(
                    f"Server did not return {status_code}: {last_error}"
                )

            def post(path: str, csrf_token: str):
                request = Request(
                    origin + path,
                    data=urlencode({"csrf_token": csrf_token}).encode("ascii"),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": origin,
                    },
                    method="POST",
                )
                with opener.open(request, timeout=10) as response:
                    return response.status, response.read()

            try:
                first = spawn_server_process(listener, settings, 0)
                children.append(first)
                first.start()
                listener.close()
                wait_for("/launch?token=spawn-launch-token", 200)
                projects, _status = wait_for("/projects", 200)
                stopped_status, _body = post(
                    "/quit",
                    _csrf(projects.decode("utf-8")),
                )
                self.assertEqual(stopped_status, 200)
                first.join(timeout=30)
                self.assertFalse(first.is_alive())
                self.assertEqual(first.exitcode, 0)

                listener = bind_loopback_listener(port)
                self.assertIsNotNone(listener)
                assert listener is not None
                second = spawn_server_process(listener, settings, 1)
                children.append(second)
                second.start()
                listener.close()
                health, _status = wait_for("/health", 200)
                self.assertEqual(json.loads(health), {"status": "ok"})
                stopped_again_status, _body = post(
                    "/quit",
                    _csrf(projects.decode("utf-8")),
                )
                self.assertEqual(stopped_again_status, 200)
                second.join(timeout=30)
                self.assertFalse(second.is_alive())
                self.assertEqual(second.exitcode, 0)
            finally:
                for child in children:
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=5)
                listener.close()

    def test_unexpected_exit_restarts_once_on_the_same_listener(self) -> None:
        with _diagnostic_test_directory("impodo-supervisor") as directory:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = listener.getsockname()[1]
            settings = ServerChildSettings(
                project_root=directory / "projects",
                expected_host=f"127.0.0.1:{port}",
                launch_token="private-launch-token",
                session_secret="private-session-secret",
                diagnostics_root=directory / "diagnostics",
                development_mode=False,
            )
            exits = iter((7, 0))
            attempts: list[tuple[int, int]] = []
            opened: list[str] = []

            def factory(active_listener, _settings, attempt):
                attempts.append((active_listener.getsockname()[1], attempt))
                return _FakeServerChild(
                    process_id=4100 + attempt,
                    exit_code=next(exits),
                )

            self.assertTrue(listener_is_owned_loopback(listener, port))
            result = supervise_server(
                listener,
                settings,
                process_factory=factory,
                browser_opener=lambda url, **_kwargs: opened.append(url) or True,
            )
            listener.close()
            encoded = (
                directory / "diagnostics" / DIAGNOSTIC_LOG_NAME
            ).read_text(encoding="utf-8")
            records = [json.loads(line) for line in encoded.splitlines()]

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.restart_attempts, 1)
        self.assertEqual(attempts, [(port, 0), (port, 1)])
        self.assertEqual(len(opened), 1)
        self.assertNotIn("private-launch-token", encoded)
        self.assertEqual(
            [record["event"] for record in records],
            [
                "server_process_exited",
                "server_restart_attempted",
                "server_process_exited",
            ],
        )
        self.assertEqual(records[1]["restart_attempt"], 1)

    def test_second_unexpected_exit_opens_the_restart_circuit(self) -> None:
        with _diagnostic_test_directory("impodo-supervisor-circuit") as directory:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = listener.getsockname()[1]
            settings = ServerChildSettings(
                project_root=directory / "projects",
                expected_host=f"127.0.0.1:{port}",
                launch_token="launch-token",
                session_secret="session-secret",
                diagnostics_root=directory / "diagnostics",
                development_mode=False,
            )
            exits = iter((8, 9))

            result = supervise_server(
                listener,
                settings,
                process_factory=lambda _listener, _settings, attempt: (
                    _FakeServerChild(
                        process_id=5100 + attempt,
                        exit_code=next(exits),
                    )
                ),
                browser_opener=lambda _url, **_kwargs: True,
            )
            listener.close()
            encoded = (
                directory / "diagnostics" / DIAGNOSTIC_LOG_NAME
            ).read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 9)
        self.assertEqual(result.reason, "restart_circuit_open")
        self.assertIn("server_restart_circuit_open", encoded)


class LauncherDiagnosticTests(unittest.TestCase):
    def test_launcher_records_normal_start_and_stop_without_the_launch_token(
        self,
    ) -> None:
        with _diagnostic_test_directory("impodo-launcher") as directory:
            root = directory / "projects"
            root.mkdir()
            root_security = SimpleNamespace(root=root, development_mode=False)
            supervised = ServerSupervisionResult(
                exit_code=0,
                reason="normal_shutdown",
                restart_attempts=0,
            )
            with (
                patch(
                    "impodo.web.launcher.default_project_root",
                    return_value=root,
                ),
                patch(
                    "impodo.web.launcher.prepare_project_root",
                    return_value=root_security,
                ) as prepare_root,
                patch(
                    "impodo.web.launcher.supervise_server",
                    return_value=supervised,
                ) as supervise,
            ):
                from impodo.web.launcher import main

                result = main()

            encoded = (
                directory / "diagnostics" / DIAGNOSTIC_LOG_NAME
            ).read_text(encoding="utf-8")
            events = [
                json.loads(line)["event"] for line in encoded.splitlines()
            ]

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            ["launcher_starting", "server_bound", "launcher_stopped"],
        )
        self.assertEqual(prepare_root.call_count, 2)
        self.assertEqual(
            prepare_root.call_args_list[1].args[0],
            directory / "diagnostics",
        )
        settings = supervise.call_args.args[1]
        self.assertEqual(settings.diagnostics_root, directory / "diagnostics")
        self.assertNotIn("token", encoded.casefold())

    def test_launcher_records_exception_class_without_exception_message(self) -> None:
        with _diagnostic_test_directory("impodo-launcher-error") as directory:
            root = directory / "projects"
            root.mkdir()
            root_security = SimpleNamespace(root=root, development_mode=False)
            with (
                patch(
                    "impodo.web.launcher.default_project_root",
                    return_value=root,
                ),
                patch(
                    "impodo.web.launcher.prepare_project_root",
                    return_value=root_security,
                ),
                patch(
                    "impodo.web.launcher.supervise_server",
                    side_effect=RuntimeError("private formula and source value"),
                ),
            ):
                from impodo.web.launcher import main

                with self.assertRaisesRegex(RuntimeError, "private formula"):
                    main()

            encoded = (
                directory / "diagnostics" / DIAGNOSTIC_LOG_NAME
            ).read_text(encoding="utf-8")
            records = [json.loads(line) for line in encoded.splitlines()]

        failed = next(
            record for record in records if record["event"] == "launcher_failed"
        )
        self.assertEqual(failed["exception_class"], "RuntimeError")
        self.assertNotIn("private formula", encoded)
        self.assertNotIn("source value", encoded)


if __name__ == "__main__":
    unittest.main()
