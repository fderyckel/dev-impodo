from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    target_record_read_config,
)
from impodo.adapters.scenarios import ProfileScenarioWorkflow, load_scenario
from impodo.application.scenarios import ScenarioRunner
from impodo.domain.scenarios import ScenarioRunStatus


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "scenarios" / "contact-round-trip" / "v1" / "scenario.yaml"


class _OdooTransport:
    """Small stateful Odoo JSON-2 boundary, not a replacement migration engine."""

    def __init__(self, *, lose_create_response: bool = False) -> None:
        self.records: list[dict[str, object]] = []
        self.write_calls = 0
        self.lose_create_response = lose_create_response

    def __call__(self, url, headers, body, timeout, method):
        del headers, timeout, method
        if url.endswith("/web/version"):
            return 200, {"version": "19.0"}
        payload = json.loads(body) if body else {}
        if url.endswith("/fields_get"):
            return 200, {
                "ref": {
                    "string": "Reference",
                    "type": "char",
                    "required": False,
                    "readonly": False,
                    "relation": None,
                    "relation_field": None,
                },
                "name": {
                    "string": "Name",
                    "type": "char",
                    "required": True,
                    "readonly": False,
                    "relation": None,
                    "relation_field": None,
                },
            }
        if url.endswith("/default_get"):
            return 200, {}
        if url.endswith("/create"):
            self.write_calls += 1
            if self.lose_create_response:
                return 200, True
            identifiers = []
            for values in payload["vals_list"]:
                identifier = len(self.records) + 41
                self.records.append({"id": identifier, **values})
                identifiers.append(identifier)
            return 200, identifiers
        if url.endswith("/search_read"):
            fields = payload.get("fields", [])
            rows = self._matching(payload.get("domain", []))
            return 200, [
                {"id": row["id"], **{name: row.get(name) for name in fields}}
                for row in rows
            ]
        raise AssertionError(f"unexpected fake Odoo endpoint: {url}")

    def _matching(self, domain):
        if not domain:
            return list(self.records)
        clauses = [item for item in domain if isinstance(item, list)]
        rows = list(self.records)
        for field, operator, value in clauses:
            if operator == "=":
                rows = [row for row in rows if row.get(field) == value]
            elif operator == "in":
                rows = [row for row in rows if row.get(field) in value]
        return rows


class ProfileScenarioRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = ROOT / ".tmp"
        temporary_root.mkdir(exist_ok=True)
        self.root = temporary_root / f"scenario-round-trip-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_profile_scenario_uses_real_writer_journal_and_reconciliation(self) -> None:
        loaded = load_scenario(SCENARIO)
        transport = _OdooTransport()
        config = Json2Config(
            base_url="http://127.0.0.1:8069",
            database="impodo_scenario_contact_test",
            api_key="not-logged-test-key",
            connection_mode="LOCAL",
            retries=0,
        )
        connector = Json2ReadConnector(
            target_record_read_config(config),
            transport=transport,
        )
        workflow = ProfileScenarioWorkflow(
            loaded,
            connector=connector,
            connector_factory=lambda: Json2ReadConnector(
                target_record_read_config(config),
                transport=transport,
            ),
            write_config=config,
            evidence_directory=self.root,
            write_transport=transport,
            readback_transport=transport,
        )

        result = ScenarioRunner().run_write(
            loaded.definition,
            fixture_hash=loaded.fixture_hash,
            workflow=workflow,
        )

        self.assertIs(result.status, ScenarioRunStatus.PASSED)
        self.assertEqual(result.actual_execution["committed"], 1)
        self.assertEqual(result.actual_reconciliation["verified"], 1)
        self.assertEqual(result.actual_repeat_comparison["UNCHANGED"], 1)
        self.assertEqual(result.verified_projection_records, 1)
        self.assertEqual(transport.write_calls, 1)
        self.assertTrue((self.root / "execution-journal.json").is_file())
        self.assertTrue((self.root / "execution-snapshot.json").is_file())
        self.assertTrue((self.root / "reconciliation.json").is_file())

    def test_lost_write_response_is_not_blindly_retried(self) -> None:
        loaded = load_scenario(SCENARIO)
        transport = _OdooTransport(lose_create_response=True)
        config = Json2Config(
            base_url="http://127.0.0.1:8069",
            database="impodo_scenario_contact_test",
            api_key="not-logged-test-key",
            connection_mode="LOCAL",
            retries=0,
        )

        def workflow():
            return ProfileScenarioWorkflow(
                loaded,
                connector=Json2ReadConnector(
                    target_record_read_config(config),
                    transport=transport,
                ),
                connector_factory=lambda: Json2ReadConnector(
                    target_record_read_config(config),
                    transport=transport,
                ),
                write_config=config,
                evidence_directory=self.root,
                write_transport=transport,
                readback_transport=transport,
            )

        first = ScenarioRunner().run_write(
            loaded.definition,
            fixture_hash=loaded.fixture_hash,
            workflow=workflow(),
        )
        second = ScenarioRunner().run_write(
            loaded.definition,
            fixture_hash=loaded.fixture_hash,
            workflow=workflow(),
        )

        self.assertIs(first.status, ScenarioRunStatus.UNSAFE_TO_CONTINUE)
        self.assertIs(second.status, ScenarioRunStatus.UNSAFE_TO_CONTINUE)
        self.assertEqual(transport.write_calls, 1)
        journal = json.loads(
            (self.root / "execution-journal.json").read_text(encoding="utf-8")
        )
        self.assertEqual(journal["status"], "OUTCOME_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
