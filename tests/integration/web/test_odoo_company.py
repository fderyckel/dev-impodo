"""Exercise run-owned company selection without replacing an existing workspace."""

from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping
from impodo.domain.odoo.company import OdooCompanyAccess, OdooCompanySelection
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.workbench import (
    transfer_destination_workspace,
    transfer_destination_identity_hash,
)
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from uuid import uuid4
from tests.support.browser_scenarios import (
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.identity import resolve_local_actor


class OdooCompanyWorkflowTests(ProjectSetupBrowserTestCase):
    def test_browser_recipe_activation_requires_its_own_company_choice(self):
        from dataclasses import replace
        from impodo.web.routers.schema import _require_run_company
        from impodo.domain.workspace.workbench import WorkspaceStateError

        workspace_id, access, _draft, _source, _schema = self.configured()
        state = self.app.state.context.queries.get(workspace_id)
        with self.assertRaisesRegex(WorkspaceStateError, "Choose the company"):
            _require_run_company(state)
        _require_run_company(replace(state, odoo_company=access.choices[1]))

    def test_local_shell_company_context_does_not_claim_an_api_principal(self):
        from dataclasses import replace
        from impodo.application.schema_workspace_service import _validate_read_identity
        from impodo.domain.shared.models import target_identity_hash

        workspace_id, _access, _draft, _source, _schema = self.configured()
        state = self.app.state.context.queries.get(workspace_id)
        target = target_identity_hash(
            connection_mode="LOCAL", base_url="http://127.0.0.1:8069", database="local"
        )
        state = replace(
            state,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="local",
            odoo_company=OdooCompanySelection(target, 707, "Local factory"),
        )
        identity = _validate_read_identity(state, None, required_models=("x.route",))
        self.assertEqual(identity["read_context_hash"], state.odoo_company.context_hash)
        self.assertEqual(identity["read_principal_hash"], "")
        self.assertEqual(identity["read_permission_hash"], "")

    def test_transfer_destination_company_does_not_change_frozen_source_or_its_context(
        self,
    ):
        source, schema = self._frozen_odoo_source_workspace()
        context = self.app.state.context
        actor = resolve_local_actor(context)
        source_selection = context.queries.get_source_selection(source.workspace_id)
        configured = context.workspace_states.configure_transfer_destination(
            source.workspace_id,
            actor=actor,
            expected_revision=source.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://destination.example.test",
            odoo_database="fictional_destination",
        )
        store_target_credential(
            self.secrets,
            configured,
            TargetCredentialRole.DESTINATION_TRANSFER,
            "destination-key",
            persistent=False,
        )
        choice = OdooCompanySelection(
            transfer_destination_identity_hash(configured),
            707,
            "Fictional destination factory",
        )
        access = OdooCompanyAccess(
            "destination@example.test", "Destination Group", (choice,)
        )
        with patch(
            "impodo.web.routers.odoo_company.read_company_access", return_value=access
        ) as read:
            response = self._post(
                f"/workspaces/{source.workspace_id}/transfer-destination/company",
                {
                    "csrf_token": self.csrf,
                    "revision": str(configured.revision),
                    "company_id": "707",
                },
            )
        self.assertEqual(response.status_code, 303, response.text[:500])
        called_state, called_key = read.call_args.args
        self.assertEqual(called_state.odoo_database, "fictional_destination")
        self.assertEqual(called_key, "destination-key")
        saved = context.queries.get(source.workspace_id)
        self.assertIsNone(saved.odoo_company)
        self.assertEqual(saved.odoo_operation_context, {})
        self.assertEqual(saved.destination_company, choice)
        self.assertEqual(saved.odoo_database, source.odoo_database)
        self.assertEqual(saved.odoo_base_url, source.odoo_base_url)
        self.assertEqual(
            context.queries.get_odoo_schema_catalog(source.workspace_id), schema
        )
        self.assertEqual(
            context.queries.get_source_selection(source.workspace_id), source_selection
        )
        self.assertEqual(
            transfer_destination_workspace(saved).odoo_operation_context,
            {"allowed_company_ids": [707]},
        )
        self.assertIn(
            "Fictional destination factory",
            self.client.get(response.headers["location"]).text,
        )

    def test_running_or_uncertain_load_blocks_company_change(self):
        workspace_id, access, _draft, _source, _schema = self.configured()
        context = self.app.state.context
        before = context.queries.get(workspace_id)
        repository = context.workspace_states.repository
        path = repository.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        run_id = str(uuid4())
        with duckdb.connect(str(path)) as connection:
            connection.execute(
                "INSERT INTO execution_run (run_id, snapshot_hash, snapshot_root_hash, preflight_run_id, target_hash, target_database, status, started_at, started_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    str(uuid4()),
                    access.choices[0].target_hash,
                    before.odoo_database,
                    "RUNNING",
                    "2026-10-01T00:00:00Z",
                    "Fictional operator",
                ],
            )
            connection.execute("INSERT INTO execution_current VALUES (1, ?)", [run_id])
        for status in ("RUNNING", "OUTCOME_UNKNOWN"):
            with self.subTest(status=status):
                with duckdb.connect(str(path)) as connection:
                    connection.execute("UPDATE execution_run SET status = ?", [status])
                with self.assertRaisesRegex(ValueError, "Verify or finish"):
                    context.workspace_states.select_odoo_company(
                        workspace_id,
                        actor=resolve_local_actor(context),
                        expected_revision=before.revision,
                        selection=access.choices[1],
                    )
                self.assertEqual(
                    context.queries.get(workspace_id).revision, before.revision
                )
                self.assertIsNone(context.queries.get(workspace_id).odoo_company)

    def configured(self):
        workspace_id, dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=1,
            connection_mode=OdooConnectionMode.REMOTE,
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        context.read_identity_probe = lambda _state, _key, models: OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash,
            readable_models=tuple(models),
            observed_at="2026-10-01T00:00:00Z",
        )
        draft = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode="upsert",
                    source_identity_column_keys=(dataset.columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(dataset.columns[0].stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                ),
            ),
            expected_version=None,
            actor=actor,
        )
        selection = context.queries.get_source_selection(workspace_id)
        access = OdooCompanyAccess(
            "checking@example.test",
            "Northwind Group",
            (
                OdooCompanySelection(
                    schema.connection_target_hash, 80, "Northwind Group"
                ),
                OdooCompanySelection(
                    schema.connection_target_hash, 105, "Northwind Factory"
                ),
            ),
        )
        return workspace_id, access, draft, selection, schema

    def test_selection_survives_reload_and_keeps_existing_source_and_mapping(self):
        workspace_id, access, draft, source, schema = self.configured()
        context = self.app.state.context
        before = context.queries.get(workspace_id)
        physical_paths = []
        connect = duckdb.connect

        def observed_connect(path, *args, **kwargs):
            physical_paths.append(str(path))
            return connect(path, *args, **kwargs)

        with patch(
            "impodo.web.routers.odoo_company.read_company_access", return_value=access
        ) as remote:
            choices = self._post(
                f"/workspaces/{workspace_id}/target/company/choices",
                {"csrf_token": self.csrf},
            )
            self.assertEqual(choices.status_code, 200, choices.text[:500])
            self.assertIn("checking@example.test", choices.text)
            self.assertIn("Northwind Factory", choices.text)
            with (
                collect_duckdb_request_timings() as timings,
                patch("duckdb.connect", side_effect=observed_connect),
            ):
                response = self._post(
                    f"/workspaces/{workspace_id}/target/company",
                    {
                        "csrf_token": self.csrf,
                        "revision": str(before.revision),
                        "company_id": "105",
                    },
                )
        self.assertEqual(response.status_code, 303, response.text[:500])
        self.assertEqual(remote.call_count, 2)
        after = context.queries.get(workspace_id)
        self.assertEqual(after.workspace_id, before.workspace_id)
        self.assertEqual(after.source_files, before.source_files)
        self.assertEqual(after.odoo_company, access.choices[1])
        self.assertEqual(after.odoo_operation_context, {"allowed_company_ids": [105]})
        self.assertEqual(context.queries.get_source_selection(workspace_id), source)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            draft,
        )
        self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), schema)
        self.assertIn(
            "Northwind Factory", self.client.get(response.headers["location"]).text
        )
        # Each phase uses four owners: registry, workspace linkage, Data version
        # and workspace engine. Publication also changes registry/linkage
        # connection modes for their writes. No owner spans the API call.
        self.assertLessEqual(len(set(physical_paths)), 4)
        self.assertLessEqual(timings.schema_check_count, 2)
        self.assertLessEqual(timings.connection_count, 10)
        self.assertEqual(timings.lock_retry_count, 0)

    def test_stale_and_unavailable_choices_do_not_change_the_company(self):
        workspace_id, access, _draft, _source, _schema = self.configured()
        state = self.app.state.context.queries.get(workspace_id)
        with patch(
            "impodo.web.routers.odoo_company.read_company_access", return_value=access
        ) as remote:
            stale = self._post(
                f"/workspaces/{workspace_id}/target/company",
                {
                    "csrf_token": self.csrf,
                    "revision": str(state.revision - 1),
                    "company_id": "105",
                },
            )
            self.assertEqual(stale.status_code, 422)
            remote.assert_not_called()
            missing = self._post(
                f"/workspaces/{workspace_id}/target/company",
                {
                    "csrf_token": self.csrf,
                    "revision": str(state.revision),
                    "company_id": "999",
                },
            )
            self.assertEqual(missing.status_code, 422)
        self.assertIsNone(self.app.state.context.queries.get(workspace_id).odoo_company)

    def test_company_context_refresh_preserves_mapping_when_schema_is_unchanged(self):
        workspace_id, access, draft, _source, schema = self.configured()
        context = self.app.state.context
        state = context.queries.get(workspace_id)
        actor = resolve_local_actor(context)
        selected = context.workspace_states.select_odoo_company(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=access.choices[1],
        )
        from tests.support.browser_scenarios import (
            FieldMetadata,
            MetadataSnapshot,
            ModelMetadata,
            _browser_schema,
        )

        metadata = MetadataSnapshot(
            fingerprint=_browser_schema(selected).fingerprint,
            models={
                model.name: ModelMetadata(
                    model.name,
                    model.label,
                    {
                        field.name: FieldMetadata(
                            field.name,
                            field.type,
                            required=field.required,
                            readonly=field.readonly,
                            relation=field.relation,
                            selection=field.selection,
                        )
                        for field in model.fields
                    },
                )
                for model in schema.models
            },
        )
        identity = OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=selected.odoo_company.context_hash,
            readable_models=tuple(model.name for model in schema.models),
            observed_at="2026-10-01T00:00:00Z",
        )
        checked = context.schema_workspace.check_refresh(
            workspace_id,
            metadata,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_identity=identity,
            actor=actor,
        )
        self.assertIsNone(checked.pending_refresh)
        self.assertEqual(checked.read_context_hash, identity.context_hash)
        self.assertEqual(checked.content_hash, schema.content_hash)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            draft,
        )
