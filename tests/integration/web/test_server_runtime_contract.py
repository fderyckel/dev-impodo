"""Prove the fail-closed configuration, storage, and lifecycle contracts."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import PurePosixPath
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from impodo.web.composition.server_cli import CONFIGURATION_ERROR_EXIT, main
from impodo.web.composition.server_config import (
    OdooEgressMode,
    SecretProviderKind,
    ServerConfigurationError,
    load_server_settings,
)
from impodo.web.composition.server_control_app import create_server_control_app
from impodo.web.composition.server_linux_storage import (
    LinuxStorageObservationError,
    observe_linux_server_storage,
    parse_linux_mountinfo,
)
from impodo.web.composition.server_lifecycle import (
    ReadinessCheck,
    ServerLifecycle,
    ServerLifecycleError,
    ServerLifecycleOwner,
    ServerPhase,
    liveness_payload,
)
from impodo.web.composition.server_storage import (
    ServerStorageValidationError,
    StorageMountKind,
    StorageRootObservation,
    validate_server_storage,
)


def _valid_environment() -> dict[str, str]:
    return {
        "IMPODO_DEPLOYMENT_ID": "shared-primary",
        "IMPODO_PUBLIC_URL": "https://Impodo.Example.test:443/",
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


def _observation(
    path: PurePosixPath,
    *,
    mount_kind: StorageMountKind,
    owner_uid: int | None = 10001,
    owner_gid: int | None = 10001,
    mode: int | None = 0o750,
    writable: bool = True,
) -> StorageRootObservation:
    return StorageRootObservation(
        path=path,
        exists=True,
        is_directory=True,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=mode,
        writable=writable,
        mount_kind=mount_kind,
    )


class ServerConfigurationTests(unittest.TestCase):
    def test_valid_non_secret_settings_are_typed_and_normalized(self) -> None:
        settings = load_server_settings(_valid_environment())

        self.assertEqual(settings.deployment_id, "shared-primary")
        self.assertEqual(settings.public_url, "https://impodo.example.test")
        self.assertEqual(
            tuple(str(network) for network in settings.trusted_proxy_networks),
            ("172.20.0.0/24", "10.10.0.7/32"),
        )
        self.assertEqual(str(settings.web_bind_host), "0.0.0.0")
        self.assertEqual(settings.web_bind_port, 8000)
        self.assertEqual(settings.drain_timeout_seconds, 30)
        self.assertEqual(settings.worker_capacity, 8)
        self.assertEqual(settings.log_level.value, "WARNING")
        self.assertIs(settings.secret_provider.kind, SecretProviderKind.FILES)
        self.assertIs(settings.odoo_network.mode, OdooEgressMode.DISABLED)

    def test_unknown_impodo_setting_rejects_raw_database_secret(self) -> None:
        environment = _valid_environment()
        environment["IMPODO_POSTGRES_DSN"] = "postgresql://user:secret@database/impodo"

        with self.assertRaises(ServerConfigurationError) as caught:
            load_server_settings(environment)

        message = str(caught.exception)
        self.assertIn("IMPODO_POSTGRES_DSN", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("postgresql://", message)

    def test_all_missing_required_settings_are_reported_without_values(self) -> None:
        with self.assertRaisesRegex(
            ServerConfigurationError,
            "Required server setting.*IMPODO_ARTIFACT_ROOT",
        ):
            load_server_settings({})

    def test_public_url_must_be_a_plain_https_origin(self) -> None:
        for value in (
            "http://impodo.example.test",
            "https://user@example.test",
            "https://impodo.example.test/application",
            "https://impodo.example.test?secret=value",
            "https://invalid_host.example.test",
        ):
            with self.subTest(value=value):
                environment = _valid_environment()
                environment["IMPODO_PUBLIC_URL"] = value
                with self.assertRaisesRegex(
                    ServerConfigurationError,
                    "IMPODO_PUBLIC_URL",
                ):
                    load_server_settings(environment)

    def test_web_bind_and_drain_settings_are_bounded(self) -> None:
        invalid_settings = (
            ("IMPODO_WEB_BIND_HOST", "impodo-web"),
            ("IMPODO_WEB_BIND_PORT", "1023"),
            ("IMPODO_WEB_BIND_PORT", "65536"),
            ("IMPODO_DRAIN_TIMEOUT_SECONDS", "0"),
            ("IMPODO_DRAIN_TIMEOUT_SECONDS", "3601"),
        )
        for key, value in invalid_settings:
            with self.subTest(key=key, value=value):
                environment = _valid_environment()
                environment[key] = value
                with self.assertRaisesRegex(ServerConfigurationError, key):
                    load_server_settings(environment)

    def test_secret_references_cannot_reside_below_data_roots(self) -> None:
        environment = _valid_environment()
        environment["IMPODO_POSTGRES_DSN_FILE"] = (
            "/var/lib/impodo/artifacts/postgres-dsn"
        )

        with self.assertRaisesRegex(ServerConfigurationError, "Secret references"):
            load_server_settings(environment)

    def test_file_secret_references_must_be_direct_children_of_secret_root(
        self,
    ) -> None:
        for value in (
            "/etc/impodo/postgres-dsn",
            "/run/secrets/database/postgres-dsn",
        ):
            with self.subTest(value=value):
                environment = _valid_environment()
                environment["IMPODO_POSTGRES_DSN_FILE"] = value
                with self.assertRaisesRegex(
                    ServerConfigurationError,
                    "direct children",
                ):
                    load_server_settings(environment)

    def test_external_secret_provider_requires_only_its_configuration_file(
        self,
    ) -> None:
        environment = _valid_environment()
        environment["IMPODO_SECRET_PROVIDER"] = "external"
        environment["IMPODO_SECRET_PROVIDER_CONFIG_FILE"] = (
            "/etc/impodo/secret-provider.toml"
        )
        del environment["IMPODO_SECRET_ROOT"]

        settings = load_server_settings(environment)

        self.assertIs(settings.secret_provider.kind, SecretProviderKind.EXTERNAL)
        self.assertEqual(
            settings.secret_provider.configuration_file,
            PurePosixPath("/etc/impodo/secret-provider.toml"),
        )

    def test_enabled_odoo_egress_requires_exact_destinations(self) -> None:
        environment = _valid_environment()
        environment["IMPODO_ODOO_EGRESS_MODE"] = "allowlist"

        with self.assertRaisesRegex(ServerConfigurationError, "destinations"):
            load_server_settings(environment)

        environment["IMPODO_ODOO_ALLOWED_DESTINATIONS"] = (
            "Odoo.Example.test:8069,10.42.0.5,10.42.1.0/24"
        )
        settings = load_server_settings(environment)
        self.assertEqual(
            settings.odoo_network.allowed_destinations,
            ("odoo.example.test:8069", "10.42.0.5", "10.42.1.0/24"),
        )

    def test_proxy_egress_accepts_only_a_proxy_secret_file_reference(self) -> None:
        environment = _valid_environment()
        environment["IMPODO_ODOO_EGRESS_MODE"] = "http_proxy"
        environment["IMPODO_ODOO_ALLOWED_DESTINATIONS"] = "odoo.example.test"

        with self.assertRaisesRegex(ServerConfigurationError, "PROXY_URL_FILE"):
            load_server_settings(environment)

        environment["IMPODO_ODOO_PROXY_URL_FILE"] = "/run/secrets/odoo-proxy-url"
        settings = load_server_settings(environment)
        self.assertEqual(
            settings.odoo_network.proxy_url_file,
            PurePosixPath("/run/secrets/odoo-proxy-url"),
        )

    def test_server_cli_runs_the_health_only_web_process(self) -> None:
        with (
            patch.dict("os.environ", _valid_environment(), clear=True),
            patch(
                "impodo.web.composition.server_cli.run_file_secret_linux_server_web"
            ) as run_web,
        ):
            result = main(("web",))

        self.assertEqual(result, 0)
        run_web.assert_called_once()
        self.assertEqual(run_web.call_args.args[0].web_bind_port, 8000)

    def test_server_cli_validates_settings_before_each_incomplete_runtime_role(
        self,
    ) -> None:
        with patch.dict("os.environ", _valid_environment(), clear=True):
            for command in ("worker", "admin"):
                with self.subTest(command=command):
                    error = StringIO()
                    with redirect_stderr(error):
                        result = main((command,))
                    self.assertEqual(result, CONFIGURATION_ERROR_EXIT)
                    self.assertIn(f"{command} startup refused", error.getvalue())
                    self.assertNotIn("Required server setting", error.getvalue())


class ServerStorageTests(unittest.TestCase):
    def test_safe_roots_accept_persistent_artifacts_and_private_scratch(self) -> None:
        settings = load_server_settings(_valid_environment())

        validate_server_storage(
            settings,
            (
                _observation(
                    settings.artifact_root,
                    mount_kind=StorageMountKind.PERSISTENT,
                ),
                _observation(
                    settings.runtime_root,
                    mount_kind=StorageMountKind.PERSISTENT,
                ),
                _observation(
                    settings.temporary_root,
                    mount_kind=StorageMountKind.TRANSIENT,
                ),
            ),
        )

    def test_unsafe_roots_report_all_bounded_policy_failures(self) -> None:
        settings = load_server_settings(_valid_environment())

        with self.assertRaises(ServerStorageValidationError) as caught:
            validate_server_storage(
                settings,
                (
                    _observation(
                        settings.artifact_root,
                        mount_kind=StorageMountKind.TRANSIENT,
                        owner_uid=0,
                        mode=0o757,
                        writable=False,
                    ),
                    _observation(
                        settings.runtime_root,
                        mount_kind=StorageMountKind.UNKNOWN,
                    ),
                ),
            )

        self.assertEqual(
            tuple(issue.code for issue in caught.exception.issues),
            (
                "wrong_owner_uid",
                "world_accessible",
                "not_writable",
                "not_persistent",
                "not_observed",
            ),
        )


class ServerLifecycleTests(unittest.TestCase):
    def test_process_accepts_only_after_every_check_then_drains(self) -> None:
        lifecycle = ServerLifecycle()
        for check in ReadinessCheck:
            lifecycle = lifecycle.with_check(check, satisfied=True)

        accepting = lifecycle.begin_accepting()
        self.assertTrue(accepting.is_ready)
        self.assertEqual(accepting.readiness_payload(), {"status": "ready"})

        draining = accepting.begin_draining()
        self.assertFalse(draining.is_ready)
        self.assertEqual(draining.readiness_payload(), {"status": "not_ready"})
        self.assertIs(draining.mark_stopped().phase, ServerPhase.STOPPED)

    def test_failed_check_withdraws_readiness_without_public_detail(self) -> None:
        lifecycle = ServerLifecycle(
            phase=ServerPhase.ACCEPTING,
            satisfied_checks=frozenset(ReadinessCheck),
        )

        lifecycle = lifecycle.with_check(ReadinessCheck.POSTGRESQL, satisfied=False)

        self.assertIs(lifecycle.phase, ServerPhase.STARTING)
        self.assertEqual(lifecycle.blockers, (ReadinessCheck.POSTGRESQL,))
        self.assertEqual(lifecycle.readiness_payload(), {"status": "not_ready"})
        self.assertEqual(liveness_payload(), {"status": "alive"})

    def test_incomplete_or_uncontrolled_transitions_fail_closed(self) -> None:
        with self.assertRaisesRegex(ServerLifecycleError, "checks are blocked"):
            ServerLifecycle().begin_accepting()

        accepting = ServerLifecycle(
            phase=ServerPhase.ACCEPTING,
            satisfied_checks=frozenset(ReadinessCheck),
        )
        with self.assertRaisesRegex(ServerLifecycleError, "stop directly"):
            accepting.mark_stopped()


class ServerControlAppTests(unittest.TestCase):
    def test_liveness_is_dependency_free_and_readiness_is_generic(self) -> None:
        reads = 0
        owner = ServerLifecycleOwner()

        def read_lifecycle() -> ServerLifecycle:
            nonlocal reads
            reads += 1
            return owner.snapshot()

        client = TestClient(create_server_control_app(read_lifecycle))

        live = client.get("/health/live")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json(), {"status": "alive"})
        self.assertEqual(reads, 0)
        self.assertEqual(live.headers["cache-control"], "no-store")

        ready = client.get("/health/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json(), {"status": "not_ready"})
        self.assertEqual(reads, 1)
        self.assertEqual(ready.headers["cache-control"], "no-store")

    def test_readiness_follows_atomic_accept_and_drain_transitions(self) -> None:
        owner = ServerLifecycleOwner()
        client = TestClient(create_server_control_app(owner.snapshot))
        for check in ReadinessCheck:
            owner.record_check(check, satisfied=True)
        owner.begin_accepting()

        self.assertEqual(client.get("/health/ready").status_code, 200)
        self.assertEqual(
            client.get("/health/ready").json(),
            {"status": "ready"},
        )

        owner.begin_draining()
        self.assertEqual(client.get("/health/ready").status_code, 503)
        self.assertEqual(client.get("/health/live").status_code, 200)

    def test_control_app_exposes_no_docs_or_migration_routes(self) -> None:
        client = TestClient(create_server_control_app(ServerLifecycleOwner().snapshot))

        for path in ("/", "/docs", "/redoc", "/openapi.json", "/projects"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)


class LinuxStorageObserverTests(unittest.TestCase):
    def test_observer_classifies_explicit_persistent_and_transient_mounts(self) -> None:
        settings = load_server_settings(_valid_environment())
        mountinfo = "\n".join(
            (
                "30 20 0:30 / / rw,relatime - overlay overlay rw",
                "31 30 8:16 / /var/lib/impodo/artifacts rw,relatime - ext4 /dev/vdb rw",
                "32 30 0:45 / /var/tmp/impodo rw,nosuid - tmpfs tmpfs rw",
            )
        )

        def read_stat(_path: PurePosixPath) -> SimpleNamespace:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o750,
                st_uid=10001,
                st_gid=10001,
            )

        observations = observe_linux_server_storage(
            settings,
            mountinfo=mountinfo,
            stat_reader=read_stat,
            writable_probe=lambda _path: True,
        )

        self.assertEqual(
            tuple(observation.mount_kind for observation in observations),
            (
                StorageMountKind.PERSISTENT,
                StorageMountKind.TRANSIENT,
                StorageMountKind.TRANSIENT,
            ),
        )
        validate_server_storage(settings, observations)

    def test_root_filesystem_does_not_prove_persistent_artifact_storage(self) -> None:
        settings = load_server_settings(_valid_environment())
        mountinfo = "30 20 8:1 / / rw,relatime - ext4 /dev/vda1 rw"

        observations = observe_linux_server_storage(
            settings,
            mountinfo=mountinfo,
            stat_reader=lambda _path: SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o750,
                st_uid=10001,
                st_gid=10001,
            ),
            writable_probe=lambda _path: True,
        )

        with self.assertRaises(ServerStorageValidationError) as caught:
            validate_server_storage(settings, observations)
        self.assertIn(
            "not_persistent",
            tuple(issue.code for issue in caught.exception.issues),
        )

    def test_read_only_mount_overrides_a_successful_write_probe(self) -> None:
        settings = load_server_settings(_valid_environment())
        mountinfo = "\n".join(
            (
                "30 20 0:30 / / rw,relatime - overlay overlay rw",
                "31 30 8:16 / /var/lib/impodo/artifacts ro,relatime - ext4 /dev/vdb ro",
                "32 30 0:45 / /var/tmp/impodo rw,nosuid - tmpfs tmpfs rw",
            )
        )

        observations = observe_linux_server_storage(
            settings,
            mountinfo=mountinfo,
            stat_reader=lambda _path: SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o750,
                st_uid=10001,
                st_gid=10001,
            ),
            writable_probe=lambda _path: True,
        )

        self.assertFalse(observations[0].writable)

    def test_mount_parser_decodes_kernel_escapes_and_rejects_bad_input(self) -> None:
        mounts = parse_linux_mountinfo(
            "31 30 8:16 / /var/lib/impodo/artifacts\\040archive rw - ext4 /dev/vdb rw"
        )
        self.assertEqual(
            mounts[0].mount_point,
            PurePosixPath("/var/lib/impodo/artifacts archive"),
        )

        for value in ("", "not mount information"):
            with self.subTest(value=value):
                with self.assertRaises(LinuxStorageObservationError):
                    parse_linux_mountinfo(value)


if __name__ == "__main__":
    unittest.main()
