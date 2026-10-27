"""Prove production web binding, signal drain, and active-request ownership."""

from __future__ import annotations

import signal
from pathlib import PurePosixPath
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from impodo.web.composition.server_active_work import (
    ServerWorkAdmissionClosedError,
)
from impodo.web.composition.server_config import ServerSettings, load_server_settings
from impodo.web.composition.server_lifecycle import ServerPhase
from impodo.web.composition.server_linux_web import (
    run_file_secret_linux_server_web,
)
from impodo.web.composition.server_runtime import (
    ServerRuntimeCoordinator,
    ServerRuntimePorts,
)
from impodo.web.composition.server_runtime_app import (
    create_server_runtime_control_app,
)
from impodo.web.composition.server_storage import (
    StorageMountKind,
    StorageRootObservation,
)
from impodo.web.composition.server_uvicorn import (
    create_production_uvicorn_server,
)


def _settings() -> ServerSettings:
    return load_server_settings(
        {
            "IMPODO_DEPLOYMENT_ID": "shared-primary",
            "IMPODO_PUBLIC_URL": "https://impodo.example.test",
            "IMPODO_TRUSTED_PROXY_CIDRS": "172.20.0.0/24,10.10.0.7/32",
            "IMPODO_WEB_BIND_HOST": "0.0.0.0",
            "IMPODO_WEB_BIND_PORT": "8000",
            "IMPODO_DRAIN_TIMEOUT_SECONDS": "30",
            "IMPODO_POSTGRES_DSN_FILE": "/run/secrets/postgres-dsn",
            "IMPODO_ARTIFACT_ROOT": "/var/lib/impodo/artifacts",
            "IMPODO_RUNTIME_ROOT": "/var/lib/impodo/runtime",
            "IMPODO_TEMP_ROOT": "/var/tmp/impodo",
            "IMPODO_SECRET_PROVIDER": "files",
            "IMPODO_SECRET_ROOT": "/run/secrets",
            "IMPODO_WORKER_CAPACITY": "8",
            "IMPODO_LOG_LEVEL": "warning",
            "IMPODO_ODOO_EGRESS_MODE": "disabled",
        }
    )


def _safe_observations(
    settings: ServerSettings,
) -> tuple[StorageRootObservation, ...]:
    def observation(
        path: PurePosixPath,
        mount_kind: StorageMountKind,
    ) -> StorageRootObservation:
        return StorageRootObservation(
            path=path,
            exists=True,
            is_directory=True,
            owner_uid=10001,
            owner_gid=10001,
            mode=0o750,
            writable=True,
            mount_kind=mount_kind,
        )

    return (
        observation(settings.artifact_root, StorageMountKind.PERSISTENT),
        observation(settings.runtime_root, StorageMountKind.PERSISTENT),
        observation(settings.temporary_root, StorageMountKind.TRANSIENT),
    )


def _runtime() -> ServerRuntimeCoordinator:
    return ServerRuntimeCoordinator(
        _settings(),
        ServerRuntimePorts(
            storage_observer=_safe_observations,
            postgresql_inspector=lambda _settings: None,
        ),
    )


class ServerWebProcessTests(unittest.TestCase):
    def test_uvicorn_uses_explicit_private_proxy_and_shutdown_settings(self) -> None:
        runtime = _runtime()
        server = create_production_uvicorn_server(
            create_server_runtime_control_app(runtime),
            _settings(),
            begin_draining=runtime.begin_draining,
        )

        self.assertEqual(server.config.host, "0.0.0.0")
        self.assertEqual(server.config.port, 8000)
        self.assertEqual(server.config.workers, 1)
        self.assertTrue(server.config.proxy_headers)
        self.assertEqual(
            server.config.forwarded_allow_ips,
            ["172.20.0.0/24", "10.10.0.7/32"],
        )
        self.assertFalse(server.config.access_log)
        self.assertFalse(server.config.server_header)
        self.assertEqual(server.config.timeout_graceful_shutdown, 30)
        self.assertEqual(server.config.lifespan, "on")

    def test_uvicorn_signal_closes_admission_before_transport_exit(self) -> None:
        runtime = _runtime()
        runtime.startup()
        server = create_production_uvicorn_server(
            create_server_runtime_control_app(runtime),
            _settings(),
            begin_draining=runtime.begin_draining,
        )

        server.handle_exit(signal.SIGTERM, None)

        self.assertTrue(server.should_exit)
        self.assertIs(runtime.snapshot().phase, ServerPhase.DRAINING)
        with self.assertRaises(ServerWorkAdmissionClosedError):
            runtime.begin_work()
        self.assertIs(runtime.complete_draining().phase, ServerPhase.STOPPED)

    def test_asgi_work_lease_lasts_until_the_full_response_completes(self) -> None:
        runtime = _runtime()
        entered = Event()
        release = Event()
        responses: list[int] = []
        app = create_server_runtime_control_app(runtime)

        @app.get("/work")
        def work() -> dict[str, str]:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release active request")
            return {"status": "complete"}

        with TestClient(app) as client:
            thread = Thread(
                target=lambda: responses.append(client.get("/work").status_code)
            )
            thread.start()
            self.assertTrue(entered.wait(timeout=2))

            runtime.begin_draining()

            self.assertEqual(client.get("/health/ready").status_code, 503)
            self.assertEqual(client.get("/not-admitted").status_code, 503)
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(responses, [200])
        self.assertIs(runtime.snapshot().phase, ServerPhase.STOPPED)

    def test_linux_web_composition_runs_the_created_uvicorn_server(self) -> None:
        runtime = _runtime()
        app = create_server_runtime_control_app(runtime)
        server = Mock()
        with (
            patch(
                "impodo.web.composition.server_linux_web."
                "create_file_secret_linux_server_runtime",
                return_value=runtime,
            ),
            patch(
                "impodo.web.composition.server_linux_web."
                "create_server_runtime_control_app",
                return_value=app,
            ),
            patch(
                "impodo.web.composition.server_linux_web."
                "create_production_uvicorn_server",
                return_value=server,
            ) as create_server,
        ):
            run_file_secret_linux_server_web(_settings())

        create_server.assert_called_once_with(
            app,
            _settings(),
            begin_draining=runtime.begin_draining,
        )
        server.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
