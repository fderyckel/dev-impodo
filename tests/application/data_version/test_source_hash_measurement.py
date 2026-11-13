"""Observe governed source verification without another byte read."""

from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from impodo.application.data_version.source_files import _source_content_hash
from impodo.application.shared.io_performance import collect_file_io


class SourceHashMeasurementTests(unittest.TestCase):
    def test_original_hash_and_exact_byte_count_are_preserved(self):
        content = b"code,name\nC001,Example\n" * 100_000
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.csv"
            path.write_bytes(content)
            with collect_file_io() as observed:
                actual = _source_content_hash(path)
        self.assertEqual(actual, "sha256:" + sha256(content).hexdigest())
        self.assertEqual(observed.snapshot()["hash_operations"], 1)
        self.assertEqual(observed.snapshot()["hash_bytes"], len(content))
        self.assertNotIn("read_bytes", observed.snapshot())
        self.assertNotIn("source.csv", str(observed.snapshot()))

    def test_missing_source_keeps_original_failure_and_records_no_fake_bytes(self):
        with tempfile.TemporaryDirectory() as temporary, collect_file_io() as observed:
            with self.assertRaises(FileNotFoundError):
                _source_content_hash(Path(temporary) / "missing.csv")
        evidence = observed.snapshot()
        self.assertEqual(evidence["hash_operations"], 1)
        self.assertEqual(evidence["hash_bytes"], 0)
        self.assertEqual(evidence["failed_operations"], 1)


if __name__ == "__main__":
    unittest.main()
