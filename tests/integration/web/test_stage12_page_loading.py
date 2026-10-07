"""Keep phase-one and phase-two page reads bounded as saved rules grow."""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch
from dataclasses import replace
from uuid import uuid4

from impodo.adapters.duckdb.foundation_source_package_reader import FoundationSourcePackageReader
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory, retain_databases_for_operation
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from impodo.application.shared.secrets import SecretStoreError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential


class _QueryCounter:
    def __init__(self, connection, counts):
        self.connection = connection
        self.counts = counts

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, query, *args, **kwargs):
        self.counts[0] += 1
        return self.connection.execute(query, *args, **kwargs)


class Stage12PageLoadingTests(ProjectSetupBrowserTestCase):
    def test_remote_capture_page_shares_status_and_preserves_vault_error(self):
        state, _ = self._registered_remote_schema_workspace()
        store_target_credential(self.secrets, state, TargetCredentialRole.READ, "fictional-read-key", persistent=False)
        with patch.object(self.secrets, "get", wraps=self.secrets.get) as secret:
            response = self.client.get(f"/workspaces/{state.workspace_id}/sources")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["read_credential_present"])
        self.assertTrue(response.context["read_credential_prompt"]["available"])
        self.assertEqual(secret.call_count, 1)
        self.assertNotIn("_read_credential_status", response.context)

        with patch.object(self.secrets, "get", side_effect=SecretStoreError("Fictional vault unavailable")) as secret:
            unavailable = self.client.get(f"/workspaces/{state.workspace_id}/sources")
        self.assertEqual(unavailable.status_code, 422)
        self.assertFalse(unavailable.context["read_credential_present"])
        self.assertFalse(unavailable.context["read_credential_prompt"]["available"])
        self.assertIn("Fictional vault unavailable", unavailable.text)
        self.assertEqual(secret.call_count, 1)

    def test_schema_reads_setup_once_for_routing_requirements_and_recovery(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context
        with patch.object(context.test_runs.test_runs, "for_workspace", wraps=context.test_runs.test_runs.for_workspace) as test_setup:
            response = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertEqual(response.status_code, 200)
        test_setup.assert_called_once_with(workspace_id)

    def test_remote_schema_shares_status_and_reads_changed_credentials_on_next_request(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2, connection_mode=OdooConnectionMode.REMOTE)
        context = self.app.state.context
        with patch.object(self.secrets, "get", wraps=self.secrets.get) as secret:
            current = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertEqual(current.status_code, 200)
        self.assertTrue(current.context["schema_credential_current"])
        self.assertTrue(current.context["read_credential_prompt"]["available"])
        self.assertEqual(secret.call_count, 1)
        self.assertNotIn("_read_credential_status", current.context)

        state = context.queries.get(workspace_id)
        store_target_credential(self.secrets, state, TargetCredentialRole.READ, "replacement-fictional-key", persistent=False)
        with patch.object(self.secrets, "get", wraps=self.secrets.get) as secret:
            changed = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(changed.context["schema_credential_current"])
        self.assertTrue(changed.context["read_credential_prompt"]["available"])
        self.assertEqual(secret.call_count, 1)

    def test_status_is_not_shared_with_a_different_credential_owner(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2, connection_mode=OdooConnectionMode.REMOTE)
        context = self.app.state.context
        owner = replace(context.queries.get(workspace_id), workspace_id=str(uuid4()))
        with (patch.object(type(context), "target_credential_workspace", return_value=owner),
              patch.object(self.secrets, "get", wraps=self.secrets.get) as secret):
            response = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["read_credential_present"])
        self.assertFalse(response.context["read_credential_prompt"]["available"])
        self.assertEqual(secret.call_count, 2)

    def test_unavailable_vault_is_read_once_and_keeps_recovery_prompt(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=2, connection_mode=OdooConnectionMode.REMOTE)
        with patch.object(self.secrets, "get", side_effect=SecretStoreError("Fictional vault unavailable")) as secret:
            response = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["read_credential_present"])
        self.assertFalse(response.context["read_credential_prompt"]["available"])
        self.assertEqual(secret.call_count, 1)

    def test_source_pages_read_one_package_in_a_worker_with_bounded_opens(self):
        workspace_id, _dataset, _key = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context
        real_state = context.queries._workspace_states.get_with_source_package
        real_package = FoundationSourcePackageReader.get

        def in_worker(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return real_state(*args, **kwargs)

        for area in ("sources", "datasets", "schema", "derived-entities", "target"):
            with (
                self.subTest(area=area),
                patch.object(context.queries._workspace_states, "get_with_source_package", side_effect=in_worker) as state,
                patch.object(FoundationSourcePackageReader, "get", autospec=True, side_effect=real_package) as packages,
                collect_duckdb_request_timings() as timings,
            ):
                response = self.client.get(f"/workspaces/{workspace_id}/{area}", follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(state.call_count, 1)
            self.assertEqual(packages.call_count, 1)
            self.assertLessEqual(timings.connection_count, 6)

    def test_related_rule_cards_do_not_add_sql_or_package_reads(self):
        workspace_id, dataset, _key = self._mapping_ready_workspace(scalar_field_count=2)
        context = self.app.state.context
        counts_by_size = []
        real_connect = DuckDbConnectionFactory.connect
        real_package = FoundationSourcePackageReader.get
        saved = 0
        for count in (0, 5, 25):
            with retain_databases_for_operation():
                for index in range(saved, count):
                    context.derived_entities.save_rule(
                        workspace_id, output_dataset_name=f"lookup_{index}",
                        source_dataset_id=dataset.dataset_id,
                        source_column_key=dataset.columns[0].stable_key,
                        target_model="res.partner", target_name_field="name",
                        external_id_namespace=f"lookup_{index}", parent_separator=None,
                        blank_policy="block", expected_parent_version=None if index == 0 else index,
                        actor=context.actor,
                    )
            saved = count
            counts = [0]

            @contextmanager
            def connect(factory, *args, **kwargs):
                with real_connect(factory, *args, **kwargs) as connection:
                    yield _QueryCounter(connection, counts)

            with (
                patch.object(DuckDbConnectionFactory, "connect", connect),
                patch.object(FoundationSourcePackageReader, "get", autospec=True, side_effect=real_package) as packages,
                collect_duckdb_request_timings() as timings,
            ):
                response = self.client.get(f"/workspaces/{workspace_id}/derived-entities")
            self.assertEqual(response.status_code, 200)
            if count:
                self.assertIn(f"lookup_{count - 1}", response.text)
            self.assertEqual(packages.call_count, 1)
            self.assertLessEqual(timings.connection_count, 6)
            counts_by_size.append(counts[0])
        self.assertEqual(len(set(counts_by_size)), 1, counts_by_size)
