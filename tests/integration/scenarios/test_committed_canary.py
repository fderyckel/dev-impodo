from __future__ import annotations

import json
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.web.composition.cli import main


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "scenarios" / "contact-read-only" / "v1"
WRITE_SCENARIO = ROOT / "scenarios" / "contact-round-trip" / "v1"


class CommittedScenarioCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = ROOT / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"scenario-canary-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_committed_contact_canary_passes_offline(self) -> None:
        output = self.root / "result.json"

        exit_code = main(
            [
                "scenario",
                "run",
                "--definition",
                str(SCENARIO / "scenario.yaml"),
                "--connector",
                "snapshot",
                "--snapshot",
                str(SCENARIO / "target-snapshot.json"),
                "--output",
                str(output),
            ]
        )

        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["write_attempt_count"], 0)

    def test_committed_write_scenario_and_projection_are_valid(self) -> None:
        from impodo.adapters.scenarios import load_scenario

        loaded = load_scenario(WRITE_SCENARIO / "scenario.yaml")

        self.assertEqual(loaded.definition.scenario_id, "contact-round-trip-v1")
        self.assertIsNotNone(loaded.target_projection)
        self.assertEqual(len(loaded.target_projection.records), 1)

    def test_write_scenario_requires_explicit_confirmation_before_key_access(self) -> None:
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                [
                    "scenario",
                    "run",
                    "--definition",
                    str(WRITE_SCENARIO / "scenario.yaml"),
                    "--connector",
                    "json2",
                    "--database",
                    "impodo_scenario_contact",
                    "--evidence-dir",
                    str(self.root / "evidence"),
                    "--output",
                    str(self.root / "result.json"),
                ]
            )

        self.assertEqual(exit_code, 3)
        self.assertIn("--confirm-disposable-write", errors.getvalue())
        self.assertNotIn("api-key", errors.getvalue())

    def test_changed_target_projection_is_rejected_before_a_run(self) -> None:
        from impodo.adapters.scenarios import ScenarioLoadError, load_scenario

        copied = self.root / "write-scenario"
        shutil.copytree(WRITE_SCENARIO, copied)
        (copied / "expected-target.json").write_text(
            '{"contract_version":1,"records":[]}\n',
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(ScenarioLoadError, "projection bytes"):
            load_scenario(copied / "scenario.yaml")


if __name__ == "__main__":
    unittest.main()
