from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from uc_migration_profiler.cli import build_parser, main
from uc_migration_profiler.reporting import (
    MANIFEST_NAME,
    WORKBOOK_NAME,
    write_preflight_outputs,
)
from test_engine import ROOT, golden_result


class CliTests(unittest.TestCase):
    def test_help_lists_read_only_commands(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("profile", help_text)
        self.assertIn("snapshot-metadata", help_text)
        self.assertIn("snapshot-records", help_text)
        self.assertIn("preflight", help_text)
        self.assertNotIn("import", help_text.casefold())

    def test_profile_command_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "prepared.json"
            exit_code = main(
                [
                    "profile",
                    "--profile",
                    str(ROOT / "profiles/examples/bom_v1.yaml"),
                    "--input",
                    str(ROOT / "examples/bom"),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text())
            self.assertEqual(len(payload["records"]), 3)
            self.assertNotIn("odoo_id", output.read_text())


@unittest.skipUnless(
    os.environ.get("UC_RUN_WORKBOOK_TESTS") == "1",
    "set UC_RUN_WORKBOOK_TESTS=1 for artifact-tool workbook integration",
)
class WorkbookIntegrationTests(unittest.TestCase):
    def test_review_workbook_and_manifest_are_generated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            manifest_path, workbook_path = write_preflight_outputs(
                golden_result(), output
            )
            self.assertEqual(manifest_path.name, MANIFEST_NAME)
            self.assertEqual(workbook_path.name, WORKBOOK_NAME)
            manifest_text = manifest_path.read_text()
            self.assertNotIn("odoo_id", manifest_text)
            with zipfile.ZipFile(workbook_path) as archive:
                workbook_xml = archive.read("xl/workbook.xml").decode()
            for sheet_name in (
                "Dashboard",
                "Target Environment",
                "Dataset Summary",
                "Proposed Creates",
                "Proposed Updates",
                "Field Differences",
                "Unchanged",
                "Ambiguous Matches",
                "Blocked Records",
                "Reference Resolution",
                "Source Issues",
                "Metadata Coverage",
            ):
                self.assertIn(sheet_name, workbook_xml)


if __name__ == "__main__":
    unittest.main()

