from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from impodo.domain.compiler import compile_profile_document
from impodo.profile import load_profile
from impodo.source import prepare_sources
from scripts.p4_representative_runner import PROFILE, TOTAL_ROWS, _write_sources


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


if __name__ == "__main__":
    unittest.main()
