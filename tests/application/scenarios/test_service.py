from __future__ import annotations

import unittest

from impodo.application.scenarios import (
    ScenarioComparisonEvidence,
    ScenarioExecutionEvidence,
    ScenarioPreparationEvidence,
    ScenarioProjectionEvidence,
    ScenarioReconciliationEvidence,
    ScenarioRunner,
)
from impodo.domain.scenarios import (
    ScenarioDefinition,
    ScenarioReasonCode,
    ScenarioRunStatus,
)


HASH = "sha256:" + "1" * 64


def _definition(
    *,
    outcome: str = "PASS",
    blocked: int = 0,
    write: bool = False,
) -> ScenarioDefinition:
    return ScenarioDefinition.model_validate(
        {
            "contract_version": 1,
            "scenario_id": "contact-read-only",
            "purpose": "PULL_REQUEST",
            "source": {
                "mode": "FILE",
                "fixture_set": "fixtures/v1",
                "fixture_hash": HASH,
            },
            "rules": {"profile": "profile.yaml", "profile_hash": HASH},
            "destination": {
                "mode": "LOCAL_ODOO",
                "target_profile": "local.contacts",
                "expected_seed": "empty-contacts",
            },
            "execution": {
                "stop_after": "RECONCILIATION" if write else "FIRST_COMPARISON",
                "write_policy": (
                    "DISPOSABLE_SCENARIO_ONLY" if write else "READ_ONLY"
                ),
            },
            "expectations": {
                "expected_outcome": outcome,
                **(
                    {
                        "target_projection": "expected.json",
                        "target_projection_hash": HASH,
                        "reconciliation": {
                            "verified": 2,
                            "fallout": 0,
                            "outcome_unknown": 0,
                        },
                    }
                    if write
                    else {}
                ),
                "prepared_rows": 2,
                "first_comparison": {
                    "create": 2 - blocked,
                    "update": 0,
                    "unchanged": 0,
                    "blocked": blocked,
                    "ambiguous": 0,
                },
            },
        }
    )


class _Workflow:
    def __init__(
        self,
        *,
        prepared_rows: int = 2,
        counts: dict[str, int] | None = None,
    ) -> None:
        self.prepared_rows = prepared_rows
        self.counts = counts or {
            "CREATE": 2,
            "UPDATE": 0,
            "UNCHANGED": 0,
            "BLOCKED": 0,
            "AMBIGUOUS": 0,
        }
        self.prepare_calls = 0
        self.compare_calls = 0

    def prepare(self, definition):
        del definition
        self.prepare_calls += 1
        return ScenarioPreparationEvidence(
            prepared_rows=self.prepared_rows,
            source_issues=0,
            state=object(),
        )

    def compare(self, definition, preparation):
        del definition, preparation
        self.compare_calls += 1
        return ScenarioComparisonEvidence(
            counts=self.counts,
            target_hash=HASH,
            preflight_hash=HASH,
            odoo_version="19.0",
            module_versions_hash=HASH,
        )


class _WriteWorkflow(_Workflow):
    def __init__(self, *, unknown: int = 0) -> None:
        super().__init__()
        self.unknown = unknown
        self.execute_calls = 0
        self.reconcile_calls = 0
        self.projection_calls = 0

    def execute(self, definition, preparation, comparison):
        del definition, preparation, comparison
        self.execute_calls += 1
        return ScenarioExecutionEvidence(
            committed=2 - self.unknown,
            failed=0,
            partially_applied=0,
            outcome_unknown=self.unknown,
            snapshot_hash=HASH,
            state=object(),
        )

    def reconcile(self, definition, preparation, execution):
        del definition, preparation, execution
        self.reconcile_calls += 1
        return ScenarioReconciliationEvidence(
            verified=2,
            fallout=0,
            outcome_unknown=0,
            result_hash=HASH,
            state=object(),
        )

    def verify_projection(self, definition, preparation, reconciliation):
        del definition, preparation, reconciliation
        self.projection_calls += 1
        return ScenarioProjectionEvidence(
            expected_records=2,
            verified_records=2,
            difference_count=0,
        )


class ScenarioRunnerTests(unittest.TestCase):
    def runner(self) -> ScenarioRunner:
        return ScenarioRunner(run_id_factory=lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def test_matching_read_only_scenario_passes_with_zero_writes(self) -> None:
        workflow = _Workflow()

        result = self.runner().run_read_only(
            _definition(),
            fixture_hash=HASH,
            workflow=workflow,
        )

        self.assertIs(result.status, ScenarioRunStatus.PASSED)
        self.assertEqual(result.write_attempt_count, 0)
        self.assertEqual(result.actual_first_comparison["CREATE"], 2)
        self.assertEqual(workflow.prepare_calls, 1)
        self.assertEqual(workflow.compare_calls, 1)

    def test_preparation_mismatch_stops_before_target_contact(self) -> None:
        workflow = _Workflow(prepared_rows=1)

        result = self.runner().run_read_only(
            _definition(), fixture_hash=HASH, workflow=workflow
        )

        self.assertIs(result.status, ScenarioRunStatus.NEEDS_ATTENTION)
        self.assertIs(
            result.reason_code,
            ScenarioReasonCode.PREPARATION_EXPECTATION_MISMATCH,
        )
        self.assertEqual(workflow.compare_calls, 0)

    def test_comparison_mismatch_is_retained_as_attention(self) -> None:
        workflow = _Workflow(
            counts={
                "CREATE": 1,
                "UPDATE": 0,
                "UNCHANGED": 1,
                "BLOCKED": 0,
                "AMBIGUOUS": 0,
            }
        )

        result = self.runner().run_read_only(
            _definition(), fixture_hash=HASH, workflow=workflow
        )

        self.assertIs(result.status, ScenarioRunStatus.NEEDS_ATTENTION)
        self.assertIs(
            result.reason_code,
            ScenarioReasonCode.COMPARISON_EXPECTATION_MISMATCH,
        )

    def test_expected_block_passes_only_with_matching_blocker(self) -> None:
        workflow = _Workflow(
            counts={
                "CREATE": 1,
                "UPDATE": 0,
                "UNCHANGED": 0,
                "BLOCKED": 1,
                "AMBIGUOUS": 0,
            }
        )

        result = self.runner().run_read_only(
            _definition(outcome="EXPECTED_BLOCK", blocked=1),
            fixture_hash=HASH,
            workflow=workflow,
        )

        self.assertIs(result.status, ScenarioRunStatus.EXPECTED_BLOCK_PASSED)
        self.assertEqual(result.write_attempt_count, 0)

    def test_write_definition_is_rejected_before_workflow_calls(self) -> None:
        workflow = _Workflow()

        result = self.runner().run_read_only(
            _definition(write=True), fixture_hash=HASH, workflow=workflow
        )

        self.assertIs(result.status, ScenarioRunStatus.UNSAFE_TO_CONTINUE)
        self.assertIs(
            result.reason_code,
            ScenarioReasonCode.UNSUPPORTED_SCENARIO_STAGE,
        )
        self.assertEqual(workflow.prepare_calls, 0)
        self.assertEqual(workflow.compare_calls, 0)

    def test_write_scenario_requires_execution_reconciliation_and_projection(self) -> None:
        workflow = _WriteWorkflow()

        result = self.runner().run_write(
            _definition(write=True), fixture_hash=HASH, workflow=workflow
        )

        self.assertIs(result.status, ScenarioRunStatus.PASSED)
        self.assertEqual(result.actual_execution["committed"], 2)
        self.assertEqual(result.actual_reconciliation["verified"], 2)
        self.assertEqual(result.verified_projection_records, 2)
        self.assertEqual(result.write_attempt_count, 2)
        self.assertEqual(workflow.execute_calls, 1)
        self.assertEqual(workflow.reconcile_calls, 1)
        self.assertEqual(workflow.projection_calls, 1)

    def test_unknown_write_outcome_stops_before_reconciliation(self) -> None:
        workflow = _WriteWorkflow(unknown=1)

        result = self.runner().run_write(
            _definition(write=True), fixture_hash=HASH, workflow=workflow
        )

        self.assertIs(result.status, ScenarioRunStatus.UNSAFE_TO_CONTINUE)
        self.assertIs(result.reason_code, ScenarioReasonCode.UNSAFE_WRITE_OUTCOME)
        self.assertEqual(workflow.reconcile_calls, 0)


if __name__ == "__main__":
    unittest.main()
