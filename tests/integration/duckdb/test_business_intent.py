"""Business expectation publication over real contained stores and application ports."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb import business_intent_repository
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping, ScalarFieldMapping
from impodo.domain.mapping.validation.evidence import MappingValidationStatus
from impodo.domain.shared.access import Actor, ActorIdentity
from impodo.domain.workspace.business_intent import BusinessIntent, BusinessIntentError, BusinessModelIntent, ExpectedCount
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class BusinessIntentStoreTests(ProjectSetupBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.workspace_id, dataset, _key = self._mapping_ready_workspace(scalar_field_count=1)
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)
        definition = DatasetMapping(dataset.dataset_id, "res.partner",
            source_identity_column_keys=(dataset.columns[0].stable_key,),
            target_identity=(IdentityComponentMapping((dataset.columns[0].stable_key,), ("ref",)),),
            fields=(ScalarFieldMapping("field_0000", dataset.columns[1].stable_key),))
        self.mapping, validation = run_local_operation_sync(
            self.context.mapping_workspace.check_definition, self.workspace_id,
            datasets=(definition,), expected_parent_version=None,
            expected_working_draft_version=None, actor=self.actor,
        )
        self.assertNotEqual(validation.status, MappingValidationStatus.INVALID, validation.issues)
        self.service = self.context.business_intents
        self.repository = self.service.repository
        self.path = self.repository.workspace_directory(self.workspace_id) / "workspace-engine.duckdb"
        self.packet = run_local_operation_sync(self.service.inputs, self.workspace_id, actor=self.actor)
        self.intent = BusinessIntent((BusinessModelIntent("res.partner", "update", update_fields=("field_0000",), creates=ExpectedCount(maximum=0)),))

    def save(self, *, version=0, operation=None, intent=None, actor=None):
        return run_local_operation_sync(self.service.save, self.workspace_id, intent=intent or self.intent,
            expected_version=version, expected_binding_hash=self.packet.binding_hash,
            operation_id=operation or str(uuid4()), actor=actor or self.actor)

    def current(self, version=None):
        return run_local_operation_sync(self.service.get, self.workspace_id, actor=self.actor, version=version)

    def pointers(self):
        with self.repository._connect(self.path) as connection:
            return {name: connection.execute(f"SELECT * FROM {name}").fetchall()
                for name in ("mapping_current", "preflight_current", "execution_current")}

    def test_save_is_bounded_metadata_only_and_approves_no_comparison(self):
        self.assertIsNone(self.current())
        pointers = self.pointers()
        before_calls = len(self.readiness_calls)
        with collect_duckdb_request_timings() as timings:
            recorded = self.save()
        self.assertEqual(self.current(), recorded)
        self.assertEqual(recorded.author, self.actor.identity)
        self.assertEqual(recorded.intent.models[0].creates.maximum, 0)
        self.assertEqual(self.pointers(), pointers)
        self.assertEqual(len(self.readiness_calls), before_calls)
        # Registry, Data version, workspace reference and workspace engine.
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        with self.repository._connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_event WHERE event_type = 'BUSINESS_INTENT_RECORDED'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM execution_run").fetchone()[0], 0)

    def test_exact_retries_conflicting_edits_and_historical_revisions(self):
        operation = str(uuid4())
        first = self.save(operation=operation)
        self.assertEqual(self.save(operation=operation), first)
        with self.assertRaisesRegex(BusinessIntentError, "another request"):
            self.save()
        changed = BusinessIntent((replace(self.intent.models[0], updates=ExpectedCount(0, 5)),))
        second = self.save(version=1, intent=changed)
        self.assertEqual(self.current(), second)
        self.assertEqual(self.current(1), first)
        self.assertEqual(self.save(operation=operation), first)
        self.assertEqual(self.current().version, 2)
        with self.assertRaisesRegex(BusinessIntentError, "another request or author"):
            self.save(operation=operation, intent=changed)

    def test_two_concurrent_editors_publish_only_one_expected_revision(self):
        barrier = Barrier(2)
        check = business_intent_repository._require_current_inputs
        def check_together(*args):
            check(*args)
            barrier.wait(timeout=10)
        def attempt(_index):
            try:
                return self.save()
            except BusinessIntentError as error:
                return error
        with patch.object(business_intent_repository, "_require_current_inputs", side_effect=check_together):
            with ThreadPoolExecutor(max_workers=2) as workers:
                outcomes = tuple(workers.map(attempt, range(2)))
        winners = tuple(item for item in outcomes if not isinstance(item, Exception))
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(sum(isinstance(item, BusinessIntentError) for item in outcomes), 1)
        self.assertEqual(self.current(), winners[0])
        self.assertEqual(self.save(operation=winners[0].operation_id), winners[0])
        with self.repository._connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM business_intent_revision").fetchone()[0], 1)

    def test_transaction_rollback_keeps_history_and_audit_aligned(self):
        first = self.save()
        with patch.object(self.repository, "_insert_workspace_audit", side_effect=RuntimeError("interrupted audit")):
            with self.assertRaisesRegex(RuntimeError, "interrupted audit"):
                self.save(version=1)
        self.assertEqual(self.current(), first)
        with self.repository._connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM business_intent_revision").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM audit_event WHERE event_type = 'BUSINESS_INTENT_RECORDED'").fetchone()[0], 1)

    def test_scope_retired_between_read_and_publication_is_rejected(self):
        save = self.repository.save_business_intent
        def retire_then_save(*args, **kwargs):
            with self.repository._connect(self.path) as connection:
                connection.execute("DELETE FROM mapping_current")
            return save(*args, **kwargs)
        with patch.object(self.repository, "save_business_intent", side_effect=retire_then_save):
            with self.assertRaisesRegex(BusinessIntentError, "mapping changed"):
                self.save()
        self.assertIsNone(self.current())

    def test_foreign_author_or_damaged_evidence_cannot_be_reused(self):
        operation = str(uuid4())
        first = self.save(operation=operation)
        other = Actor(ActorIdentity("local", "other", "Other manager"), self.actor.capabilities)
        with self.assertRaisesRegex(BusinessIntentError, "another request or author"):
            self.save(operation=operation, actor=other)
        with self.assertRaisesRegex(BusinessIntentError, "author"):
            self.repository.save_business_intent(self.workspace_id, replace(first, version=2, operation_id=str(uuid4()), author=other.identity), expected_version=1, actor=self.actor)
        with self.repository._connect(self.path) as connection:
            original = first.to_json()
            raw = json.loads(original)
            raw["intent"]["models"][0]["creates"]["maximum"] = 4
            connection.execute("UPDATE business_intent_revision SET revision_json = ?", [json.dumps(raw)])
        with self.assertRaisesRegex(BusinessIntentError, "hash"):
            self.current()
        with self.repository._connect(self.path) as connection:
            connection.execute("UPDATE business_intent_revision SET revision_json = ?", [replace(first, workspace_id=str(uuid4())).to_json()])
        with self.assertRaisesRegex(BusinessIntentError, "another workspace"):
            self.current()
        with self.repository._connect(self.path) as connection:
            connection.execute("UPDATE business_intent_revision SET revision_json = ?", [original])

    def test_version_22_upgrade_does_not_approve_historical_workspaces(self):
        with self.repository._connect(self.path) as connection:
            original = connection.execute("SELECT revision_json FROM mapping_revision").fetchall()
            connection.execute("DROP TABLE business_intent_revision")
            for table in ("business_write_observation", "business_intent_assessment", "business_intent_current", "business_intent_approval", "odoo_company_binding"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("UPDATE schema_version SET version = 22")
        self.assertIsNone(self.current())
        with self.repository._connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], 25)
            self.assertEqual(connection.execute("SELECT revision_json FROM mapping_revision").fetchall(), original)
        first = self.save()
        self.assertEqual(first.version, 1)
