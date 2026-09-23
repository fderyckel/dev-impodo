"""Check the disposable lab's isolation without starting Odoo or PostgreSQL."""

import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from scripts.prepare_odoo_compatibility_lab import (
    LAB_ROOT,
    MANIFEST,
    _require_free_port,
    configuration,
    prepare,
)


class CompatibilityLabTests(unittest.TestCase):
    def test_targets_have_distinct_pins_databases_and_ports(self):
        targets = json.loads(MANIFEST.read_text(encoding="utf-8"))["targets"]
        for key in ("commit", "database", "http_port", "postgres_port"):
            self.assertEqual(
                len({target[key] for target in targets.values()}),
                len(targets),
            )
        for name, target in targets.items():
            config = configuration(target, Path("C:/lab/source"), LAB_ROOT / name)
            self.assertIn("http_interface = 127.0.0.1\n", config)
            self.assertIn("db_host = 127.0.0.1\n", config)
            self.assertIn(f"dbfilter = ^{target['database']}$\n", config)
            self.assertIn("list_db = False\n", config)
            self.assertIn("max_cron_threads = 0\n", config)

    def test_final_odoo20_target_is_pinned_and_not_the_preview(self):
        targets = json.loads(MANIFEST.read_text(encoding="utf-8"))["targets"]

        self.assertEqual(targets["odoo20"]["reported_version"], "20.0")
        self.assertRegex(targets["odoo20"]["commit"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(
            targets["odoo20"]["commit"],
            targets["preview"]["commit"],
        )

    def test_existing_lab_is_rejected_before_any_process_runs(self):
        with patch.object(Path, "exists", return_value=True), patch(
            "scripts.prepare_odoo_compatibility_lab._output"
        ) as command:
            with self.assertRaisesRegex(ValueError, "existing data is never reset"):
                prepare("baseline", Path("source"), Path("python"), Path("postgres"))
        command.assert_not_called()

    def test_wrong_source_pin_is_rejected_before_installation_or_database_creation(self):
        with patch.object(Path, "exists", return_value=False), patch.object(
            Path, "is_file", return_value=True
        ), patch("scripts.prepare_odoo_compatibility_lab._output", return_value="0" * 40), patch(
            "scripts.prepare_odoo_compatibility_lab._run"
        ) as command:
            with self.assertRaisesRegex(ValueError, "does not match the pinned"):
                prepare("preview", Path("source"), Path("python"), Path("postgres"))
        command.assert_not_called()

    def test_occupied_port_is_rejected(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(OSError):
                _require_free_port(listener.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
