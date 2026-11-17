"""Keep progress and retry eligibility behind successful durable receipts."""

from dataclasses import replace
from unittest import TestCase

from impodo.application.workspace.execution.journal import JournalTransitions
from impodo.application.workspace.execution.progress import ExecutionProgressPublisher
from impodo.domain.execution.load_control import RejectionPolicy
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_scope import execution_api_scope
from impodo.domain.execution_snapshot import FieldIntent
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as fixtures
from tests.application.workspace.execution import (
    test_continuation as continuation_fixtures,
)
from tests.application.workspace.execution.test_progress import _row, _run


class _OutcomeJournal:
    def __init__(self, error=None):
        self.error = error
        self.saved = []

    def record_outcomes(self, workspace_id, run_id, rows):
        if self.error is not None:
            raise self.error
        self.saved.extend(rows)


class JournalTransitionTests(TestCase):
    def test_failed_receipt_changes_neither_progress_nor_committed_eligibility(self):
        planned = _row(0)
        run = _run((planned,))
        publisher = ExecutionProgressPublisher(run, None, row_interval=1)
        recorded = {planned.row_id: planned}
        failure = ValueError("receipt could not be saved")
        journal = _OutcomeJournal(failure)
        transitions = JournalTransitions(journal, recorded, publisher)
        committed = replace(planned, status=ExecutionRowStatus.COMMITTED, odoo_id=1)

        with self.assertRaises(ValueError) as raised:
            transitions.record_outcomes(run.workspace_id, run.run_id, (committed,))

        self.assertIs(raised.exception, failure)
        self.assertFalse(transitions.has_committed_rows)
        self.assertEqual(recorded[planned.row_id], planned)
        self.assertEqual(publisher.snapshot().planned_count, 1)
        self.assertEqual(publisher.snapshot().committed_count, 0)

    def test_successful_receipt_updates_progress_before_recorded_map_replacement(self):
        planned = _row(0)
        run = _run((planned,))
        publisher = ExecutionProgressPublisher(run, None, row_interval=1)
        recorded = {planned.row_id: planned}
        journal = _OutcomeJournal()
        transitions = JournalTransitions(journal, recorded, publisher)
        committed = replace(planned, status=ExecutionRowStatus.COMMITTED, odoo_id=1)

        transitions.record_outcomes(run.workspace_id, run.run_id, (committed,))

        self.assertEqual(journal.saved, [committed])
        self.assertTrue(transitions.has_committed_rows)
        self.assertEqual(recorded[planned.row_id], planned)
        self.assertEqual(publisher.snapshot().planned_count, 0)
        self.assertEqual(publisher.snapshot().committed_count, 1)

    def test_rejected_split_progress_matches_the_journal_for_both_majors_and_modes(
        self,
    ):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    template = fixtures._snapshot()
                    rows = tuple(
                        fixtures._row(
                            dataset="categories",
                            model="product.category",
                            source_row=index + 2,
                            source_identity=(str(index),),
                            business_identity=(str(index),),
                            disposition="CREATE",
                            fields=(
                                FieldIntent(
                                    "name",
                                    "SET_VALUE",
                                    "bad" if index == 2 else str(index),
                                ),
                            ),
                        )
                        for index in range(6)
                    )
                    snapshot = fixtures._with_plan(
                        replace(
                            template,
                            rows=rows,
                            datasets=(template.datasets[0],),
                            target_odoo_version=f"{major}.0",
                            target_module_versions={"base": f"{major}.0.1.0"},
                            counts={
                                "CREATE": 6,
                                "UPDATE": 0,
                                "UNCHANGED": 0,
                                "AMBIGUOUS": 0,
                                "BLOCKED": 0,
                            },
                        )
                    )
                    service, journal = (
                        continuation_fixtures.ContinuationTests()._service(
                            snapshot, mode
                        )
                    )
                    writer = continuation_fixtures._Writer(
                        execution_api_scope(snapshot).semantic_hash, {"bad"}
                    )
                    progress = []

                    result = service.execute(
                        snapshot.workspace_id,
                        expected_snapshot_hash=snapshot.semantic_hash,
                        executor=writer,
                        actor=LOCAL_ACTOR,
                        batch_rows=6,
                        rejection_policy=RejectionPolicy.CONTINUE,
                        progress=progress.append,
                    )

                    self.assertEqual(
                        result.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS
                    )
                    self.assertEqual(result, journal.run)
                    self.assertEqual(
                        (result.committed_count, result.failed_count), (5, 1)
                    )
                    final = progress[-1]
                    self.assertEqual(
                        (final.committed_count, final.failed_count), (5, 1)
                    )
                    self.assertEqual(
                        (final.in_flight_count, final.retry_ready_count), (0, 0)
                    )
                    self.assertEqual(final.created_count, 5)
                    for published in progress:
                        counts = (
                            published.planned_count,
                            published.in_flight_count,
                            published.retry_ready_count,
                            published.committed_count,
                            published.partially_applied_count,
                            published.failed_count,
                            published.blocked_count,
                            published.unknown_count,
                        )
                        self.assertEqual(sum(counts), 6)
                        self.assertTrue(all(count >= 0 for count in counts))
