"""Verify that stale model discovery can recover without replacing reviewed work."""

from dataclasses import replace
from types import SimpleNamespace
import asyncio
import unittest
from unittest.mock import Mock, patch

from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.errors import OdooModelCatalogRefreshRequired
from impodo.web.routers.schema import (
    _capture_selected_schema_sync,
    _check_selected_schema_sync,
)
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_model_catalog,
    _browser_schema,
)


class SchemaCaptureRecoveryTests(ProjectSetupBrowserTestCase):
    def setUp(self):
        super().setUp()
        self.context = self.app.state.context
        self.workspace, _ = self._registered_remote_schema_workspace()
        self.credential = store_target_credential(
            self.secrets, self.workspace, TargetCredentialRole.READ,
            "fictional-read-key", persistent=False,
        )
        self.identity_probe = self.context.read_identity_probe
        self.schema = self.context.schema_workspace.capture(
            self.workspace.workspace_id,
            _browser_schema(self.workspace),
            read_credential_binding_hash=self.credential.binding_hash,
            read_identity=self.identity_probe(
                self.workspace, self.credential.secret, self.workspace.intended_models,
            ),
            actor=self.context.actor,
        )
        self.discover_models()
        self.read_identity_calls.clear()
        self.workspace = self.context.queries.get(self.workspace.workspace_id)

    def discover_models(self, **identity_changes):
        return self.context.schema_workspace.discover_models(
            self.workspace.workspace_id,
            _browser_model_catalog(self.workspace),
            read_credential_binding_hash=self.credential.binding_hash,
            read_identity=replace(
                self.identity_probe(self.workspace, self.credential.secret, ("ir.model",)),
                **identity_changes,
            ),
            actor=self.context.actor,
        )

    def capture(self):
        return self.client.post(
            f"/workspaces/{self.workspace.workspace_id}/schema/capture",
            data={"csrf_token": self.csrf}, headers=POST_HEADERS,
            follow_redirects=False,
        )

    def current_schema(self):
        return self.context.queries.get_odoo_schema_catalog(self.workspace.workspace_id)

    def assert_choices_unchanged(self):
        self.assertEqual(self.context.queries.get(self.workspace.workspace_id), self.workspace)

    def test_stale_catalogue_refresh_keeps_unchanged_schema_and_choices(self):
        self.discover_models(context_hash="sha256:" + "7" * 64)
        self.read_identity_calls.clear()

        response = self.capture()

        self.assertEqual(response.status_code, 303, response.text)
        current = self.current_schema()
        self.assertEqual(current.content_hash, self.schema.content_hash)
        self.assertIsNone(current.pending_refresh)
        self.assertIsNotNone(current.last_checked_at)
        self.assert_choices_unchanged()
        self.assertEqual(len(self.model_catalog_calls), 1)
        self.assertEqual(len(self.schema_calls), 2)
        self.assertEqual(
            [models for _, _, models in self.read_identity_calls],
            [("res.partner",), ("ir.model",), ("res.partner",)],
        )
        discovery = self.context.queries.get_odoo_model_catalog(self.workspace.workspace_id)
        self.assertEqual(discovery.read_context_hash, current.read_context_hash)

    def test_current_catalogue_needs_only_one_field_read(self):
        response = self.capture()

        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(len(self.schema_calls), 1)
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.current_schema().content_hash, self.schema.content_hash)
        self.assertIsNone(self.current_schema().pending_refresh)
        self.assert_choices_unchanged()

    def test_schema_page_reads_in_worker_with_bounded_database_opens(self):
        get_workspace = self.context.queries.get

        def read_in_worker(workspace_id):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return get_workspace(workspace_id)

        with (
            collect_duckdb_request_timings() as timing,
            patch.object(self.context.queries, "get", side_effect=read_in_worker),
            patch.object(self.context, "schema_reader") as fields,
            patch.object(self.context, "model_catalog_reader") as models,
            patch.object(self.context, "read_identity_probe") as identity,
        ):
            response = self.client.get(
                f"/workspaces/{self.workspace.workspace_id}/schema",
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Odoo details are ready", response.text)
        self.assertIn("res.partner", response.text)
        self.assertGreater(timing.connection_count, 0)
        self.assertLessEqual(timing.connection_count, 8)
        fields.assert_not_called()
        models.assert_not_called()
        identity.assert_not_called()

    def test_changed_access_stays_pending_until_the_user_reviews_it(self):
        self.context.read_identity_probe = lambda *args: replace(
            self.identity_probe(*args),
            principal_hash="sha256:" + "8" * 64,
            context_hash="sha256:" + "9" * 64,
        )

        response = self.capture()

        self.assertEqual(response.status_code, 303, response.text)
        current = self.current_schema()
        self.assertEqual(current.content_hash, self.schema.content_hash)
        self.assertEqual(current.read_principal_hash, self.schema.read_principal_hash)
        self.assertEqual(current.read_context_hash, self.schema.read_context_hash)
        self.assertIsNotNone(current.pending_refresh)
        self.assertEqual(current.pending_refresh.read_context_hash, "sha256:" + "9" * 64)
        self.assert_choices_unchanged()
        self.assertEqual(len(self.model_catalog_calls), 1)
        self.assertEqual(len(self.schema_calls), 2)

    def test_access_changing_during_retry_stops_after_one_catalogue_refresh(self):
        probes = 0

        def changing_identity(*args):
            nonlocal probes
            probes += 1
            return replace(
                self.identity_probe(*args), context_hash="sha256:" + str(probes + 4) * 64
            )

        self.context.read_identity_probe = changing_identity
        response = self.capture()

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Odoo access changed again", response.text)
        self.assertEqual(len(self.model_catalog_calls), 1)
        self.assertEqual(len(self.schema_calls), 2)
        self.assertEqual(probes, 3)
        self.assertEqual(self.current_schema(), self.schema)
        self.assert_choices_unchanged()
    def test_catalogue_failure_preserves_current_schema_and_saved_choices(self):
        self.discover_models(context_hash="sha256:" + "7" * 64)
        with patch.object(
            self.context, "model_catalog_reader",
            side_effect=ConnectorTransportError("Odoo is temporarily unavailable"),
        ) as reader:
            response = self.capture()

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Odoo is temporarily unavailable", response.text)
        self.assertIn("Try loading details again", response.text)
        reader.assert_called_once()
        self.assertEqual(len(self.schema_calls), 1)
        self.assertEqual(self.current_schema(), self.schema)
        self.assert_choices_unchanged()

    def test_unrelated_field_failure_does_not_refresh_catalogue(self):
        with patch.object(
            self.context, "schema_reader",
            side_effect=ConnectorTransportError("The field request failed"),
        ) as reader:
            response = self.capture()

        self.assertEqual(response.status_code, 422, response.text)
        reader.assert_called_once()
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.current_schema(), self.schema)
        self.assert_choices_unchanged()


class SelectedSchemaCaptureTests(unittest.TestCase):
    def test_first_capture_and_background_check_use_fresh_reads_after_discovery(self):
        for load, operation, other in (
            (_capture_selected_schema_sync, "capture", "check_refresh"),
            (_check_selected_schema_sync, "check_refresh", "capture"),
        ):
            with self.subTest(operation=operation):
                workspace = SimpleNamespace(workspace_id="fictional-workspace")
                context = SimpleNamespace(schema_workspace=Mock(), actor=object())
                schema = object()
                save = getattr(context.schema_workspace, operation)
                save.side_effect = [OdooModelCatalogRefreshRequired("stale"), schema]
                with patch(
                    "impodo.web.routers.schema._read_selected_schema_sync",
                    side_effect=[
                        ("old-fields", "old-binding", "old-identity", None),
                        ("fresh-fields", "fresh-binding", "fresh-identity", None),
                    ],
                ) as read, patch(
                    "impodo.web.routers.schema._refresh_model_catalog_sync"
                ) as refresh:
                    self.assertIs(load(context, workspace), schema)
                self.assertEqual(read.call_count, 2)
                refresh.assert_called_once_with(context, workspace)
                save.assert_called_with(
                    workspace.workspace_id, "fresh-fields",
                    read_credential_binding_hash="fresh-binding",
                    read_identity="fresh-identity", actor=context.actor,
                )
                getattr(context.schema_workspace, other).assert_not_called()
