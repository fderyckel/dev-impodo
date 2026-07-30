from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from impodo.local_stack import (
    LocalStackError,
    LocalStackService,
    LocalStackStatus,
    ReadinessLevel,
    probe_local_stack,
    read_odoo_config,
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
                    "db_name = odoo19_dev",
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reads_only_safe_local_routing_and_detects_known_paths(self) -> None:
        profile = read_odoo_config(self.config)

        self.assertEqual(profile.base_url, "http://127.0.0.1:18069")
        self.assertEqual(profile.db_port, 5544)
        self.assertEqual(profile.database_hint, "odoo19_dev")
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


if __name__ == "__main__":
    unittest.main()
