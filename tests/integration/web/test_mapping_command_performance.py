"""Bound complete Match data commands without retaining owners across Odoo work."""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import (
    DuckDbConnectionFactory,
    _read_databases,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.routers import mapping as mapping_routes
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


def command_entries(csrf, dataset, business_key, *, version, action, operation_id):
    """Construct the same two-column mapping against either fixed candidate."""

    identity, value = dataset.columns
    return [
        ["csrf_token", csrf],
        ["action", action],
        ["expected_parent_version", ""],
        ["expected_working_draft_version", str(version)],
        ["operation_id", operation_id],
        ["editable_dataset_id", dataset.dataset_id],
        ["target_model_0", "res.partner"],
        ["mode_0", "upsert"],
        ["on_existing_0", "block"],
        ["source_identity_0", identity.stable_key],
        ["business_key_0", business_key.key_id],
        ["identity_source_0_0", identity.stable_key],
        ["visible_scalar_target_0", "field_0000"],
        ["scalar_value_source_0_0", "source"],
        ["scalar_source_0_0", value.stable_key],
        ["scalar_type_0_0", "string"],
        ["scalar_case_0_0", "preserve"],
        ["scalar_compare_0_0", "1"],
        ["scalar_null_0_0", "distinct"],
    ]


class MappingCommandFixture(ProjectSetupBrowserTestCase):
    def command_fixture(self, major=19, mode="LOCAL"):
        """Use identical source bytes, field choices and stores across candidates."""

        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            odoo_major=major,
            connection_mode=OdooConnectionMode(mode),
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        identity, value = dataset.columns
        initial = run_local_operation_sync(
            context.mapping_workspace.save_working_draft,
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(identity.stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping(
                            target_field="field_0000",
                            source_column_key=value.stable_key,
                            value_source=ScalarValueSource.SOURCE,
                        ),
                    ),
                ),
            ),
            expected_version=None,
            actor=actor,
        )
        entries = command_entries(
            self.csrf,
            dataset,
            business_key,
            version=initial.version,
            action="save_progress",
            operation_id=str(uuid4()),
        )
        return workspace_id, entries

    def post_command(self, workspace_id, entries):
        return self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
            follow_redirects=False,
        )

    def bounded_command(
        self, workspace_id, entries, *, opens, checks=None, sql_limit=None
    ):
        owners = []
        real_connect = DuckDbConnectionFactory._connect_with_lock_wait

        def connect(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            self.assertIsNotNone(getattr(_read_databases, "connections", None))
            owner = real_connect(factory, path, config)
            owners.append(owner)
            return owner

        with (
            patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", connect),
            count_duckdb_statements() as sql,
            collect_duckdb_request_timings() as timings,
        ):
            response = self.post_command(workspace_id, entries)
        for owner in owners:
            with self.assertRaises(duckdb.ConnectionException):
                owner.execute("SELECT 1")
        # Preserve the independent middleware/source-admission lifetimes.
        # Each contiguous command/publication phase needs at most its four
        # registry, linkage, engine and Data-version owners.
        self.assertLessEqual(timings.connection_count, opens)
        if checks is not None:
            self.assertLessEqual(timings.schema_check_count, checks)
        if sql_limit is not None:
            self.assertLessEqual(len(sql), sql_limit)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in sql),
            timings.schema_check_count,
        )
        return response

    def exercise(self, major, mode):
        workspace_id, entries = self.command_fixture(major, mode)
        context = self.app.state.context
        original_selection = context.queries.get_mapping_source_selection(workspace_id)
        saved = self.bounded_command(
            workspace_id, entries, opens=8, checks=6, sql_limit=120
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "committed")
        working = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(working.version, 2)
        self.assertEqual(saved.json()["content_identity"], working.content_hash)

        with patch.object(
            context.mapping_workspace,
            "save_working_draft",
            side_effect=AssertionError("replayed save changed the draft"),
        ):
            replayed = self.bounded_command(
                workspace_id, entries, opens=8, checks=6, sql_limit=60
            )
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json()["operation_id"], saved.json()["operation_id"])
        self.assertEqual(replayed.json()["content_identity"], working.content_hash)

        stale_entries = self.changed_entries(entries, operation_id=str(uuid4()))
        stale = self.bounded_command(
            workspace_id, stale_entries, opens=8, checks=6, sql_limit=94
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["failure_code"], "MAPPING_VERSION_CONFLICT")
        self.assertEqual(stale.json()["current_working_draft_version"], 2)
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id), working
        )

        checked_entries = self.changed_entries(
            entries,
            action="draft",
            expected_working_draft_version="2",
            operation_id=str(uuid4()),
        )
        with self.remote_boundary_probes() as remote_steps:
            # The same fresh inputs serve publication and validation. The check
            # also re-reads the selected run target and reader after Odoo returns.
            # The old path used 176 statements without that selection check.
            checked = self.bounded_command(
                workspace_id, checked_entries, opens=12, checks=9, sql_limit=158
            )
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "committed")
        self.assertEqual(remote_steps, ["access", "supporting"])
        self.assertEqual(
            context.queries.get_mapping_source_selection(workspace_id),
            original_selection,
        )
        revision = context.queries.get_mapping_revision(workspace_id)
        current = context.queries.get_mapping_working_draft(workspace_id)
        confirmed_entries = self.changed_entries(
            checked_entries,
            action="submit",
            expected_parent_version=str(revision.version),
            expected_working_draft_version=str(current.version),
            operation_id=str(uuid4()),
        )
        confirmed = self.bounded_command(
            workspace_id, confirmed_entries, opens=8, checks=6, sql_limit=104
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "committed")
        self.assertEqual(self.readiness_calls, [])

    @staticmethod
    def changed_entries(entries, **updates):
        return [[name, updates.get(name, value)] for name, value in entries]

    @contextmanager
    def remote_boundary_probes(self):
        owners, steps = [], []
        connect = DuckDbConnectionFactory._connect_with_lock_wait
        check_access = mapping_routes.check_matching_access
        capture = mapping_routes._capture_mapping_supporting_values

        def observe_connect(factory, path, config):
            owner = connect(factory, path, config)
            owners.append(owner)
            return owner

        def assert_closed():
            self.assertTrue(owners)
            for owner in owners:
                with self.assertRaises(duckdb.ConnectionException):
                    owner.execute("SELECT 1")

        async def remote_access(*args, **kwargs):
            assert_closed()
            steps.append("access")
            return await check_access(*args, **kwargs)

        def remote_supporting(*args, **kwargs):
            assert_closed()
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            steps.append("supporting")
            return capture(*args, **kwargs)

        with (
            patch.object(
                DuckDbConnectionFactory, "_connect_with_lock_wait", observe_connect
            ),
            patch.object(
                mapping_routes, "check_matching_access", side_effect=remote_access
            ),
            patch.object(
                mapping_routes,
                "_capture_mapping_supporting_values",
                side_effect=remote_supporting,
            ),
        ):
            yield steps


class MappingCommandPerformanceTests(MappingCommandFixture):
    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")
