"""Browser evidence for Odoo-source revisions and lifecycle recovery."""

from __future__ import annotations

from unittest.mock import patch

from impodo.domain.data_version.models import DataVersionState
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _BrowserOdooCaptureGateway,
    re,
    replace,
)
from tests.support.identity import resolve_local_actor


class OdooSourceLifecycleBrowserTests(ProjectSetupBrowserTestCase):
    def test_changing_a_frozen_odoo_source_starts_a_clean_successor(self) -> None:
        context = self.app.state.context
        predecessor_state, _schema = self._frozen_odoo_source_workspace()
        predecessor_workspace = context.migration_workspaces.get(
            predecessor_state.workspace_id,
            actor=resolve_local_actor(context),
        )
        predecessor_data = context.data_versions.get(
            predecessor_workspace.data_version_id,
            actor=resolve_local_actor(context),
        )
        warning_page = self.client.get(
            f"/workspaces/{predecessor_state.workspace_id}/target"
        )
        self.assertIn(
            "Changing this source starts a new Data version.",
            warning_page.text,
        )

        changed_form = {
            "csrf_token": self.csrf,
            "revision": str(predecessor_state.revision),
            "odoo_connection_mode": "REMOTE",
            "odoo_base_url": "https://replacement.example.test",
            "odoo_database": "replacement",
            "read_api_key": "replacement-read-secret",
            "action": "test",
        }
        restart_handoff = self._post(
            f"/workspaces/{predecessor_state.workspace_id}/target",
            changed_form,
        )
        self.assertEqual(restart_handoff.status_code, 307)
        changed = self._post(
            restart_handoff.headers["location"],
            changed_form,
        )

        self.assertEqual(changed.status_code, 303, changed.text)
        successor_workspace_id = re.search(
            r"/workspaces/([^/]+)/target",
            changed.headers["location"],
        ).group(1)
        self.assertNotEqual(
            successor_workspace_id,
            predecessor_state.workspace_id,
        )
        self.assertEqual(
            changed.headers["location"],
            f"/workspaces/{successor_workspace_id}/target#remote-connection-status",
        )
        successor_workspace = context.migration_workspaces.get(
            successor_workspace_id,
            actor=resolve_local_actor(context),
        )
        successor_data = context.data_versions.get(
            successor_workspace.data_version_id,
            actor=resolve_local_actor(context),
        )
        successor_package = context.data_version_source_projection.packages.repository.get_source_package(
            successor_data.data_version_id
        )
        self.assertIs(predecessor_data.state, DataVersionState.FROZEN)
        self.assertIs(successor_data.state, DataVersionState.DRAFT)
        self.assertEqual(
            successor_data.parent_data_version_id,
            predecessor_data.data_version_id,
        )
        self.assertEqual(successor_data.version_number, 2)
        self.assertEqual(successor_package.datasets, ())
        self.assertIsNotNone(
            context.queries.get_source_selection(predecessor_state.workspace_id)
        )
        self.assertIsNone(context.queries.get_source_selection(successor_workspace_id))
        successor_state = context.queries.get(successor_workspace_id)
        self.assertEqual(
            successor_state.odoo_base_url,
            "https://replacement.example.test",
        )
        self.assertEqual(successor_state.odoo_database, "replacement")

        continued = self._post(
            f"/workspaces/{successor_workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": str(successor_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://replacement.example.test",
                "odoo_database": "replacement",
                "action": "save",
            },
        )
        self.assertEqual(continued.status_code, 303, continued.text)
        self.assertEqual(
            continued.headers["location"],
            f"/workspaces/{successor_workspace_id}/schema",
        )

    def test_frozen_stage_three_restart_rebuilds_schema_in_a_successor(self) -> None:
        context = self.app.state.context
        predecessor_state, _schema = self._frozen_odoo_source_workspace()
        predecessor_workspace = context.migration_workspaces.get(
            predecessor_state.workspace_id,
            actor=resolve_local_actor(context),
        )
        invalid_restart = self._post(
            f"/projects/{predecessor_workspace.project_id}/odoo-source-restarts/"
            f"{predecessor_state.workspace_id}?resume=invalid",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(invalid_restart.status_code, 404)
        predecessor_data = context.data_versions.get(
            predecessor_workspace.data_version_id,
            actor=resolve_local_actor(context),
        )
        changed_state = context.workspace_states.update_target(
            predecessor_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=predecessor_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://changed-before-fix.example.test",
            odoo_database="changed_before_fix",
            intended_applications=(),
            intended_models=("res.partner",),
        )
        credential = store_target_credential(
            context.secret_store,
            changed_state,
            TargetCredentialRole.READ,
            "changed-source-secret",
            persistent=False,
        )
        audit_stored_target_credential(
            context.workspace_states,
            changed_state,
            TargetCredentialRole.READ,
            credential,
            actor=resolve_local_actor(context),
        )
        captured = self._post(
            f"/workspaces/{changed_state.workspace_id}/schema/capture",
            {"csrf_token": self.csrf, "return_to_sources": "1"},
        )
        self.assertEqual(captured.status_code, 303, captured.text)
        saved_plan = self._post(
            f"/workspaces/{changed_state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "changed_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(saved_plan.status_code, 303, saved_plan.text)
        selection = context.queries.get_current_odoo_capture_selections(
            changed_state.workspace_id
        )[0]

        assessment_form = {
            "csrf_token": self.csrf,
            "selection_id": selection.selection_id,
            "selection_hash": selection.content_hash,
        }
        restart_handoff = self._post(
            f"/workspaces/{changed_state.workspace_id}/sources/odoo-assessment",
            assessment_form,
        )
        self.assertEqual(restart_handoff.status_code, 307)
        restarted = self._post(
            restart_handoff.headers["location"],
            assessment_form,
        )

        self.assertEqual(restarted.status_code, 303, restarted.text)
        successor_workspace_id = re.search(
            r"/workspaces/([^/]+)/sources",
            restarted.headers["location"],
        ).group(1)
        successor_workspace = context.migration_workspaces.get(
            successor_workspace_id,
            actor=resolve_local_actor(context),
        )
        successor_data = context.data_versions.get(
            successor_workspace.data_version_id,
            actor=resolve_local_actor(context),
        )
        self.assertIs(predecessor_data.state, DataVersionState.FROZEN)
        self.assertIs(successor_data.state, DataVersionState.DRAFT)
        self.assertEqual(
            successor_data.parent_data_version_id,
            predecessor_data.data_version_id,
        )
        self.assertIsNotNone(
            context.queries.get_odoo_schema_catalog(successor_workspace_id)
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(successor_workspace_id),
            (),
        )

    def test_changed_odoo_source_scope_blocks_downstream_until_refrozen(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_id = workspace_state.workspace_id
        saved_plan = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(saved_plan.status_code, 303)
        context.odoo_capture_publication.publish(
            workspace_id,
            _BrowserOdooCaptureGateway(workspace_state, schema),
            actor=resolve_local_actor(context),
        )
        self.assertTrue(context.navigation.get_source_readiness(workspace_id).ready)

        current = context.queries.get(workspace_id)
        context.workspace_states.update_schema_scope(
            workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=current.revision,
            permitted_models=("res.partner", "uom.uom"),
        )
        expanded_schema = replace(
            schema,
            models=(
                schema.models[0],
                replace(
                    schema.models[0],
                    name="uom.uom",
                    label="Units of Measure",
                ),
            ),
            content_hash="sha256:" + "7" * 64,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id,
            expanded_schema,
            actor=resolve_local_actor(context),
        )
        partial_plan = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(partial_plan.status_code, 303)
        expected_location = f"/workspaces/{workspace_id}/sources#capture-plan"

        for path in ("mapping", "summary", "prepare"):
            with self.subTest(path=path):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/{path}",
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], expected_location)

        with patch.object(
            context.preparation_jobs,
            "enqueue",
            wraps=context.preparation_jobs.enqueue,
        ) as enqueue:
            response = self._post(
                f"/workspaces/{workspace_id}/summary/check",
                {"csrf_token": self.csrf},
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], expected_location)
        enqueue.assert_not_called()
        source_page = self.client.get(response.headers["location"])
        self.assertIn("Your source choices changed", source_page.text)
        self.assertIn("still need a capture plan", source_page.text)
        self.assertIn("previous frozen version remains in history", source_page.text)

    def test_odoo_selection_failure_renders_the_workspace_error(self) -> None:
        workspace_state, _schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        with patch.object(
            context.sources,
            "define_odoo_capture_selection",
            side_effect=WorkspaceError("The capture plan could not be saved"),
        ):
            response = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
                {
                    "csrf_token": self.csrf,
                    "dataset_name": "odoo_contacts",
                    "model": "res.partner",
                    "field_names": "name",
                    "include_archived": "",
                    "page_size": "100",
                },
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("The capture plan could not be saved", response.text)
