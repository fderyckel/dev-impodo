"""Compare separate child tables using the generated parents' exact ancestry."""

import time
from unittest import TestCase
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.preflight_jobs import PreflightJobManager
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_hierarchy_consumer_preparation as preparation
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo


class HierarchyConsumerComparisonTests(TestCase):
    def exercise(self, major, mode):
        case = preparation.HierarchyConsumerPreparationTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        fixture = case.fixture
        workspace_id = case.hierarchy(major, mode)
        fixture.submit(workspace_id, case.mappings(workspace_id))
        _staging, quality = fixture.prepare(workspace_id)
        self.assertEqual((quality.blocked_count, quality.quarantined_count), (0, 0))
        run_local_operation_sync(fixture.approve, workspace_id)
        fixture.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            fixture.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        target.records.update(
            {
                ("res.company", 7): {"name": "Example"},
                ("x.storage.place", 101): {
                    "name": "Physical",
                    "container_id": False,
                    "company_id": False,
                },
                ("x.storage.place", 102): {
                    "name": "Stock",
                    "container_id": 101,
                    "company_id": 7,
                },
            }
        )

        def read(requirements):
            self.assertFalse(getattr(_read_databases, "connections", None))
            self.assertTrue(
                all(request.domain for request in requirements.record_requests)
            )
            return target.compare(requirements)

        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            report = fixture.context.preflight.compare(
                workspace_id,
                reader=read,
                actor=fixture.actor,
                local_operation=run_local_operation_sync,
            )
        self.assertEqual(
            (report.create_count, report.unchanged_count, report.attention_count),
            (50, 2, 0),
        )
        # Verification and publication retain registry, engine and source owners
        # independently, with no local owner crossing the read-only Odoo call.
        self.assertLessEqual(timings.connection_count, 8)
        self.assertLessEqual(timings.schema_check_count, 7)
        self.assertLessEqual(len(statements), 200)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertFalse(getattr(_read_databases, "connections", None))
        self.assertEqual(target.writes, [])
        self.assertTrue(
            any(
                ["container_id.name", "=", "A024"] in request.domain
                and ["container_id.container_id.name", "=", "Stock"] in request.domain
                and ["company_id.name", "=", "Example"] in request.domain
                for request in target.requests
            )
        )
        current = run_local_operation_sync(
            fixture.context.preflight.current_report, workspace_id
        )
        self.assertEqual(current.run_id, report.run_id)
        manager = PreflightJobManager(fixture.context.migration_projects.repository)
        fixture.context.preflight_jobs = manager
        self.addCleanup(manager.shutdown)
        with (
            patch(
                "impodo.web.routers.preflight._rebind_remote_read_access",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.preflight._read_readiness_snapshots",
                side_effect=lambda _context, _state, requirements, **_kwargs: read(
                    requirements
                ),
            ),
        ):
            response = fixture.client.post(
                f"/workspaces/{workspace_id}/summary/compare",
                data={"csrf_token": fixture.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303, response.text[:500])
            progress_url = response.headers["location"]
            self.assertIn("/preflight/", progress_url)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = fixture.client.get(progress_url + "/status").json()
                if status["status"] not in {"QUEUED", "RUNNING"}:
                    break
                time.sleep(0.01)
            else:
                self.fail("Comparison worker did not finish")
            self.assertEqual(status["status"], "SUCCEEDED", status)
            progress = fixture.client.get(progress_url)
            self.assertEqual(progress.status_code, 200)
            self.assertIn("Comparison saved", progress.text)
        self.assertEqual(target.writes, [])

    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")
