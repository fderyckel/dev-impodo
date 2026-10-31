"""Exercise committed comparison, explicit intent changes, and journal authority."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb import business_intent_assessment_repository
from impodo.domain.workspace.business_intent import BusinessIntent, ExpectedCount
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.hierarchy_execution import compared_hierarchy
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.browser_scenarios import POST_HEADERS


class BusinessIntentEnforcementTests(HierarchyWorkflowFixture):
    def setUp(self):
        super().setUp()
        self.workspace_id, _, (self.target, self.snapshot, self.identities) = compared_hierarchy(self, mode="LOCAL")
        self.repository = self.context.preflight.preflight

    def assessment(self):
        return run_local_operation_sync(self.context.preflight.current_business_intent_assessment,
            self.workspace_id, self.snapshot.semantic_hash)

    def deny_product_creates(self):
        revision = self.context.business_intents.get(self.workspace_id, actor=self.actor)
        intent = BusinessIntent(tuple(replace(rule, creates=ExpectedCount(maximum=0))
            if rule.model == "product.template" else rule for rule in revision.intent.models))
        return self.expect_business_writes(self.workspace_id, intent=intent)

    def execute(self, expected=None):
        return self.context.execution.execute(self.workspace_id,
            expected_snapshot_hash=self.snapshot.semantic_hash,
            expected_intent_assessment_hash=expected,
            executor=self.target, actor=self.actor, **self.identities)

    def assert_no_load(self):
        self.assertEqual(self.target.writes, [])
        self.assertIsNone(self.context.execution.journal.get_current_run(self.workspace_id))

    def test_publication_records_all_supporting_creates_and_bounded_reassessment(self):
        before = self.assessment()
        self.assertTrue(before.ready)
        observed = self.repository.get_business_write_observation(self.workspace_id, self.snapshot.semantic_hash)
        self.assertEqual(sum(item.creates for item in observed.models), 13)
        self.assertEqual(sum(item.updates for item in observed.models), 0)
        remote = len(self.target.requests)
        with collect_duckdb_request_timings() as timings:
            after = run_local_operation_sync(self.context.preflight.reassess_business_intent,
                self.workspace_id, actor=self.actor)
        self.assertEqual(before.content_hash, after.content_hash)
        self.assertEqual(len(self.target.requests), remote)
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)

    def test_deviation_and_unassessed_edit_reject_before_journal_or_writes(self):
        previous = self.assessment()
        self.deny_product_creates()
        self.assertIsNone(self.assessment())
        with self.assertRaises(WorkspaceError):
            self.execute(previous.content_hash)
        self.assert_no_load()
        assessment = run_local_operation_sync(self.context.preflight.reassess_business_intent,
            self.workspace_id, actor=self.actor)
        self.assertFalse(assessment.ready)
        self.assertIn("exceeds maximum", " ".join(assessment.deviations))
        with self.assertRaises(WorkspaceError):
            self.execute(assessment.content_hash)
        self.assert_no_load()

    def test_queued_confirmation_cannot_inherit_a_new_matching_expectation(self):
        old = self.assessment()
        original = self.context.business_intents.get(self.workspace_id, actor=self.actor)
        self.expect_business_writes(self.workspace_id, intent=BusinessIntent(tuple(
            replace(rule, creates=ExpectedCount(maximum=100)) for rule in original.intent.models)))
        new = run_local_operation_sync(self.context.preflight.reassess_business_intent,
            self.workspace_id, actor=self.actor)
        self.assertTrue(new.ready)
        self.assertNotEqual(old.content_hash, new.content_hash)
        with self.assertRaisesRegex(WorkspaceError, "changed after confirmation"):
            run_local_operation_sync(self.context.execution.require_business_intent, self.workspace_id,
                expected_snapshot_hash=self.snapshot.semantic_hash,
                expected_assessment_hash=old.content_hash, actor=self.actor)
        self.assert_no_load()
        with self.assertRaises(WorkspaceError):
            self.context.execution.require_business_intent(self.workspace_id,
                expected_snapshot_hash="sha256:" + "9" * 64,
                expected_assessment_hash=new.content_hash, actor=self.actor)
        self.assert_no_load()

    def test_journal_publication_rechecks_intent_and_rolls_back_approval(self):
        previous = self.assessment()
        journal = self.context.execution.journal
        start = journal.start_run
        def changed_before_start(*args, **kwargs):
            self.deny_product_creates()
            return start(*args, **kwargs)
        with patch.object(journal, "start_run", side_effect=changed_before_start):
            with self.assertRaises(WorkspaceError):
                self.execute(previous.content_hash)
        self.assert_no_load()
        path = journal.workspace_directory(self.workspace_id) / "workspace-engine.duckdb"
        with journal._connect(path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM business_intent_approval").fetchone()[0], 0)

    def test_stale_browser_confirmation_never_constructs_a_writer_or_queues_work(self):
        with patch.object(self.context, "write_executor_factory") as writer:
            response = self.client.post(f"/workspaces/{self.workspace_id}/load", data={
                "csrf_token": self.csrf, "snapshot_hash": self.snapshot.semantic_hash,
                "intent_assessment_hash": "sha256:" + "9" * 64, "batch_rows": "10",
            }, headers=POST_HEADERS, follow_redirects=False)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Business expectations changed after confirmation", response.text)
        writer.assert_not_called()
        self.assertIsNone(self.context.load_jobs.active(self.workspace_id))
        self.assert_no_load()

    def test_edit_racing_journal_publication_cannot_commit_a_stale_approval(self):
        expected = self.assessment().content_hash
        checked, release = Event(), Event()
        read = business_intent_assessment_repository.current_business_assessment
        def read_then_wait(*args):
            assessment = read(*args)
            checked.set()
            if not release.wait(10):
                raise RuntimeError("publication test timed out")
            return assessment
        with patch.object(business_intent_assessment_repository, "current_business_assessment", side_effect=read_then_wait):
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(self.execute, expected)
                try:
                    self.assertTrue(checked.wait(10))
                    self.deny_product_creates()
                finally:
                    release.set()
                with self.assertRaises(WorkspaceError):
                    future.result(timeout=10)
        self.assert_no_load()

    def test_failed_comparison_publication_keeps_original_assessment_and_pointer(self):
        original = self.assessment()
        with patch("impodo.adapters.duckdb.preflight_repository.publish_business_observation", side_effect=RuntimeError("isolated publication failure")):
            with self.assertRaisesRegex(RuntimeError, "isolated publication failure"):
                self.context.preflight.compare(self.workspace_id, reader=self.target.compare, actor=self.actor)
        self.assertEqual(self.assessment(), original)
        self.assert_no_load()

    def test_interrupted_journal_keeps_its_original_intent_after_edit(self):
        original = self.assessment()
        def crash(*_args):
            raise RuntimeError("isolated interruption")
        self.target.after_create = crash
        with self.assertRaisesRegex(RuntimeError, "isolated interruption"):
            self.execute(original.content_hash)
        interrupted = self.context.execution.journal.get_current_run(self.workspace_id)
        self.target.after_create = None
        self.deny_product_creates()
        run_local_operation_sync(self.context.preflight.reassess_business_intent, self.workspace_id, actor=self.actor)
        self.assertFalse(self.assessment().ready)
        self.context.execution.require_business_intent(self.workspace_id,
            expected_snapshot_hash=self.snapshot.semantic_hash, expected_assessment_hash="",
            recovery_run_id=interrupted.run_id, actor=self.actor)
        write = {key: value for key, value in self.identities.items() if key.startswith("write_")}
        recovery = self.context.reconciliation.assess_recovery(self.workspace_id,
            expected_execution_run_id=interrupted.run_id, reader=self.target,
            actor=self.actor, targeted=True, **write)
        completed = self.context.execution.resume(self.workspace_id,
            expected_execution_run_id=interrupted.run_id, recovery=recovery,
            executor=self.target, actor=self.actor, **write)
        self.assertEqual(completed.committed_count, 13)
        self.assertEqual(sum(len(ids) for _, _, ids in self.target.writes), 13)
        before = tuple(self.target.writes)
        with self.assertRaises(WorkspaceError):
            self.execute(original.content_hash)
        self.assertEqual(tuple(self.target.writes), before)


    def test_damaged_original_approval_is_rejected(self):
        import json
        completed = self.execute(self.assessment().content_hash)
        journal = self.context.execution.journal
        path = journal.workspace_directory(self.workspace_id) / "workspace-engine.duckdb"
        with journal._connect(path) as connection:
            raw = json.loads(connection.execute("SELECT approval_json FROM business_intent_approval WHERE run_id = ?", [completed.run_id]).fetchone()[0])
            raw["actor"]["subject_id"] = "another-actor"
            connection.execute("UPDATE business_intent_approval SET approval_json = ? WHERE run_id = ?", [json.dumps(raw), completed.run_id])
        with self.assertRaisesRegex(WorkspaceError, "approval changed"):
            self.repository.require_business_intent_approval(self.workspace_id, completed)
