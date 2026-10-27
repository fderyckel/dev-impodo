"""Prove ordered startup, private failures, and controlled server drain."""

from __future__ import annotations

from pathlib import PurePosixPath
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.web.composition.server_config import ServerSettings, load_server_settings
from impodo.web.composition.server_lifecycle import (
    ServerLifecycleError,
    ServerPhase,
)
from impodo.adapters.postgresql.startup import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
)
from impodo.web.composition.server_active_work import (
    ServerActiveWorkOwner,
    ServerWorkAdmissionClosedError,
)
from impodo.web.composition.server_linux_runtime import (
    create_file_secret_linux_server_runtime,
    create_linux_server_runtime,
)
from impodo.web.composition.server_runtime import (
    ServerDrainError,
    ServerPostgreSQLProbeError,
    ServerRuntimeCoordinator,
    ServerRuntimePorts,
    ServerSchemaProbeError,
    ServerStartupError,
    ServerStartupStage,
)
from impodo.web.composition.server_runtime_app import (
    create_server_runtime_control_app,
)
from impodo.web.composition.server_storage import (
    StorageMountKind,
    StorageRootObservation,
)


def _settings() -> ServerSettings:
    return load_server_settings(
        {
            "IMPODO_DEPLOYMENT_ID": "shared-primary",
            "IMPODO_PUBLIC_URL": "https://impodo.example.test",
            "IMPODO_TRUSTED_PROXY_CIDRS": "172.20.0.0/24",
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


class ServerRuntimeCoordinationTests(unittest.TestCase):
    def test_file_secret_linux_runtime_binds_secure_reader_and_psycopg(self) -> None:
        settings = _settings()
        session = Mock()
        session.fetch_rows.side_effect = [
            ((1, POSTGRESQL_RUNTIME_ROLE),),
            (
                (
                    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
                    POSTGRESQL_CONTROL_SCHEMA_VERSION,
                ),
            ),
        ]
        with (
            patch(
                "impodo.web.composition.server_linux_runtime."
                "observe_linux_server_storage",
                return_value=_safe_observations(settings),
            ),
            patch(
                "impodo.web.composition.server_linux_runtime."
                "LinuxSecretFileReader.read_text",
                return_value="private-dsn",
            ) as read_secret,
        ):
            runtime = create_file_secret_linux_server_runtime(
                settings,
                postgresql_session_factory=lambda _dsn: session,
            )
            runtime.startup()

        read_secret.assert_called_once_with(
            settings.secret_provider.root,
            settings.postgres_dsn_file,
        )
        self.assertTrue(runtime.snapshot().is_ready)
        session.close.assert_called_once_with()

    def test_linux_composition_binds_the_real_storage_observer(self) -> None:
        settings = _settings()
        with patch(
            "impodo.web.composition.server_linux_runtime.observe_linux_server_storage",
            return_value=_safe_observations(settings),
        ) as observer:
            runtime = create_linux_server_runtime(
                settings,
                postgresql_inspector=lambda _settings: None,
            )
            runtime.startup()

        observer.assert_called_once_with(settings)
        self.assertTrue(runtime.snapshot().is_ready)

    def test_startup_orders_all_proofs_before_admitting_work(self) -> None:
        events: list[str] = []

        def observe_storage(
            settings: ServerSettings,
        ) -> tuple[StorageRootObservation, ...]:
            events.append("storage")
            return _safe_observations(settings)

        def inspect_postgresql(_settings: ServerSettings) -> None:
            events.append("postgresql_and_schema")

        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=observe_storage,
                postgresql_inspector=inspect_postgresql,
            ),
        )

        accepting = runtime.startup()

        self.assertTrue(accepting.is_ready)
        self.assertEqual(events, ["storage", "postgresql_and_schema"])
        lease = runtime.begin_work()
        lease.complete()
        stopped = runtime.drain()
        self.assertIs(stopped.phase, ServerPhase.STOPPED)
        self.assertEqual(events, ["storage", "postgresql_and_schema"])

    def test_dependency_failure_stops_startup_without_exposing_cause(self) -> None:
        def fail_postgresql(_settings: ServerSettings) -> None:
            raise ServerPostgreSQLProbeError(
                "postgresql://operator:secret@database/impodo"
            )

        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=_safe_observations,
                postgresql_inspector=fail_postgresql,
            ),
        )

        with self.assertRaises(ServerStartupError) as caught:
            runtime.startup()

        self.assertIs(caught.exception.stage, ServerStartupStage.POSTGRESQL)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIs(runtime.snapshot().phase, ServerPhase.STOPPED)
        self.assertFalse(runtime.snapshot().is_ready)

    def test_schema_failure_records_reachability_before_stopping(self) -> None:
        def fail_schema(_settings: ServerSettings) -> None:
            raise ServerSchemaProbeError("observed generation and version")

        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=_safe_observations,
                postgresql_inspector=fail_schema,
            ),
        )

        with self.assertRaises(ServerStartupError) as caught:
            runtime.startup()

        self.assertIs(caught.exception.stage, ServerStartupStage.SCHEMA)
        self.assertNotIn("generation", str(caught.exception))
        self.assertIn(
            "postgresql",
            tuple(check.value for check in runtime.snapshot().satisfied_checks),
        )
        self.assertIs(runtime.snapshot().phase, ServerPhase.STOPPED)

    def test_failed_checkpoint_wait_keeps_process_draining_for_retry(self) -> None:
        clock_values = iter((0.0, 31.0, 31.0))
        active_work = ServerActiveWorkOwner(monotonic_clock=lambda: next(clock_values))

        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=_safe_observations,
                postgresql_inspector=lambda _settings: None,
            ),
            active_work_owner=active_work,
        )
        runtime.startup()
        lease = runtime.begin_work()
        runtime.begin_draining()

        with self.assertRaises(ServerDrainError):
            runtime.complete_draining()

        self.assertIs(runtime.snapshot().phase, ServerPhase.DRAINING)
        self.assertEqual(active_work.snapshot().active_count, 1)
        with self.assertRaises(ServerWorkAdmissionClosedError):
            runtime.begin_work()
        lease.complete()
        self.assertIs(runtime.complete_draining().phase, ServerPhase.STOPPED)
        self.assertEqual(active_work.snapshot().active_count, 0)

    def test_complete_drain_refuses_to_skip_readiness_withdrawal(self) -> None:
        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=_safe_observations,
                postgresql_inspector=lambda _settings: None,
            ),
        )
        runtime.startup()

        with self.assertRaisesRegex(ServerLifecycleError, "withdrawn"):
            runtime.complete_draining()

        self.assertTrue(runtime.snapshot().is_ready)

    def test_asgi_lifespan_starts_and_drains_the_runtime(self) -> None:
        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=_safe_observations,
                postgresql_inspector=lambda _settings: None,
            ),
        )

        with TestClient(create_server_runtime_control_app(runtime)) as client:
            response = client.get("/health/ready")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ready"})

        self.assertIs(runtime.snapshot().phase, ServerPhase.STOPPED)

    def test_asgi_lifespan_refuses_a_failed_startup(self) -> None:
        def fail_postgresql(_settings: ServerSettings) -> None:
            raise ServerPostgreSQLProbeError("database unavailable")

        runtime = ServerRuntimeCoordinator(
            _settings(),
            ServerRuntimePorts(
                storage_observer=_safe_observations,
                postgresql_inspector=fail_postgresql,
            ),
        )

        with self.assertRaises(ServerStartupError):
            with TestClient(create_server_runtime_control_app(runtime)):
                self.fail("The ASGI process admitted a failed runtime")

        self.assertIs(runtime.snapshot().phase, ServerPhase.STOPPED)


if __name__ == "__main__":
    unittest.main()
