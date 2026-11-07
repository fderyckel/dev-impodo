"""Reject unsafe manufacturing qualification before target access."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from scripts.qualify_practice_manufacturing import qualify, validate_files


class ManufacturingRunnerGuardsTests(TestCase):
    def options(self, root):
        return SimpleNamespace(
            output_directory=root / "result",
            target_descriptor=root / "missing.json",
            controlled=False,
            allow_write=False,
            expected_major=19,
            mode="LOCAL",
            entrypoint="authoring",
        )

    def test_live_authorization_precedes_descriptor_and_seed_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "scripts.practice_customer_live.PracticeConnection.read"
            ) as read:
                report = qualify(
                    self.options(root), corpus_root=root / "missing-corpus"
                )
            self.assertEqual(report["status"], "FAILED")
            self.assertEqual(report["failure_code"], "ALLOW_WRITE_REQUIRED")
            self.assertEqual(report["phase"], "authorization")
            self.assertEqual(report["phases"], [])
            read.assert_not_called()

    def test_existing_evidence_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            options = self.options(root)
            options.output_directory.mkdir()
            previous = options.output_directory / "result.json"
            previous.write_text("previous evidence", encoding="utf-8")
            with (
                patch("scripts.practice_customer_live.PracticeConnection.read") as read,
                self.assertRaises(FileExistsError),
            ):
                qualify(options)
            self.assertEqual(previous.read_text(), "previous evidence")
            read.assert_not_called()

    def test_changed_input_is_rejected_before_a_target_is_needed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"Code,Name\nP1,Fictional product\n"
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "products.csv",
                                "bytes": len(original),
                                "sha256": hashlib.sha256(original).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "products.csv").write_bytes(original)
            validate_files(root)
            (root / "products.csv").write_bytes(original.replace(b"P1", b"P2"))
            with self.assertRaisesRegex(
                RuntimeError, "MANUFACTURING_INPUT_HASH_DIFFERED"
            ):
                validate_files(root)
