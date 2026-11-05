"""Bound setup/source commands and keep every file worker outside database scopes."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory, _read_databases
from impodo.application.data_version import source_worker
from impodo.application.data_version.inspection import (
    SourceInspectionError,
    SourceInspectionService,
)
from impodo.application.data_version.intake import validate_source_file_isolated
from impodo.domain.shared.access import Capability
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class FileIntakePerformanceTests(ProjectSetupBrowserTestCase):
    def create_and_upload(self):
        workspace = self.workspaces.create(
            name="Source performance", source_system="CSV"
        )
        prefix = f"/workspaces/{workspace.workspace_id}/"
        uploaded = self.bounded(
            lambda: self.client.post(
                prefix + "files",
                data={"csrf_token": self.csrf, "revision": str(workspace.revision)},
                files=[
                    (
                        "source_file",
                        ("first.csv", b"Reference,Value\n001,First\n", "text/csv"),
                    ),
                    (
                        "source_file",
                        ("second.csv", b"Reference,Value\n002,Second\n", "text/csv"),
                    ),
                ],
                headers=POST_HEADERS,
                follow_redirects=False,
            ),
            14,
            10,
            190,
        )
        self.assertEqual(uploaded.status_code, 303)
        return workspace.workspace_id, prefix

    def register(self, workspace_id, prefix):
        state = self.app.state.context.queries.get(workspace_id)
        result = self.bounded(
            lambda: self._post(
                prefix + "register",
                {
                    "csrf_token": self.csrf,
                    "revision": str(state.revision),
                },
            ),
            14,
            10,
            180,
        )
        self.assertEqual(result.status_code, 303, result.text)
        self.assertEqual(result.headers["location"], prefix + "sources#source-files")

    def test_upload_register_check_and_preview_keep_safety_and_owner_budgets(self):
        inspections = []
        original_inspect = source_worker.inspect_source_file_isolated

        def no_owners():
            self.assertIsNone(getattr(_read_databases, "connections", None))
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()

        def validate(*args, **kwargs):
            no_owners()
            return validate_source_file_isolated(*args, **kwargs)

        def inspect(*args, **kwargs):
            no_owners()
            inspections.append(kwargs["inspector"].__name__)
            return original_inspect(*args, **kwargs)

        with (
            patch(
                "impodo.application.data_version.intake.validate_source_file_isolated",
                validate,
            ),
            patch.object(source_worker, "inspect_source_file_isolated", inspect),
        ):
            workspace_id, prefix = self.create_and_upload()
            files = self.bounded(
                lambda: self.client.get(prefix + "files", follow_redirects=False),
                4,
                3,
                55,
            )
            self.assertIn("Use these files and continue", files.text)
            self.register(workspace_id, prefix)
            self.assertEqual(
                inspections, ["inspect_source_file", "inspect_source_file"]
            )
            context = self.app.state.context
            original = context.queries.get_source_catalogs(workspace_id)
            page = self.bounded(
                lambda: self.client.get(prefix + "sources", follow_redirects=False),
                4,
                3,
                65,
            )
            self.assertEqual(page.status_code, 200)
            self.assertIn("Check files again", page.text)
            inspections.clear()
            checked = self.bounded(
                lambda: self._post(
                    prefix + "sources/inspect", {"csrf_token": self.csrf}
                ),
                10,
                7,
                150,
            )
            self.assertEqual(checked.status_code, 303)
            self.assertEqual(
                inspections, ["_refresh_verified_catalog", "_refresh_verified_catalog"]
            )
            current = context.queries.get_source_catalogs(workspace_id)
            for before, after in zip(original, current, strict=True):
                self.assertEqual(
                    before, replace(after, inspected_at=before.inspected_at)
                )
            inspections.clear()
            catalog = current[0]
            preview = self.bounded(
                lambda: self._post(
                    prefix + f"sources/{catalog.file_id}/configure",
                    {
                        "csrf_token": self.csrf,
                        "action": "preview",
                        "encoding": "utf-8",
                        "delimiter": ",",
                        "header_row_0": "1",
                    },
                ),
                12,
                8,
                160,
            )
            self.assertEqual(preview.status_code, 303)
            self.assertEqual(inspections, ["_refresh_verified_catalog"])
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.readiness_calls, [])

    def test_new_inspector_and_changed_settings_require_full_profiling(self):
        workspace_id, prefix = self.create_and_upload()
        self.register(workspace_id, prefix)
        context = self.app.state.context
        old = context.inspections
        context.inspections = SourceInspectionService(
            old.workspace_states,
            old.sources,
            old.artifacts,
            old.authorization,
            local_operation=run_local_operation_sync,
        )
        called = []
        original = source_worker.inspect_source_file_isolated

        def inspect(*args, **kwargs):
            called.append(kwargs["inspector"].__name__)
            return original(*args, **kwargs)

        with patch.object(source_worker, "inspect_source_file_isolated", inspect):
            result = self._post(prefix + "sources/inspect", {"csrf_token": self.csrf})
            self.assertEqual(result.status_code, 303)
            self.assertEqual(called, ["inspect_source_file", "inspect_source_file"])
            called.clear()
            catalog = context.queries.get_source_catalogs(workspace_id)[0]
            result = self._post(
                prefix + f"sources/{catalog.file_id}/configure",
                {
                    "csrf_token": self.csrf,
                    "action": "preview",
                    "encoding": "utf-8",
                    "delimiter": ";",
                    "header_row_0": "1",
                },
            )
            self.assertEqual(result.status_code, 303)
            self.assertEqual(called, ["inspect_source_file"])
            called.clear()
            result = self._post(prefix + "sources/inspect", {"csrf_token": self.csrf})
            self.assertEqual(result.status_code, 303)
            self.assertEqual(called.count("inspect_source_file"), 1)
            self.assertEqual(called.count("_refresh_verified_catalog"), 1)

    def test_bounded_receipts_fall_back_to_full_profiling_after_eviction(self):
        workspace_id, prefix = self.create_and_upload()
        called = []
        original = source_worker.inspect_source_file_isolated

        def inspect(*args, **kwargs):
            called.append(kwargs["inspector"].__name__)
            return original(*args, **kwargs)

        with patch(
            "impodo.application.data_version.inspection.TRUSTED_INSPECTION_LIMIT", 1
        ):
            self.register(workspace_id, prefix)
            with patch.object(source_worker, "inspect_source_file_isolated", inspect):
                result = self._post(
                    prefix + "sources/inspect", {"csrf_token": self.csrf}
                )
            self.assertEqual(result.status_code, 303)
            self.assertEqual(called.count("inspect_source_file"), 1)
            self.assertEqual(called.count("_refresh_verified_catalog"), 1)

    def test_failed_inspection_does_not_trust_an_unpublished_partial_result(self):
        workspace_id, prefix = self.create_and_upload()
        original = source_worker.inspect_source_file_isolated
        called = []

        def fail_second(*args, **kwargs):
            called.append(kwargs["inspector"].__name__)
            if len(called) == 2:
                raise SourceInspectionError("Fictional second-file failure")
            return original(*args, **kwargs)

        with patch.object(source_worker, "inspect_source_file_isolated", fail_second):
            self.register(workspace_id, prefix)
        context = self.app.state.context
        self.assertEqual(context.queries.get_source_catalogs(workspace_id), ())
        called.clear()

        def inspect(*args, **kwargs):
            called.append(kwargs["inspector"].__name__)
            return original(*args, **kwargs)

        with patch.object(source_worker, "inspect_source_file_isolated", inspect):
            result = self._post(prefix + "sources/inspect", {"csrf_token": self.csrf})
        self.assertEqual(result.status_code, 303)
        self.assertEqual(called, ["inspect_source_file", "inspect_source_file"])

    def test_new_catalog_receipt_during_worker_is_not_overwritten(self):
        workspace_id, prefix = self.create_and_upload()
        self.register(workspace_id, prefix)
        context = self.app.state.context
        original = source_worker.inspect_source_file_isolated
        published = None

        def inspect(*args, **kwargs):
            nonlocal published
            if published is None:

                def change():
                    catalog = context.queries.get_source_catalogs(workspace_id)[0]
                    context.inspections.sources.save_source_catalog(
                        workspace_id,
                        replace(
                            catalog,
                            inspected_at=catalog.inspected_at + timedelta(seconds=1),
                        ),
                        actor=resolve_local_actor(context),
                    )
                    return context.queries.get_source_catalogs(workspace_id)

                published = run_local_operation_sync(change)
            return original(*args, **kwargs)

        with patch.object(source_worker, "inspect_source_file_isolated", inspect):
            result = self._post(prefix + "sources/inspect", {"csrf_token": self.csrf})
        self.assertEqual(result.status_code, 422)
        self.assertIn("changed while", result.text)
        self.assertEqual(context.queries.get_source_catalogs(workspace_id), published)

    def test_changed_bytes_are_rejected_without_replacing_saved_catalogs(self):
        workspace_id, prefix = self.create_and_upload()
        self.register(workspace_id, prefix)
        context = self.app.state.context
        catalogs = context.queries.get_source_catalogs(workspace_id)
        state = context.queries.get(workspace_id)
        actor = resolve_local_actor(context)
        access = context.inspections.authorization.resolve(
            workspace_id, actor=actor, capability=Capability.SOURCE_INSPECT
        )
        with context.inspections.artifacts.materialize_source(
            access.data_version_id, state.source_files[0].stored_name
        ) as path:
            original = path.read_bytes()
            path.write_bytes(original + b"003,Tampered\n")
            try:
                result = self._post(
                    prefix + "sources/inspect", {"csrf_token": self.csrf}
                )
                self.assertEqual(result.status_code, 422)
                self.assertIn("no longer matches", result.text)
                self.assertEqual(
                    context.queries.get_source_catalogs(workspace_id), catalogs
                )
            finally:
                path.write_bytes(original)

    def test_workspace_change_during_worker_keeps_previous_catalogs(self):
        workspace_id, prefix = self.create_and_upload()
        self.register(workspace_id, prefix)
        context = self.app.state.context
        catalogs = context.queries.get_source_catalogs(workspace_id)
        original = source_worker.inspect_source_file_isolated
        changed = False

        def inspect(*args, **kwargs):
            nonlocal changed
            self.assertIsNone(getattr(_read_databases, "connections", None))
            if not changed:

                def change():
                    current = context.workspace_states.repository.get(workspace_id)
                    context.workspace_states.repository.save(
                        replace(
                            current,
                            revision=current.revision + 1,
                            name="Changed during check",
                        ),
                        expected_revision=current.revision,
                        actor=resolve_local_actor(context),
                        event_type="WORKSPACE_DETAILS_UPDATED",
                        event_detail="Fictional concurrent update",
                    )

                run_local_operation_sync(change)
                changed = True
            return original(*args, **kwargs)

        with patch.object(source_worker, "inspect_source_file_isolated", inspect):
            result = self._post(prefix + "sources/inspect", {"csrf_token": self.csrf})
        self.assertEqual(result.status_code, 422)
        self.assertIn("changed while they were being checked", result.text)
        self.assertEqual(context.queries.get_source_catalogs(workspace_id), catalogs)

    def bounded(self, function, opens, checks, sql_limit):
        owners = []
        real_connect = DuckDbConnectionFactory._connect_with_lock_wait

        def connect(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            owner = real_connect(factory, path, config)
            owners.append(owner)
            return owner

        with (
            patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect),
            count_duckdb_statements() as sql,
            collect_duckdb_request_timings() as timings,
        ):
            response = function()
        self.assertLessEqual(timings.connection_count, opens)
        self.assertLessEqual(timings.schema_check_count, checks)
        self.assertLessEqual(len(sql), sql_limit)
        self.assertEqual(timings.lock_retry_count, 0)
        for owner in owners:
            with self.assertRaises(duckdb.ConnectionException):
                owner.execute("SELECT 1")
        return response
