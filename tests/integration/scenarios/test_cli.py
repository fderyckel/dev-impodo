from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.web.composition.cli import build_parser, main


PROFILE = """\
profile:
  id: example
datasets:
  - name: contacts
    source:
      file: contacts.csv
    target:
      model: res.partner
      mode: upsert
    source_identity:
      fields: [reference]
    target_identity:
      components:
        - source_fields: [reference]
          target_fields: [ref]
          type: string
    fields:
      name:
        source: name
        type: string
        required: true
        required_on_create: true
        compare: true
    relations: {}
"""


SCENARIO = """\
contract_version: 1
scenario_id: contact-read-only
purpose: PULL_REQUEST
source:
  mode: FILE
  fixture_set: fixtures/v1
  fixture_hash: sha256:183046c91ff74d8fe3143b47605ed852e9ecb34e8df3a0c4d47455bcd414010e
rules:
  profile: profile.yaml
  profile_hash: sha256:1e348a419037b4bb59ae5d505d1f8363cba9745f0d5c6e7080703f468d1e8d95
destination:
  mode: LOCAL_ODOO
  target_profile: local.contacts
  expected_seed: empty-contacts
execution:
  stop_after: FIRST_COMPARISON
  write_policy: READ_ONLY
expectations:
  prepared_rows: 1
  first_comparison:
    create: 1
    update: 0
    unchanged: 0
    blocked: 0
    ambiguous: 0
"""


class ScenarioCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(__file__).resolve().parents[3] / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"scenario-cli-{uuid4()}"
        fixture = self.root / "fixtures" / "v1"
        fixture.mkdir(parents=True)
        (fixture / "contacts.csv").write_text(
            "reference,name\nC001,Example\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.root / "profile.yaml").write_text(
            PROFILE,
            encoding="utf-8",
            newline="\n",
        )
        self.definition = self.root / "scenario.yaml"
        self.definition.write_text(SCENARIO, encoding="utf-8", newline="\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_top_level_help_exposes_scenario_command(self) -> None:
        self.assertIn("scenario", build_parser().format_help())

    def test_validate_prints_non_secret_canonical_receipt(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                ["scenario", "validate", "--definition", str(self.definition)]
            )

        receipt = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["status"], "VALID")
        self.assertEqual(receipt["scenario_id"], "contact-read-only")
        self.assertEqual(receipt["fixture_file_count"], 1)
        self.assertNotIn("definition", receipt)

    def test_validate_rejects_credentials_without_echoing_the_value(self) -> None:
        self.definition.write_text(
            SCENARIO + "api_key: super-private-value\n",
            encoding="utf-8",
            newline="\n",
        )
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                ["scenario", "validate", "--definition", str(self.definition)]
            )

        self.assertEqual(exit_code, 3)
        self.assertIn("cannot contain credentials", errors.getvalue())
        self.assertNotIn("super-private-value", errors.getvalue())

    def test_run_executes_real_preparation_and_offline_comparison(self) -> None:
        output_path = self.root / "result.json"
        snapshot = (
            Path(__file__).resolve().parents[3]
            / "fixtures"
            / "golden"
            / "target_snapshot.json"
        )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "scenario",
                    "run",
                    "--definition",
                    str(self.definition),
                    "--connector",
                    "snapshot",
                    "--snapshot",
                    str(snapshot),
                    "--output",
                    str(output_path),
                ]
            )

        result = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["preparation"]["prepared_rows"], 1)
        self.assertEqual(result["first_comparison"]["actual"]["CREATE"], 1)
        self.assertEqual(result["write_attempt_count"], 0)
        self.assertNotIn("C001", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
