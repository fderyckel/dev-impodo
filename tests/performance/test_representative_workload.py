from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from impodo.domain.compiler import compile_profile_document
from impodo.adapters.artifacts.profile_loader import load_profile
from impodo.application.data_version.source_files import prepare_sources
from scripts.p4_representative_runner import (
    PROFILE,
    TOTAL_ROWS,
    _connection_mode,
    _rate,
    _seed_scope,
    _write_sources,
    main,
)


class P4RepresentativeFixtureTests(unittest.TestCase):
    def test_sanitized_fixture_compiles_to_the_expected_practical_scope(self):
        with tempfile.TemporaryDirectory(prefix="impodo-p4-test-") as directory:
            _write_sources(Path(directory))
            plan = compile_profile_document(load_profile(PROFILE))
            prepared = prepare_sources(plan, directory)

        self.assertEqual(len(prepared.records), TOTAL_ROWS)
        self.assertEqual(prepared.issues, ())
        self.assertEqual(
            {record.target_model for record in prepared.records},
            {"product.category", "res.partner", "product.template"},
        )

    def test_runner_derives_remote_mode_only_for_non_loopback_targets(self):
        self.assertEqual(_connection_mode("http://127.0.0.1:8069"), "LOCAL")
        self.assertEqual(_connection_mode("http://[::1]:8069"), "LOCAL")
        self.assertEqual(
            _connection_mode("https://odoo-onprem.example.test"),
            "REMOTE",
        )

    def test_seed_capability_is_fixed_to_the_three_fixture_models(self):
        scope = _seed_scope()

        self.assertEqual(
            tuple(item.model for item in scope.models),
            ("product.category", "product.template", "res.partner"),
        )
        self.assertEqual(scope.lookup_fields("res.partner"), frozenset())
        self.assertEqual(
            scope.write_fields("product.template"),
            frozenset(
                {"active", "categ_id", "default_code", "list_price", "name"}
            ),
        )

    def test_observed_throughput_is_reported_without_a_release_threshold(self):
        self.assertEqual(_rate(145, 2.0), 72.5)
        self.assertEqual(_rate(145, 0.0), 0.0)

    def test_runner_refuses_a_non_disposable_database_before_connecting(self):
        with patch(
            "sys.argv",
            [
                "p4_representative_runner.py",
                "--database",
                "production",
                "--api-key-file",
                "/does/not/matter",
            ],
        ):
            with self.assertRaisesRegex(SystemExit, "impodo_p4_"):
                main()


if __name__ == "__main__":
    unittest.main()
