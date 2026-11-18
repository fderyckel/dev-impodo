"""Bound complete file table acceptance and preserve its publication barriers."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory, _read_databases
from impodo.domain.data_version.models import DataVersionState
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class FileSelectionPerformanceTests(ProjectSetupBrowserTestCase):
    def confirmed_files(self, count=1):
        """Confirm fictional CSV files through the complete browser intake path."""

        context = self.app.state.context
        workspace = self.workspaces.create(
            name="File table acceptance", source_system="CSV"
        )
        prefix = f"/workspaces/{workspace.workspace_id}/"
        uploaded = self.client.post(
            prefix + "files",
            data={"csrf_token": self.csrf, "revision": str(workspace.revision)},
            files=[
                (
                    "source_file",
                    (
                        f"customers-{index}.csv",
                        f"Reference,Value\n00{index},First\n01{index},Second\n".encode(),
                        "text/csv",
                    ),
                )
                for index in range(count)
            ],
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        current = context.queries.get(workspace.workspace_id)
        registered = self._post(
            prefix + "register",
            {"csrf_token": self.csrf, "revision": str(current.revision)},
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        catalogs = context.queries.get_source_catalogs(workspace.workspace_id)
        for catalog in catalogs:
            confirmed = self._post(
                prefix + f"sources/{catalog.file_id}/configure",
                {
                    "csrf_token": self.csrf,
                    "action": "confirm",
                    "encoding": "utf-8",
                    "delimiter": ",",
                    "header_row_0": "1",
                    "selected_0": "1",
                },
            )
            self.assertEqual(confirmed.status_code, 303, confirmed.text)
        return workspace.workspace_id, prefix

    def freeze(self, prefix, names=("customers",), *, budget=(21, 15, 339)):
        """Check full-request budgets, worker placement and physical owner closure."""

        context = self.app.state.context
        owners = []
        publisher = context.sources.snapshot_publisher
        self.assertIsNotNone(publisher)
        original_publish = publisher.publish
        artifacts = context.sources.artifacts
        self.assertIsNotNone(artifacts)
        original_cleanup = artifacts.cleanup_source_snapshots
        original_connect = DuckDbConnectionFactory._connect_with_lock_wait

        def off_event_loop():
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()

        def owners_closed():
            self.assertIsNone(getattr(_read_databases, "connections", None))
            for owner in owners:
                with self.assertRaises(duckdb.ConnectionException):
                    owner.execute("SELECT 1")

        def connect(factory, path, config):
            off_event_loop()
            owner = original_connect(factory, path, config)
            owners.append(owner)
            return owner

        def publish(*args, **kwargs):
            off_event_loop()
            owners_closed()
            return original_publish(*args, **kwargs)

        def cleanup(*args, **kwargs):
            off_event_loop()
            owners_closed()
            return original_cleanup(*args, **kwargs)

        with (
            patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect),
            patch.object(publisher, "publish", publish),
            patch.object(artifacts, "cleanup_source_snapshots", cleanup),
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = self._post(
                prefix + "datasets/freeze",
                {
                    "csrf_token": self.csrf,
                    **{
                        f"dataset_name_{index}": name
                        for index, name in enumerate(names)
                    },
                },
            )
        owners_closed()
        # Four opens cover admission/choices; first acceptance then needs
        # three for the package precheck, ten around source snapshot work,
        # and four for canonical package acceptance/projection. Separate
        # artifact phases release every owner; replays open fresh owners.
        self.assertGreater(timings.connection_count, 0)
        self.assertEqual(timings.connection_count, len(owners))
        self.assertLessEqual(timings.connection_count, budget[0])
        self.assertLessEqual(timings.schema_check_count, budget[1])
        self.assertLessEqual(len(statements), budget[2])
        self.assertEqual(
            sum("duckdb_columns()" in sql for sql in statements),
            timings.schema_check_count,
        )
        self.assertEqual(timings.lock_retry_count, 0)
        return response

    def test_first_acceptance_and_replay_keep_evidence_and_bounded_owners(self):
        workspace_id, prefix = self.confirmed_files()
        context = self.app.state.context
        accepted = self.freeze(prefix)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        self.assertEqual(accepted.headers["location"], prefix + "datasets#tables-ready")
        selection = context.queries.get_source_selection(workspace_id)
        self.assertEqual([item.name for item in selection.datasets], ["customers"])
        self.assertEqual(selection.datasets[0].row_count, 2)
        self.assertEqual(len(selection.datasets[0].columns), 2)
        snapshots = context.sources.sources.get_current_source_snapshots(workspace_id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].physical_selection_hash, selection.content_hash)
        actor = resolve_local_actor(context)
        workspace = context.migration_workspaces.get(workspace_id, actor=actor)
        self.assertEqual(
            context.data_versions.get(workspace.data_version_id, actor=actor).state,
            DataVersionState.FROZEN,
        )
        package = context.data_version_source_projection.packages.repository.get_source_package(
            workspace.data_version_id
        )
        projection = context.data_version_source_projection.projections.repository.get_workspace_source_projection(
            workspace_id
        )
        with patch.object(
            context.sources.snapshot_publisher,
            "publish",
            side_effect=AssertionError("A replay must reuse immutable snapshots"),
        ):
            repeated = self.freeze(prefix, budget=(8, 5, 75))
        self.assertEqual(repeated.status_code, 303, repeated.text)
        self.assertEqual(context.queries.get_source_selection(workspace_id), selection)
        self.assertEqual(
            context.sources.sources.get_current_source_snapshots(workspace_id),
            snapshots,
        )
        self.assertEqual(
            context.data_version_source_projection.packages.repository.get_source_package(
                workspace.data_version_id
            ),
            package,
        )
        self.assertEqual(
            context.data_version_source_projection.projections.repository.get_workspace_source_projection(
                workspace_id
            ),
            projection,
        )
        self.assertEqual(self.readiness_calls, [])
        self.assertEqual(self.schema_calls, [])

    def test_two_files_share_local_phases_and_keep_distinct_snapshots(self):
        workspace_id, prefix = self.confirmed_files(2)
        accepted = self.freeze(prefix, ("customers", "suppliers"), budget=(21, 15, 342))
        self.assertEqual(accepted.status_code, 303, accepted.text)
        context = self.app.state.context
        selection = context.queries.get_source_selection(workspace_id)
        self.assertEqual(
            [item.name for item in selection.datasets], ["customers", "suppliers"]
        )
        snapshots = context.sources.sources.get_current_source_snapshots(workspace_id)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(
            {item.dataset_id for item in snapshots},
            {item.dataset_id for item in selection.datasets},
        )
        self.assertEqual({item.row_count for item in snapshots}, {2})

    def test_rejected_names_preserve_draft_and_accepted_evidence(self):
        workspace_id, prefix = self.confirmed_files()
        context = self.app.state.context
        catalogs = context.queries.get_source_catalogs(workspace_id)
        invalid = self.freeze(prefix, ("Bad name",), budget=(15, 10, 151))
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertIn("Start with a lowercase letter", invalid.text)
        self.assertIsNone(context.queries.get_source_selection(workspace_id))
        self.assertEqual(context.queries.get_source_catalogs(workspace_id), catalogs)
        self.assertEqual(self.snapshot_files(), [])
        accepted = self.freeze(prefix)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        selection = context.queries.get_source_selection(workspace_id)
        snapshots = context.sources.sources.get_current_source_snapshots(workspace_id)
        with patch.object(
            context.sources.snapshot_publisher,
            "publish",
            side_effect=AssertionError("A changed replay cannot replace snapshots"),
        ):
            rejected = self.freeze(prefix, ("renamed",), budget=(12, 8, 115))
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("already frozen", rejected.text)
        self.assertEqual(context.queries.get_source_selection(workspace_id), selection)
        self.assertEqual(
            context.sources.sources.get_current_source_snapshots(workspace_id),
            snapshots,
        )

    def test_changed_inputs_during_snapshot_reject_stale_publication_and_clean_files(
        self,
    ):
        for change_kind in ("catalog", "configuration", "workspace"):
            with self.subTest(change=change_kind):
                self.assert_changed_input_rejected(change_kind)

    def assert_changed_input_rejected(self, change_kind):
        workspace_id, prefix = self.confirmed_files()
        context = self.app.state.context
        publisher = context.sources.snapshot_publisher
        original = publisher.publish
        current_catalogs = None

        def publish(*args, **kwargs):
            nonlocal current_catalogs
            result = original(*args, **kwargs)

            def change():
                actor = resolve_local_actor(context)
                if change_kind == "catalog":
                    catalog = context.queries.get_source_catalogs(workspace_id)[0]
                    context.inspections.sources.save_source_catalog(
                        workspace_id,
                        replace(
                            catalog,
                            inspected_at=catalog.inspected_at + timedelta(seconds=1),
                        ),
                        actor=actor,
                    )
                elif change_kind == "configuration":
                    configuration = context.queries.get_source_configurations(
                        workspace_id
                    )[0]
                    context.sources.sources.save_source_configuration(
                        workspace_id,
                        replace(
                            configuration,
                            confirmed_at=configuration.confirmed_at
                            + timedelta(seconds=1),
                        ),
                        actor=actor,
                    )
                else:
                    current = context.queries.get(workspace_id)
                    context.workspace_states.repository.save(
                        replace(
                            current,
                            revision=current.revision + 1,
                            name="Concurrent source update",
                        ),
                        expected_revision=current.revision,
                        actor=actor,
                        event_type="WORKSPACE_DETAILS_UPDATED",
                        event_detail="Fictional concurrent update",
                    )
                return context.queries.get_source_catalogs(workspace_id)

            current_catalogs = run_local_operation_sync(change)
            return result

        with patch.object(publisher, "publish", publish):
            # The simulated concurrent command adds four physical owners.
            sql_limit = {
                "catalog": 285,
                "configuration": 284,
                "workspace": 256,
            }[change_kind]
            rejected = self.freeze(prefix, budget=(25, 18, sql_limit))
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("changed", rejected.text)
        self.assertIsNotNone(current_catalogs)
        self.assertEqual(
            context.queries.get_source_catalogs(workspace_id), current_catalogs
        )
        self.assertIsNone(context.queries.get_source_selection(workspace_id))
        self.assertEqual(
            context.sources.sources.get_current_source_snapshots(workspace_id),
            (),
        )
        self.assertEqual(self.snapshot_files(), [])

    def test_snapshot_failure_keeps_confirmation_and_removes_unpublished_files(self):
        workspace_id, prefix = self.confirmed_files()
        context = self.app.state.context
        catalogs = context.queries.get_source_catalogs(workspace_id)
        configurations = context.queries.get_source_configurations(workspace_id)
        publisher = context.sources.snapshot_publisher
        original = publisher.publish

        def fail_after_publication(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Fictional snapshot publication failure")

        with patch.object(publisher, "publish", fail_after_publication):
            rejected = self.freeze(prefix, budget=(17, 12, 160))
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("immutable source snapshot", rejected.text)
        self.assertEqual(context.queries.get_source_catalogs(workspace_id), catalogs)
        self.assertEqual(
            context.queries.get_source_configurations(workspace_id), configurations
        )
        self.assertIsNone(context.queries.get_source_selection(workspace_id))
        self.assertEqual(
            context.sources.sources.get_current_source_snapshots(workspace_id), ()
        )
        self.assertEqual(self.snapshot_files(), [])

    def snapshot_files(self):
        return sorted(Path(self.temporary.name).rglob("*.parquet"))
