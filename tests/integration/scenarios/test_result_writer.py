from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.adapters.scenarios import write_scenario_result
from impodo.domain.scenarios import ScenarioRunResult, ScenarioRunStatus


HASH = "sha256:" + "3" * 64


class ScenarioResultWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(__file__).resolve().parents[3] / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"scenario-result-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_atomically_publishes_portable_result(self) -> None:
        timestamp = datetime(2026, 9, 4, tzinfo=timezone.utc)
        result = ScenarioRunResult(
            contract_version=1,
            run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id="contact-read-only",
            scenario_hash=HASH,
            fixture_hash=HASH,
            expectation_hash=HASH,
            started_at=timestamp,
            completed_at=timestamp,
            status=ScenarioRunStatus.PASSED,
        )
        destination = self.root / "nested" / "result.json"

        written = write_scenario_result(result, destination)

        self.assertEqual(written, destination.resolve())
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8"))["status"],
            "PASSED",
        )
        self.assertEqual(list(destination.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
