"""Load cost observation preserves durable receipts and remote call order."""

import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.application.workspace.execution.load_observation import (
    LoadObservation,
    bind_load_observation,
)
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.execution.models import ExecutionRunStatus
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.web.load_performance import collect_load_stage_metrics
from tests.application.workspace.execution import test_service as service_fixture
from tests.application.workspace.execution.test_service import (
    _Executor,
    _snapshot,
)


class LoadPhaseBoundaryTests(unittest.TestCase):
    def test_execution_keeps_before_and_after_receipts_with_aggregate_costs(self):
        snapshot = _snapshot()
        service, journal = service_fixture.ExecutionServiceTests()._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        events = []
        observer = LoadObservation(
            str(uuid4()),
            lambda event, facts: events.append((event, facts)),
            stage_metrics=collect_load_stage_metrics,
        )
        progress = []
        with bind_load_observation(observer):
            result = service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
                progress=progress.append,
            )

        self.assertEqual(result.status, ExecutionRunStatus.COMPLETED)
        self.assertEqual(result.committed_count, 3)
        self.assertEqual(journal.run, result)
        self.assertEqual(len(executor.creates), 2)
        self.assertEqual(len(executor.updates), 1)
        self.assertEqual(observer.local_phases["journal_before_call"]["calls"], 3)
        self.assertEqual(observer.local_phases["journal_after_call"]["calls"], 3)
        self.assertGreater(observer.local_phases["progress_publication"]["calls"], 0)
        self.assertEqual(progress[-1], result)
        self.assertEqual(
            len([event for event, _ in events if event == "transport_started"]),
            3,
        )
        self.assertFalse(any(event == "performance_checkpoint" for event, _ in events))
        self.assertFalse(
            any(
                event in {"stage_started", "stage_finished"}
                and facts.get("stage") in observer.local_phases
                for event, facts in events
            )
        )

    def test_failed_journal_receipt_preserves_exception_and_observes_attempt(self):
        snapshot = _snapshot()
        service, journal = service_fixture.ExecutionServiceTests()._service(snapshot)
        executor = _Executor(execution_api_scope(snapshot).semantic_hash)
        observer = LoadObservation(
            str(uuid4()),
            lambda *_args: None,
            stage_metrics=collect_load_stage_metrics,
        )
        error = ValueError("receipt persistence failed")
        with (
            bind_load_observation(observer),
            patch.object(journal, "record_outcomes", side_effect=error),
            self.assertRaises(ValueError) as raised,
        ):
            service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=executor,
                actor=LOCAL_ACTOR,
            )
        self.assertIs(raised.exception, error)
        self.assertEqual(observer.local_phases["journal_before_call"]["calls"], 1)
        self.assertEqual(observer.local_phases["journal_after_call"]["calls"], 1)
        self.assertEqual(len(executor.creates), 1)
        self.assertEqual(executor.updates, [])
