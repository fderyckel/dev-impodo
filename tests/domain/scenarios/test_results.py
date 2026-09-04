from __future__ import annotations

from datetime import datetime, timezone
import unittest

from impodo.domain.scenarios import ScenarioRunResult, ScenarioRunStatus


HASH = "sha256:" + "2" * 64


class ScenarioRunResultTests(unittest.TestCase):
    def test_portable_result_contains_counts_and_hashes_but_no_values(self) -> None:
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
            prepared_rows=1,
            expected_first_comparison={"CREATE": 1},
            actual_first_comparison={"CREATE": 1},
            target_hash=HASH,
            preflight_hash=HASH,
            odoo_version="19.0",
            module_versions_hash=HASH,
        )

        payload = result.to_portable_dict()
        serialized = result.to_json_bytes().decode("utf-8")

        self.assertEqual(payload["first_comparison"]["actual"]["CREATE"], 1)
        self.assertNotIn("source_values", serialized)
        self.assertNotIn("target_values", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("base_url", serialized)

    def test_failed_result_requires_controlled_failure_details(self) -> None:
        timestamp = datetime(2026, 9, 4, tzinfo=timezone.utc)

        with self.assertRaisesRegex(ValueError, "requires a stage"):
            ScenarioRunResult(
                contract_version=1,
                run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                scenario_id="contact-read-only",
                scenario_hash=HASH,
                fixture_hash=HASH,
                expectation_hash=HASH,
                started_at=timestamp,
                completed_at=timestamp,
                status=ScenarioRunStatus.NEEDS_ATTENTION,
            )


if __name__ == "__main__":
    unittest.main()
