"""Retain confirmed company context across registry-isolated preparation."""

from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.hierarchy_parent_lookup import HierarchyParentChoice
from impodo.domain.errors import ReadinessError
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.workbench import WorkspaceStateError
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.preparation_worker import create_preparation_worker
from impodo.web.run_commands import _prepare_enqueue
from tests.integration.web import test_existing_hierarchy_parents as existing
from tests.support.browser_scenarios import _spawned_duckdb_locks, _wait_for_preparation
from tests.support.database_probe import count_duckdb_statements


class HierarchyWorkerCompanyTests(TestCase):
    def setUp(self):
        self.fixture = existing.ExistingHierarchyParentWebTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def save_hierarchy(self, major, mode):
        fixture = self.fixture
        workspace_id, form, _schema = fixture.configure(major, mode)
        form.update(hierarchy_company_id="7", hierarchy_start="existing")
        prefix = f"/workspaces/{workspace_id}/derived-entities/hierarchy"
        self.assertEqual(fixture._post(prefix + "/parents", form).status_code, 200)
        form["existing_parent_choice"] = HierarchyParentChoice(
            ("Physical", "Stock"), (None, "Example")
        ).value
        response = fixture._post(prefix + "/save", form)
        self.assertEqual(response.status_code, 303, response.text[:500])
        return workspace_id

    def worker(self, workspace_id):
        fixture = self.fixture
        captured = run_local_operation_sync(
            _prepare_enqueue, fixture.context, workspace_id, actor=fixture.actor
        )
        workspace, _row_count, _name = captured
        self.packet = workspace
        return create_preparation_worker(fixture.temporary.name, workspace=workspace)

    def prepare_in_worker(self, workspace_id):
        fixture = self.fixture
        worker = self.worker(workspace_id)
        # Neither composition nor the worker may consult the browser registry.
        with _spawned_duckdb_locks(Path(fixture.temporary.name) / "registry.duckdb"):
            with (
                worker.sessions.operation_scope(),
                collect_duckdb_request_timings() as timings,
                count_duckdb_statements() as statements,
            ):
                state = worker.workspaces.get(workspace_id)
            self.assertEqual(state.odoo_company.name, "Example")
            # Workspace linkage, accepted Data version, and engine each own
            # one connection; linkage and engine have instrumented validators.
            self.assertEqual(timings.connection_count, 3)
            self.assertEqual(timings.schema_check_count, 2)
            self.assertLessEqual(len(statements), 60)
            manager = fixture.context.preparation_jobs
            job = manager.enqueue(
                workspace_id,
                "Fictional hierarchy",
                2,
                actor=fixture.actor,
                workspace=self.packet,
            )
            completed = _wait_for_preparation(
                fixture.client, f"/workspaces/{workspace_id}/preparation/{job.job_id}"
            )
            self.assertEqual(completed["status"], "SUCCEEDED", completed)
        summary = fixture.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        staging = fixture.context.preparation.staging.get_canonical_staging_run(
            workspace_id, summary.run_id, expected_content_hash=summary.content_hash
        )
        return staging, fixture.context.quality.current_summary(workspace_id)

    def test_saved_company_reaches_registry_isolated_worker(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace_id = self.save_hierarchy(major, mode)
                    with patch.object(
                        self.fixture, "prepare", side_effect=self.prepare_in_worker
                    ):
                        self.fixture.prepare_hierarchy(workspace_id)

    def test_a_real_company_change_still_stops_preparation(self):
        fixture = self.fixture
        workspace_id = self.save_hierarchy(20, "REMOTE")
        fixture.prepare_hierarchy(workspace_id)
        state = fixture.context.queries.get(workspace_id)
        changed = OdooCompanySelection(
            state.odoo_company.target_hash, 8, "Another company"
        )
        fixture.context.workspace_states.repository.save(
            replace(state, odoo_company=changed),
            expected_revision=state.revision,
            actor=fixture.actor,
            event_type="WORKSPACE_COMPANY_UPDATED",
            event_detail="Fictional company changed",
        )
        worker = self.worker(workspace_id)
        with self.assertRaisesRegex(
            ReadinessError, "company for this hierarchy changed"
        ):
            run_local_operation_sync(worker.prepare, workspace_id, actor=fixture.actor)

    def test_missing_and_tampered_company_evidence_fail_closed(self):
        fixture = self.fixture
        workspace_id = self.save_hierarchy(20, "REMOTE")
        worker = self.worker(workspace_id)
        directory = worker.workspaces.workspace_directory(workspace_id)
        path = directory / "workspace-engine.duckdb"
        binding_hash = self.packet.target_company_binding_hash
        with worker.workspaces._connect(path) as connection:
            original = connection.execute(
                "SELECT selection_json FROM odoo_company_binding WHERE binding_hash = ?",
                [binding_hash],
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM odoo_company_binding WHERE binding_hash = ?",
                [binding_hash],
            )
        with self.assertRaisesRegex(WorkspaceStateError, "company evidence is missing"):
            worker.workspaces.get(workspace_id)
        changed = replace(OdooCompanySelection.from_json(original), name="Tampered")
        with worker.workspaces._connect(path) as connection:
            connection.execute(
                "INSERT INTO odoo_company_binding VALUES (?, ?)",
                [binding_hash, changed.to_json()],
            )
        with self.assertRaisesRegex(WorkspaceStateError, "company evidence changed"):
            worker.workspaces.get(workspace_id)

    def test_odoo_source_and_destination_companies_remain_separate(self):
        fixture = self.fixture
        workspace_id = self.save_hierarchy(20, "REMOTE")
        worker = self.worker(workspace_id)
        path = (
            worker.workspaces.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        destination = fixture.context.queries.get(workspace_id).odoo_company
        source = OdooCompanySelection("sha256:" + "e" * 64, 13, "Source company")
        # Exercise the persisted Odoo-source shape with a distinct saved company.
        with worker.workspaces._connect(path) as connection:
            connection.execute(
                "UPDATE workspace_projection_cache SET source_mode = 'ODOO'"
            )
            connection.execute(
                "INSERT INTO odoo_saved_source_company VALUES (1, ?, ?)",
                [source.binding_hash, source.to_json()],
            )
        state = worker.workspaces.get(workspace_id)
        self.assertEqual(state.odoo_company, source)
        self.assertEqual(state.destination_company, destination)
