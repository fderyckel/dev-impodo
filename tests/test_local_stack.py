from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from impodo.local_stack import (
    LocalStackCheck,
    LocalStackError,
    LocalStackService,
    LocalStackStartError,
    LocalStackStartResult,
    LocalStackStatus,
    ReadinessLevel,
    probe_local_stack,
    read_odoo_config,
    start_local_stack,
)


ROOT = Path(__file__).resolve().parents[1]


class LocalStackConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.workspace = Path(self.temporary.name) / "odoo_ve"
        self.config = self.workspace / "config" / "odoo.conf"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            "\n".join(
                (
                    "[options]",
                    "http_enable = true",
                    "http_interface = 127.0.0.1",
                    "http_port = 18069",
                    "db_host = 127.0.0.1",
                    "db_port = 5544",
                    "db_user = odoo",
                    "db_name = odoo19_local",
                    "db_password = postgres-secret",
                    "admin_passwd = master-secret",
                )
            ),
            encoding="utf-8",
        )
        for relative_path in (
            "tools/postgresql/pgsql/bin/pg_isready.exe",
            "tools/postgresql/pgsql/bin/pg_ctl.exe",
            "venv/Scripts/python.exe",
            "odoo/odoo-bin",
        ):
            candidate = self.workspace / relative_path
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.touch()
        (self.workspace / "pgdata").mkdir()
        (self.workspace / "pgdata" / "postmaster.pid").write_text(
            "4242\n",
            encoding="ascii",
        )
        (self.workspace / "logs").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reads_only_safe_local_routing_and_detects_known_paths(self) -> None:
        profile = read_odoo_config(self.config)

        self.assertEqual(profile.base_url, "http://127.0.0.1:18069")
        self.assertEqual(profile.db_port, 5544)
        self.assertEqual(profile.database_hint, "odoo19_local")
        self.assertEqual(
            profile.pg_isready_path,
            self.workspace
            / "tools"
            / "postgresql"
            / "pgsql"
            / "bin"
            / "pg_isready.exe",
        )
        self.assertEqual(profile.pg_data_path, self.workspace / "pgdata")
        self.assertNotIn("postgres-secret", repr(profile))
        self.assertNotIn("master-secret", repr(profile))

    def test_rejects_non_loopback_and_missing_configuration(self) -> None:
        self.config.write_text(
            "\n".join(
                (
                    "[options]",
                    "http_interface = 0.0.0.0",
                    "db_host = database.example.com",
                )
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(LocalStackError, "bind explicitly"):
            read_odoo_config(self.config)
        with self.assertRaisesRegex(LocalStackError, "existing .conf"):
            read_odoo_config(self.workspace / "config" / "missing.conf")

    @patch("impodo.local_stack._open_loopback")
    @patch("impodo.local_stack.subprocess.run")
    def test_checks_postgresql_then_odoo_without_a_shell(
        self,
        run,
        open_loopback,
    ) -> None:
        profile = read_odoo_config(self.config)
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="accepting connections",
            stderr="",
        )
        open_loopback.return_value = BytesIO(
            b'{"jsonrpc": "2.0", "id": 1, '
            b'"result": {"server_version": "19.0"}}'
        )

        status = probe_local_stack(profile)
        checks = {check.key: check for check in status.checks}

        self.assertEqual(checks["configuration"].level, ReadinessLevel.READY)
        self.assertEqual(checks["postgresql"].level, ReadinessLevel.READY)
        self.assertEqual(checks["odoo"].level, ReadinessLevel.READY)
        self.assertEqual(checks["api"].level, ReadinessLevel.UNKNOWN)
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ["-h", "127.0.0.1", "-p", "5544", "-t", "3"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(
            open_loopback.call_args.args[0].full_url,
            "http://127.0.0.1:18069/web/webclient/version_info",
        )
        self.assertEqual(open_loopback.call_args.args[0].method, "POST")

    def test_invalid_selection_is_reported_without_persisting_secrets(self) -> None:
        service = LocalStackService()

        status = service.select_config("project-1", self.config.with_suffix(".txt"))

        self.assertEqual(status.checks[0].level, ReadinessLevel.ERROR)
        self.assertNotIn("postgres-secret", repr(status))
        self.assertNotIn("master-secret", repr(status))

    def test_refresh_rereads_the_live_configuration(self) -> None:
        def echo_status(profile):
            return LocalStackStatus(
                config_path=str(profile.config_path),
                base_url=profile.base_url,
                database_hint=profile.database_hint,
                checks=(),
                profile=profile,
            )

        service = LocalStackService(probe=echo_status)
        first = service.select_config("project-1", self.config)
        self.assertEqual(first.base_url, "http://127.0.0.1:18069")
        revised = self.config.read_text(encoding="utf-8").replace(
            "http_port = 18069",
            "http_port = 28069",
        )
        self.config.write_text(revised, encoding="utf-8")

        refreshed = service.refresh("project-1")

        self.assertEqual(refreshed.base_url, "http://127.0.0.1:28069")

    def test_starts_postgresql_then_odoo_with_fixed_commands(self) -> None:
        profile = read_odoo_config(self.config)
        initial = _stack_status(
            profile,
            postgresql=ReadinessLevel.ACTION,
            odoo=ReadinessLevel.ACTION,
        )
        ready = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.READY,
        )
        process = MagicMock()
        process.poll.return_value = None
        completed = (
            subprocess.CompletedProcess(args=[], returncode=3),
            subprocess.CompletedProcess(args=[], returncode=0),
        )

        with (
            patch("impodo.local_stack.os.name", "nt"),
            patch(
                "impodo.local_stack.probe_local_stack",
                side_effect=(initial, ready),
            ) as probe,
            patch(
                "impodo.local_stack.subprocess.run",
                side_effect=completed,
            ) as run,
            patch(
                "impodo.local_stack._probe_postgresql",
                return_value=ready.checks[1],
            ),
            patch(
                "impodo.local_stack._probe_odoo",
                return_value=ready.checks[2],
            ),
            patch(
                "impodo.local_stack.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch.dict(
                "impodo.local_stack.os.environ",
                {"PGPASSWORD": "must-not-be-inherited"},
            ),
        ):
            result = start_local_stack(profile)

        self.assertEqual(result.status, ready)
        self.assertIs(result.odoo_process, process)
        self.assertEqual(result.postgresql_pid, 4242)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(run.call_count, 2)
        status_command = run.call_args_list[0].args[0]
        start_command = run.call_args_list[1].args[0]
        self.assertEqual(
            status_command,
            [
                str(profile.pg_ctl_path),
                "status",
                "-D",
                str(profile.pg_data_path),
            ],
        )
        self.assertEqual(
            start_command,
            [
                str(profile.pg_ctl_path),
                "start",
                "-D",
                str(profile.pg_data_path),
                "-l",
                str(profile.logs_path / "postgresql.log"),
                "-o",
                "-h 127.0.0.1 -p 5544",
                "-w",
                "-t",
                "15",
            ],
        )
        for invoked in run.call_args_list:
            self.assertFalse(invoked.kwargs["shell"])
        odoo_command = popen.call_args.args[0]
        self.assertEqual(
            odoo_command,
            [
                str(profile.python_path),
                str(profile.odoo_bin_path),
                "-c",
                str(profile.config_path),
            ],
        )
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertNotIn("PGPASSWORD", popen.call_args.kwargs["env"])

    def test_does_not_start_odoo_until_postgresql_is_ready(self) -> None:
        profile = read_odoo_config(self.config)
        initial = _stack_status(
            profile,
            postgresql=ReadinessLevel.ACTION,
            odoo=ReadinessLevel.ACTION,
        )
        not_ready = LocalStackCheck(
            "postgresql",
            "PostgreSQL",
            ReadinessLevel.ACTION,
            "PostgreSQL is not ready yet.",
        )

        with (
            patch("impodo.local_stack.os.name", "nt"),
            patch("impodo.local_stack.probe_local_stack", return_value=initial),
            patch(
                "impodo.local_stack.subprocess.run",
                side_effect=(
                    subprocess.CompletedProcess(args=[], returncode=3),
                    subprocess.CompletedProcess(args=[], returncode=0),
                ),
            ),
            patch(
                "impodo.local_stack._probe_postgresql",
                return_value=not_ready,
            ),
            patch("impodo.local_stack.subprocess.Popen") as popen,
        ):
            with self.assertRaisesRegex(
                LocalStackError,
                "Odoo was not started",
            ):
                start_local_stack(profile)

        popen.assert_not_called()

    def test_stops_new_odoo_process_when_readiness_times_out(self) -> None:
        profile = read_odoo_config(self.config)
        initial = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.ACTION,
        )
        not_ready = initial.checks[2]
        process = MagicMock()
        process.poll.return_value = None

        with (
            patch("impodo.local_stack.os.name", "nt"),
            patch("impodo.local_stack.probe_local_stack", return_value=initial),
            patch(
                "impodo.local_stack._probe_odoo",
                return_value=not_ready,
            ),
            patch(
                "impodo.local_stack.subprocess.Popen",
                return_value=process,
            ),
            patch(
                "impodo.local_stack.time.monotonic",
                side_effect=(0, 31),
            ),
        ):
            with self.assertRaisesRegex(
                LocalStackError,
                "newly launched process was stopped",
            ):
                start_local_stack(profile)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_service_stops_owned_odoo_before_owned_postgresql(self) -> None:
        profile = read_odoo_config(self.config)
        action = _stack_status(
            profile,
            postgresql=ReadinessLevel.ACTION,
            odoo=ReadinessLevel.ACTION,
        )
        ready = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.READY,
        )
        process = MagicMock()
        process.poll.return_value = None
        service = LocalStackService(
            probe=lambda _profile: action,
            starter=lambda _profile: LocalStackStartResult(
                status=ready,
                odoo_process=process,
                postgresql_pid=4242,
            ),
        )
        service.select_config("project-1", self.config)
        started = service.start("project-1")
        self.assertEqual(started.managed_services, ("Odoo", "PostgreSQL"))
        events: list[str] = []

        with (
            patch(
                "impodo.local_stack._stop_owned_odoo",
                side_effect=lambda _process: events.append("odoo"),
            ),
            patch(
                "impodo.local_stack._stop_postgresql",
                side_effect=lambda _profile, **_kwargs: events.append(
                    "postgresql"
                ),
            ),
            patch(
                "impodo.local_stack._wait_for_loopback_port_closed",
                return_value=True,
            ),
        ):
            stopped = service.stop("project-1")

        self.assertEqual(events, ["odoo", "postgresql"])
        self.assertEqual(stopped.managed_services, ())

    def test_service_never_stops_an_external_stack(self) -> None:
        profile = read_odoo_config(self.config)
        ready = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.READY,
        )
        service = LocalStackService(probe=lambda _profile: ready)
        service.select_config("project-1", self.config)

        with (
            patch("impodo.local_stack._stop_owned_odoo") as stop_odoo,
            patch("impodo.local_stack._stop_postgresql") as stop_postgresql,
        ):
            with self.assertRaisesRegex(
                LocalStackError,
                "does not own any running service",
            ):
                service.stop("project-1")

        stop_odoo.assert_not_called()
        stop_postgresql.assert_not_called()

    def test_service_does_not_overwrite_existing_process_ownership(self) -> None:
        profile = read_odoo_config(self.config)
        action = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.ACTION,
        )
        ready = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.READY,
        )
        process = MagicMock()
        process.poll.return_value = None
        starter = MagicMock(
            return_value=LocalStackStartResult(
                status=ready,
                odoo_process=process,
                postgresql_pid=None,
            )
        )
        service = LocalStackService(
            probe=lambda _profile: action,
            starter=starter,
        )
        service.select_config("project-1", self.config)
        service.start("project-1")

        with self.assertRaisesRegex(
            LocalStackError,
            "already manages local services",
        ):
            service.start("project-1")

        self.assertEqual(starter.call_count, 1)
        self.assertEqual(service.get("project-1").managed_services, ("Odoo",))

    def test_service_retains_a_process_that_startup_could_not_clean_up(
        self,
    ) -> None:
        profile = read_odoo_config(self.config)
        action = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.ACTION,
        )
        process = MagicMock()
        process.poll.return_value = None

        def fail_start(_profile):
            raise LocalStackStartError(
                "Odoo startup failed and cleanup did not finish.",
                profile=profile,
                postgresql_pid=None,
                odoo_process=process,
            )

        service = LocalStackService(
            probe=lambda _profile: action,
            starter=fail_start,
        )
        service.select_config("project-1", self.config)

        with self.assertRaisesRegex(
            LocalStackStartError,
            "cleanup did not finish",
        ):
            service.start("project-1")

        self.assertEqual(service.get("project-1").managed_services, ("Odoo",))

    def test_owned_postgresql_uses_fast_stop_and_verifies_status(self) -> None:
        profile = read_odoo_config(self.config)
        ready = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.ACTION,
        )
        stopped_status = _stack_status(
            profile,
            postgresql=ReadinessLevel.ACTION,
            odoo=ReadinessLevel.ACTION,
        )
        service = LocalStackService(
            probe=lambda _profile: stopped_status,
            starter=lambda _profile: LocalStackStartResult(
                status=ready,
                odoo_process=None,
                postgresql_pid=4242,
            ),
        )
        service.select_config("project-1", self.config)
        service.start("project-1")

        with patch(
            "impodo.local_stack.subprocess.run",
            side_effect=(
                subprocess.CompletedProcess(args=[], returncode=0),
                subprocess.CompletedProcess(args=[], returncode=0),
                subprocess.CompletedProcess(args=[], returncode=3),
            ),
        ) as run:
            stopped = service.stop("project-1")

        self.assertEqual(stopped.managed_services, ())
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                str(profile.pg_ctl_path),
                "stop",
                "-D",
                str(profile.pg_data_path),
                "-m",
                "fast",
                "-w",
                "-t",
                "15",
            ],
        )
        for invoked in run.call_args_list:
            self.assertFalse(invoked.kwargs["shell"])

    def test_owned_postgresql_is_not_stopped_after_pid_changes(self) -> None:
        profile = read_odoo_config(self.config)
        ready = _stack_status(
            profile,
            postgresql=ReadinessLevel.READY,
            odoo=ReadinessLevel.ACTION,
        )
        service = LocalStackService(
            probe=lambda _profile: ready,
            starter=lambda _profile: LocalStackStartResult(
                status=ready,
                odoo_process=None,
                postgresql_pid=4242,
            ),
        )
        service.select_config("project-1", self.config)
        service.start("project-1")
        (self.workspace / "pgdata" / "postmaster.pid").write_text(
            "5252\n",
            encoding="ascii",
        )

        with patch(
            "impodo.local_stack.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as run:
            with self.assertRaisesRegex(
                LocalStackError,
                "server identity changed",
            ):
                service.stop("project-1")

        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            service.get("project-1").managed_services,
            ("PostgreSQL",),
        )


def _stack_status(
    profile,
    *,
    postgresql: ReadinessLevel,
    odoo: ReadinessLevel,
) -> LocalStackStatus:
    return LocalStackStatus(
        config_path=str(profile.config_path),
        base_url=profile.base_url,
        database_hint=profile.database_hint,
        checks=(
            LocalStackCheck(
                "configuration",
                "Configuration",
                ReadinessLevel.READY,
                "Valid loopback Odoo configuration.",
            ),
            LocalStackCheck(
                "postgresql",
                "PostgreSQL",
                postgresql,
                "PostgreSQL status.",
            ),
            LocalStackCheck(
                "odoo",
                "Odoo server",
                odoo,
                "Odoo status.",
            ),
            LocalStackCheck(
                "api",
                "Database access (read-only)",
                ReadinessLevel.UNKNOWN,
                "Use Save and test connection.",
            ),
        ),
        profile=profile,
    )


if __name__ == "__main__":
    unittest.main()
