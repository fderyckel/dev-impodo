"""Bound target commands while keeping Odoo probes outside local owners."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.adapters.odoo.local_stack import (
    LocalStackCheck,
    LocalStackStatus,
    ReadinessLevel,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.routers.target import _render_target
from impodo.web.target_credentials import TargetCredentialRole, get_target_credential
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _browser_schema,
)
from tests.support.database_probe import count_duckdb_statements


class TargetCommandPerformanceTests(ProjectSetupBrowserTestCase):
    def test_odoo19_local_commands_release_owners_before_probes(self):
        self.exercise(19, OdooConnectionMode.LOCAL)

    def test_odoo20_local_commands_release_owners_before_probes(self):
        self.exercise(20, OdooConnectionMode.LOCAL)

    def test_odoo19_remote_commands_release_owners_before_probes(self):
        self.exercise(19, OdooConnectionMode.REMOTE)

    def test_odoo20_remote_commands_release_owners_before_probes(self):
        self.exercise(20, OdooConnectionMode.REMOTE)

    def exercise(self, major, mode):
        workspace_id, _, _ = self._mapping_ready_workspace(
            scalar_field_count=1, odoo_major=major, connection_mode=mode
        )
        context = self.app.state.context
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        selection = context.queries.get_source_selection(workspace_id)
        identity_probe = context.read_identity_probe
        update_target = context.workspace_states.update_target
        target_url = f"/workspaces/{workspace_id}/target"
        probe_calls = []
        update_calls = []
        rendered_errors = []

        def fingerprint_probe(workspace_state, api_key):
            self.assert_probe_boundary()
            probe_calls.append((workspace_state.odoo_connection_mode, api_key))
            return replace(
                _browser_schema(workspace_state).fingerprint,
                odoo_version=f"{major}.0",
                module_versions={"base": f"{major}.0.1.0"},
            )

        def checked_identity(*args, **kwargs):
            self.assert_probe_boundary()
            return identity_probe(*args, **kwargs)

        def update_off_loop(*args, **kwargs):
            self.assert_local_boundary()
            update_calls.append(kwargs["expected_revision"])
            return update_target(*args, **kwargs)

        def render_off_loop(*args, **kwargs):
            self.assert_local_boundary()
            rendered_errors.append(kwargs.get("status_code"))
            return _render_target(*args, **kwargs)

        self._replace_connection_probes(
            fingerprint_probe=fingerprint_probe, identity_probe=checked_identity
        )
        local_status = replace(
            LocalStackStatus.unconfigured(),
            checks=(
                LocalStackCheck(
                    key="api",
                    label="Odoo data access",
                    level=ReadinessLevel.READY,
                    message=f"Odoo {major} read access succeeded.",
                ),
            ),
        )
        # The ordinary-key local path and the remote path share the same
        # fixed connection-test service. The local assistant's subprocess
        # readiness journey has separate coverage in test_local_stack.
        with (
            patch.object(context.workspace_states, "update_target", update_off_loop),
            patch("impodo.web.routers.target._render_target", render_off_loop),
            patch.object(context.local_stack, "get", return_value=local_status),
        ):
            state = context.queries.get(workspace_id)
            form = {
                "csrf_token": self.csrf,
                "revision": str(state.revision),
                "odoo_connection_mode": mode.value,
                "odoo_base_url": state.odoo_base_url,
                "odoo_database": state.odoo_database,
                "read_api_key": "read-secret",
                "read_api_key_storage": "session",
                "action": "test",
            }
            tested = self.bounded(
                lambda: self._post(target_url, form), opens=10, schemas=7, sql=100
            )
            self.assertEqual(tested.status_code, 303, tested.text)
            expected_location = target_url
            if mode is OdooConnectionMode.REMOTE:
                expected_location += "#remote-connection-status"
            self.assertEqual(tested.headers["location"], expected_location)
            self.assertEqual(probe_calls, [(mode, "read-secret")])
            state = context.queries.get(workspace_id)
            self.assertTrue(context.remote_connections.get(state).ready)
            self.assertEqual(
                get_target_credential(
                    self.secrets, state, TargetCredentialRole.READ
                ).secret,
                "read-secret",
            )
            self.assertIsNone(
                get_target_credential(self.secrets, state, TargetCredentialRole.WRITE)
            )
            continuation = {
                **form,
                "revision": str(state.revision),
                "action": "continue",
            }
            continuation.pop("read_api_key")
            continued = self.bounded(
                lambda: self._post(target_url, continuation),
                opens=10,
                schemas=7,
                sql=100,
            )
            self.assertEqual(continued.status_code, 303, continued.text)
            self.assertEqual(
                continued.headers["location"], f"/workspaces/{workspace_id}/schema"
            )
            current = context.queries.get(workspace_id)
            rejected = self.bounded(
                lambda: self._post(
                    target_url,
                    {**continuation, "revision": str(current.revision - 1)},
                ),
                opens=14,
                schemas=10,
                sql=130,
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(context.queries.get(workspace_id), current)
            self.assertEqual(probe_calls, [(mode, "read-secret")])
            self.assertEqual(rendered_errors, [422])
            self.assertEqual(len(update_calls), 3)

        self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), schema)
        self.assertEqual(context.queries.get_source_selection(workspace_id), selection)
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.readiness_calls, [])

    def assert_probe_boundary(self):
        with self.assertRaises(RuntimeError):
            asyncio.get_running_loop()
        self.assertFalse(hasattr(_read_databases, "connections"))

    def assert_local_boundary(self):
        with self.assertRaises(RuntimeError):
            asyncio.get_running_loop()
        self.assertTrue(hasattr(_read_databases, "connections"))

    def bounded(self, callback, *, opens, schemas, sql):
        with (
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            response = callback()
        # Admission, the current-state read, and target preparation retain
        # their distinct owner sets. Recovery adds its own independent scope;
        # the ceilings must not grow with the number of repository reads.
        self.assertLessEqual(timings.connection_count, opens)
        self.assertLessEqual(timings.schema_check_count, schemas)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertLessEqual(len(statements), sql)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        return response
