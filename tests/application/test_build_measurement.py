"""Keep build timing observational and preserve the exact hashing contract."""

from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from impodo.application.shared import build_contract


class ApplicationBuildMeasurementTests(unittest.TestCase):
    def test_measurement_reuses_the_exact_hash_pass_and_counts_only_read_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "impodo"
            package.mkdir()
            files = {"first.py": b"value = 1\n", "second.js": b"const value = 2;\n"}
            for name, content in files.items():
                (package / name).write_bytes(content)
            (package / "ignored.txt").write_text("not part of the build")
            original_read = Path.read_bytes
            read_paths = []

            def read(path):
                read_paths.append(path)
                return original_read(path)

            with (
                patch.object(
                    build_contract,
                    "__file__",
                    str(package / "application/shared/build_contract.py"),
                ),
                patch.object(Path, "read_bytes", read),
                patch.object(build_contract, "perf_counter", side_effect=[1.0, 1.25]),
            ):
                contract, measured = (
                    build_contract._calculate_application_build_contract()
                )
            expected = sha256()
            for name, content in sorted(files.items()):
                relative = name.encode("utf-8")
                expected.update(len(relative).to_bytes(4, "big"))
                expected.update(relative)
                expected.update(len(content).to_bytes(8, "big"))
                expected.update(content)
            self.assertEqual(
                contract.application_build_id, "sha256:" + expected.hexdigest()
            )
            self.assertEqual(read_paths, [(package / name).resolve() for name in sorted(files)])
            self.assertEqual(measured.file_count, 2)
            self.assertEqual(measured.read_bytes, sum(map(len, files.values())))
            self.assertEqual(measured.duration_ms, 250)

    def test_unavailable_clock_keeps_build_verification_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "impodo"
            package.mkdir()
            (package / "first.py").write_bytes(b"value = 1\n")
            with patch.object(
                build_contract,
                "__file__",
                str(package / "application/shared/build_contract.py"),
            ):
                baseline = build_contract.calculate_application_build_contract()
                with patch.object(
                    build_contract,
                    "perf_counter",
                    side_effect=RuntimeError("clock unavailable"),
                ):
                    contract, measured = (
                        build_contract._calculate_application_build_contract()
                    )
            self.assertEqual(contract, baseline)
            self.assertIsNone(measured.duration_ms)
            self.assertEqual(measured.file_count, 1)
            self.assertEqual(measured.read_bytes, len(b"value = 1\n"))


if __name__ == "__main__":
    unittest.main()
