"""Retire derived transfer checks without reading old bodies or losing source."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.execution_repository import ExecutionRepository
from impodo.adapters.duckdb.navigation_repository import WorkspaceNavigationRepository
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.destination_matching import (
    DESTINATION_MATCH_CONTRACT_VERSION,
)
from impodo.domain.workspace.transfer_review import TRANSFER_REVIEW_CONTRACT_VERSION
from impodo.domain.workspace.workbench import (
    SourceFile,
    WorkspaceStateCompatibilityError,
)
from tests.application.workspace.test_scoped_odoo_transfer import ScopedTransferFixture
from tests.integration.web import test_saved_odoo_workspace_projection as saved_fixtures
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class RetiredTransferProjectionTests(unittest.TestCase):
    major = 19

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = ScopedTransferFixture(self.major)
        self.snapshot = self.fixture.compile()
        self.source = SourceFile(
            str(uuid4()), "preserved.csv", "source.csv", 42, "a" * 64, datetime.now(UTC)
        )
        self.state = replace(self.fixture.workspace, source_files=(self.source,))
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.repository = WorkspaceStateRepository(self.database)
        self.repository.initialize_workbench(self.state, actor=LOCAL_ACTOR)
        self.path = (
            self.repository.workspace_directory(self.state.workspace_id)
            / "workspace-engine.duckdb"
        )
        # Use the ordinary source child table, independent of transfer evidence.
        with self.database.connection_factory.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO source_file VALUES (?, ?, ?, ?, ?, ?)",
                [
                    self.source.file_id,
                    self.source.display_name,
                    self.source.stored_name,
                    self.source.size_bytes,
                    self.source.sha256,
                    self.source.received_at.isoformat(),
                ],
            )

    def _replace_evidence(self, **columns):
        with self.database.connection_factory.connect(self.path) as connection:
            connection.execute(
                "UPDATE workspace_projection_cache SET "
                + ", ".join(f"{name} = ?" for name in columns),
                list(columns.values()),
            )

    def _raw(self):
        with self.database.connection_factory.connect(self.path) as connection:
            return (
                connection.execute(
                    "SELECT * FROM workspace_projection_cache"
                ).fetchall(),
                connection.execute(
                    "SELECT * FROM source_file ORDER BY file_id"
                ).fetchall(),
                connection.execute(
                    "SELECT * FROM execution_run ORDER BY run_id"
                ).fetchall(),
                connection.execute(
                    "SELECT * FROM execution_row ORDER BY run_id, ordinal"
                ).fetchall(),
                connection.execute("SELECT * FROM execution_current").fetchall(),
            )

    def _retire_matching(self):
        self._replace_evidence(
            destination_match_plan_json=json.dumps(
                {
                    "contract_version": DESTINATION_MATCH_CONTRACT_VERSION - 1,
                    "retired_body": "never decode",
                }
            ),
            transfer_order_plan_json="not an order body",
            transfer_review_package_json=json.dumps(
                {
                    "contract_version": TRANSFER_REVIEW_CONTRACT_VERSION - 1,
                    "retired_body": "never decode",
                }
            ),
            transfer_review_approval_json="not an approval body",
        )

    def test_retired_matching_is_only_projected_away_with_source_and_raw_rows_unchanged(
        self,
    ):
        with (
            collect_duckdb_request_timings() as current_timings,
            count_duckdb_statements() as current_sql,
        ):
            current = self.repository.get(self.state.workspace_id)
        self.assertIsNone(current.transfer_evidence_recovery_step)
        self._retire_matching()
        before = self._raw()
        with (
            collect_duckdb_request_timings() as retired_timings,
            count_duckdb_statements() as retired_sql,
        ):
            restored = self.repository.get(self.state.workspace_id)
        self.assertEqual(
            restored.transfer_evidence_recovery_step, "destination-matching"
        )
        self.assertIsNone(restored.destination_match_plan)
        self.assertIsNone(restored.transfer_order_plan)
        self.assertIsNone(restored.transfer_review_package)
        self.assertIsNone(restored.transfer_review_approval)
        self.assertEqual(restored.transfer_preflight_report, self.fixture.report)
        self.assertEqual(restored.source_files, (self.source,))
        self.assertFalse(
            restored.transfer_preflight_ready(
                source_selection_hash=self.fixture.selection.content_hash,
                source_schema_hash=self.fixture.schema.content_hash,
            )
        )
        self.assertEqual(self._raw(), before)
        self.assertEqual(
            retired_timings.connection_count, current_timings.connection_count
        )
        self.assertEqual(retired_timings.connection_count, 1)
        self.assertEqual(
            retired_timings.schema_check_count, current_timings.schema_check_count
        )
        self.assertEqual(len(retired_sql), len(current_sql))
        navigated = WorkspaceNavigationRepository(self.database).get_snapshot(
            self.state.workspace_id
        )
        self.assertEqual(
            navigated.workspace_state.transfer_evidence_recovery_step,
            "destination-matching",
        )

    def test_retired_review_keeps_current_matching_and_order_but_cannot_authorize_load(
        self,
    ):
        self._replace_evidence(
            transfer_review_package_json=json.dumps(
                {
                    "contract_version": TRANSFER_REVIEW_CONTRACT_VERSION - 1,
                    "retired_body": [],
                }
            ),
            transfer_review_approval_json="not an approval body",
        )
        before = self._raw()
        restored = self.repository.get(self.state.workspace_id)
        self.assertEqual(restored.transfer_evidence_recovery_step, "transfer-review")
        self.assertEqual(restored.destination_match_plan, self.fixture.match_plan)
        self.assertEqual(
            restored.transfer_order_plan, self.fixture.workspace.transfer_order_plan
        )
        self.assertIsNone(restored.transfer_review_package)
        self.assertIsNone(restored.transfer_review_approval)
        self.assertEqual(restored.transfer_preflight_report, self.fixture.report)
        self.assertFalse(
            restored.transfer_preflight_ready(
                source_selection_hash=self.fixture.selection.content_hash,
                source_schema_hash=self.fixture.schema.content_hash,
            )
        )
        self.assertEqual(self._raw(), before)

    def test_new_authorized_matching_save_replaces_the_retired_projection(self):
        self._retire_matching()
        retired = self.repository.get(self.state.workspace_id)
        refreshed = replace(
            retired,
            destination_match_plan=self.fixture.match_plan,
            transfer_order_plan=None,
            transfer_review_package=None,
            transfer_review_approval=None,
            transfer_preflight_report=None,
            revision=retired.revision + 1,
        )
        self.repository.save(
            refreshed,
            expected_revision=retired.revision,
            event_type="WORKSPACE_DESTINATION_MATCH_CHECKED",
            event_detail="Fresh checks",
            actor=LOCAL_ACTOR,
        )
        reopened = self.repository.get(self.state.workspace_id)
        self.assertIsNone(reopened.transfer_evidence_recovery_step)
        self.assertEqual(reopened.destination_match_plan, self.fixture.match_plan)
        self.assertEqual(reopened.source_files, (self.source,))

    def test_retired_projection_read_preserves_current_in_flight_journal_bytes(self):
        rows = tuple(
            ExecutionRowAttempt(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                field_names=tuple(field.field for field in row.fields),
                proposed_external_id=row.proposed_external_id,
                schedule_component=row.schedule_component,
            )
            for row in self.snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        run = ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=self.state.workspace_id,
            snapshot_hash=self.snapshot.semantic_hash,
            snapshot_root_hash=self.snapshot.root_hash,
            preflight_run_id=self.snapshot.preflight_run_id,
            target_hash=self.snapshot.target_hash,
            target_database=self.snapshot.target_database,
            batch_rows=10,
            status=ExecutionRunStatus.RUNNING,
            started_at=datetime.now(UTC),
            started_by=LOCAL_ACTOR.identity.display_name,
            completed_at=None,
            rows=rows,
        )
        journal = ExecutionRepository(self.database)
        journal.start_run(
            self.state.workspace_id,
            run,
            actor=LOCAL_ACTOR,
            transfer_preflight_hash=self.fixture.report.content_hash,
        )
        pending = next(row for row in run.rows if row.operation == "CREATE")
        journal.record_batch_started(
            self.state.workspace_id,
            run.run_id,
            (
                replace(
                    pending,
                    status=ExecutionRowStatus.IN_FLIGHT,
                    attempt=1,
                    transport_page=0,
                    transport_batch=0,
                    transport_phase="CREATE",
                ),
            ),
        )
        self._retire_matching()
        before = self._raw()
        self.repository.get(self.state.workspace_id)
        reopened = journal.get_current_run(self.state.workspace_id)
        self.assertEqual(reopened.run_id, run.run_id)
        self.assertEqual(reopened.snapshot_hash, self.snapshot.semantic_hash)
        self.assertEqual(reopened.in_flight_count, 1)
        self.assertEqual(self._raw(), before)

    def test_malformed_or_future_envelopes_are_not_retired(self):
        cases = (
            "not json",
            "[]",
            "{}",
            '{"contract_version":true}',
            '{"contract_version":0}',
            '{"contract_version":-1}',
            '{"contract_version":"17"}',
            json.dumps({"contract_version": DESTINATION_MATCH_CONTRACT_VERSION + 1}),
        )
        for body in cases:
            with self.subTest(body=body):
                self._replace_evidence(destination_match_plan_json=body)
                before = self._raw()
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    self.repository.get(self.state.workspace_id)
                self.assertEqual(self._raw(), before)

    def test_current_corrupt_business_bodies_are_not_silently_reset(self):
        for column, version in (
            ("destination_match_plan_json", DESTINATION_MATCH_CONTRACT_VERSION),
            ("transfer_review_package_json", TRANSFER_REVIEW_CONTRACT_VERSION),
        ):
            with self.subTest(column=column):
                self._replace_evidence(
                    destination_match_plan_json=self.fixture.match_plan.to_json(),
                    transfer_review_package_json=self.fixture.package.to_json(),
                )
                self._replace_evidence(
                    **{column: json.dumps({"contract_version": version})}
                )
                with self.assertRaises(ValueError):
                    self.repository.get(self.state.workspace_id)


class Odoo20RetiredTransferProjectionTests(RetiredTransferProjectionTests):
    major = 20


class RetiredSourceRestoreProjectionTests(unittest.TestCase):
    def test_retired_matching_does_not_block_frozen_source_restoration(self):
        fixture = saved_fixtures.SavedOdooWorkspaceProjectionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        for major in (19, 20):
            with self.subTest(major=major):
                source, schema, publication = fixture._capture(major)
                context = fixture.app.state.context
                actor = resolve_local_actor(context)
                database = context.sources.sources._database
                path = (
                    context.workspace_states.repository.workspace_directory(
                        source.workspace_id
                    )
                    / "workspace-engine.duckdb"
                )
                old = json.dumps(
                    {
                        "contract_version": DESTINATION_MATCH_CONTRACT_VERSION - 1,
                        "retired_body": "not interpreted",
                    }
                )
                with database.connection_factory.connect(path) as connection:
                    connection.execute(
                        "UPDATE workspace_projection_cache SET destination_match_plan_json = ?",
                        [old],
                    )
                original_source = context.queries.get_source_selection(
                    source.workspace_id
                )
                with (
                    patch.object(
                        context,
                        "read_identity_probe",
                        side_effect=AssertionError("No source access"),
                    ),
                    patch.object(
                        context,
                        "schema_reader",
                        side_effect=AssertionError("No source access"),
                    ),
                ):
                    restored = fixture._service().prepare_restore(
                        source.workspace_id,
                        actor=actor,
                        operation_id=str(uuid4()),
                        expected_source_selection_hash=publication.source_selection.content_hash,
                        expected_manifest_hashes=tuple(
                            (item.model, item.content_hash)
                            for item in publication.manifests
                        ),
                    )
                self.assertNotEqual(restored.workspace_id, source.workspace_id)
                self.assertIsNone(restored.destination_match_plan)
                self.assertIsNone(restored.transfer_evidence_recovery_step)
                self.assertEqual(
                    context.queries.get_source_selection(source.workspace_id),
                    original_source,
                )
                self.assertEqual(
                    context.queries.get_odoo_schema_catalog(source.workspace_id), schema
                )
                self.assertEqual(
                    tuple(
                        (item.model, item.content_hash)
                        for item in context.odoo_provenance.current_manifests(
                            source.workspace_id, actor=actor
                        )
                    ),
                    tuple(
                        (item.model, item.content_hash)
                        for item in publication.manifests
                    ),
                )
                with database.connection_factory.connect(path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT destination_match_plan_json FROM workspace_projection_cache"
                        ).fetchone()[0],
                        old,
                    )


if __name__ == "__main__":
    unittest.main()
