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
    POST_HEADERS,
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.identity import resolve_local_actor


class OdooCompanyWorkflowTests(ProjectSetupBrowserTestCase):
    def test_check_matches_refreshes_stale_key_and_company_before_many2one_reads(self):
        self._check_matches_refresh()

    def test_check_matches_refreshes_saved_model_catalogue_with_company_access(self):
        self._check_matches_refresh(saved_model_catalog=True, use_session=True)

    def test_catalogue_refresh_keeps_permission_changes_in_review(self):
        self._check_matches_refresh(saved_model_catalog=True, changed_permissions=True)

    def test_catalogue_refresh_rejects_key_change_before_publication(self):
        self._check_matches_refresh(saved_model_catalog=True, rotate_during_read=True)

    def test_catalogue_refresh_rejects_reader_change_between_remote_reads(self):
        self._check_matches_refresh(saved_model_catalog=True, changed_catalog_reader=True)

    def test_check_matches_requires_review_if_verified_permissions_changed(self):
        self._check_matches_refresh(changed_permissions=True)

    def test_key_change_during_automatic_refresh_rejects_before_publication(self):
        self._check_matches_refresh(rotate_during_read=True)

    def _check_matches_refresh(
        self, *, changed_permissions=False, rotate_during_read=False,
        saved_model_catalog=False, changed_catalog_reader=False, use_session=False,
    ):
        from unittest.mock import Mock
        from impodo.adapters.duckdb.unit_of_work import _read_databases
        from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
        from impodo.domain.shared.models import (
            FieldMetadata,
            ModelMetadata,
            TargetRecord,
        )
        from tests.support.recipe_lifecycle import metadata_for_schema

        from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
        from impodo.domain.workspace.contracts import SchemaField, SchemaModel
        from impodo.domain.schema.governance import (
            BusinessKeyDefinition,
            BusinessKeyStatus,
        )

        context = self.app.state.context
        actor = resolve_local_actor(context)
        self.context, self.actor = context, actor
        workspace_id = HierarchyWorkflowFixture.freeze_files(
            self, {"people": b"Reference,Country\nP001,Example\n"}
        )
        original = HierarchyWorkflowFixture.schema(
            self,
            workspace_id,
            mode="REMOTE",
            extra_models=(
                SchemaModel(
                    "res.partner",
                    "Contact",
                    (
                        SchemaField(
                            "ref", "Reference", "char", True, False, None, None, ()
                        ),
                        SchemaField(
                            "relation_0000",
                            "Country",
                            "many2one",
                            False,
                            False,
                            "res.country",
                            None,
                            (),
                        ),
                    ),
                ),
            ),
            extra_keys=(
                BusinessKeyDefinition(
                    "partner:ref",
                    "res.partner",
                    ("ref",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        keys = context.queries.get_schema_governance(workspace_id).business_keys
        original = context.schema_workspace.capture(workspace_id, metadata_for_schema(original),
            read_credential_binding_hash=original.read_credential_binding_hash,
            read_identity=OdooReadIdentity(target_hash=original.connection_target_hash,
                principal_hash=original.read_principal_hash, permission_hash=original.read_permission_hash,
                context_hash=original.read_context_hash, readable_models=tuple(sorted(model.name for model in original.models)),
                observed_at="2026-10-31T00:00:00Z"), actor=actor)
        context.schema_workspace.govern(workspace_id, business_keys=keys, actor=actor)
        model_snapshot = RecordSnapshot(metadata_for_schema(original).fingerprint,
            {"ir.model": tuple(TargetRecord("ir.model", index + 1,
                {"model": model.name, "name": model.label,
                 "abstract": False, "transient": False, "modules": "base", "state": "base"})
                for index, model in enumerate(original.models))},
            {"ir.model": ("model", "name", "abstract", "transient", "modules", "state")})
        if saved_model_catalog:
            context.schema_workspace.discover_models(
                workspace_id,
                model_snapshot,
                read_credential_binding_hash=original.read_credential_binding_hash,
                read_identity=OdooReadIdentity(target_hash=original.connection_target_hash,
                    principal_hash=original.read_principal_hash, permission_hash=original.read_permission_hash,
                    context_hash=original.read_context_hash, readable_models=("ir.model",),
                    observed_at="2026-10-31T00:00:00Z"), actor=actor)
        dataset = context.queries.get_mapping_source_selection(workspace_id).datasets[0]
        business_key = next(
            key
            for key in context.queries.get_schema_governance(workspace_id).business_keys
            if key.model == "res.partner"
        )
        state = context.queries.get(workspace_id)
        selected = context.workspace_states.select_odoo_company(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=OdooCompanySelection(
                original.connection_target_hash, 105, "Northwind Factory"
            ),
        )
        owner = context.target_credential_workspace(
            workspace_id, workspace_state=selected, actor=actor
        )
        credential = store_target_credential(
            self.secrets,
            owner,
            TargetCredentialRole.READ,
            "current-key",
            persistent=False,
        )
        metadata = metadata_for_schema(original)

        def read_schema(_state, _key):
            self.assertFalse(
                hasattr(_read_databases, "connections"),
                "A database owner crossed the Odoo read",
            )
            if rotate_during_read:
                store_target_credential(
                    self.secrets,
                    owner,
                    TargetCredentialRole.READ,
                    "newer-key",
                    persistent=False,
                )
            return metadata

        def identity(state, _key, models):
            self.assertFalse(hasattr(_read_databases, "connections"))
            return OdooReadIdentity(
                target_hash=original.connection_target_hash,
                principal_hash="sha256:" + "8" * 64
                if changed_catalog_reader and models == ("ir.model",)
                else original.read_principal_hash,
                permission_hash="sha256:" + "9" * 64
                if changed_permissions
                else original.read_permission_hash,
                context_hash=state.odoo_company.context_hash,
                readable_models=tuple(sorted(models)),
                observed_at="2026-10-31T00:00:00Z",
            )

        context.readiness_reader = None
        context.schema_read_session_factory = None
        context.schema_reader = Mock(side_effect=read_schema)
        context.read_identity_probe = identity

        def read_model_catalog(_state, _key):
            self.assertFalse(hasattr(_read_databases, "connections"))
            return model_snapshot

        context.model_catalog_reader = Mock(side_effect=read_model_catalog)

        if use_session:
            class ReadSession:
                def read_models(reader, _models):
                    return context.schema_reader(selected, credential.secret)

                def verify_identity(reader, models):
                    return identity(selected, credential.secret, models)

                def read_model_catalog(reader):
                    return context.model_catalog_reader(selected, credential.secret)

            context.schema_read_session_factory = Mock(return_value=ReadSession())

        class SupportingReader:
            def get_model_metadata(reader, _requests):
                self.assertFalse(hasattr(_read_databases, "connections"))
                return MetadataSnapshot(
                    metadata.fingerprint,
                    {
                        "res.country": ModelMetadata(
                            "res.country",
                            "Country",
                            {
                            name: FieldMetadata(name, "char", required=True)
                                for name in ("code", "name")
                            },
                        )
                    },
                )

            def get_records(reader, requests):
                self.assertFalse(hasattr(_read_databases, "connections"))
                return RecordSnapshot(
                    metadata.fingerprint,
                    {
                        "res.country": (
                            TargetRecord(
                                "res.country",
                                901,
                                {"code": "EX", "name": "Exampleland"},
                            ),
                        )
                    },
                    {request.model: request.fields for request in requests},
                )

        entries = [
            ["csrf_token", self.csrf],
            ["action", "draft"],
            ["operation_id", str(uuid4())],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "create"],
            ["on_existing_0", "block"],
            ["source_identity_0", dataset.columns[0].stable_key],
            ["business_key_0", business_key.key_id],
            ["identity_source_0_0", dataset.columns[0].stable_key],
            ["visible_relation_target_0", "relation_0000"],
            ["relation_source_0_0", dataset.columns[1].stable_key],
            ["relation_origin_0_0", "target_catalog"],
            ["relation_key_0_0", "odoo-standard:res.country:code"],
            ["relation_operation_0_0", "replace"],
            ["relation_compare_0_0", "1"],
            ["relation_missing_0_0", "error"],
            ["relation_ambiguous_0_0", "error"],
            ["relation_null_0_0", "distinct"],
            [
                "relation_value_matches_0_0",
                '[{"source_value":"Example","target_value":"EX"}]',
            ],
        ]
        with patch(
            "impodo.web.composition.target_readers.Json2ReadConnector",
            return_value=SupportingReader(),
        ) as supporting:
            with collect_duckdb_request_timings() as timings:
                checked = self.client.post(
                    f"/workspaces/{workspace_id}/mapping/save",
                    json={"entries": entries},
                    headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                )
        refreshed = context.queries.get_odoo_schema_catalog(workspace_id)
        if changed_permissions or rotate_during_read or changed_catalog_reader:
            self.assertEqual(checked.status_code, 422, checked.text)
            self.assertEqual(
                refreshed.read_credential_binding_hash,
                original.read_credential_binding_hash,
            )
            self.assertEqual(refreshed.pending_refresh is not None, changed_permissions)
            self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
            supporting.assert_not_called()
            if saved_model_catalog:
                catalogue = context.queries.get_odoo_model_catalog(workspace_id)
                expected_context = selected.odoo_company.context_hash if changed_permissions else original.read_context_hash
                self.assertEqual(catalogue.read_context_hash, expected_context)
            return
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "committed")
        self.assertEqual(
            refreshed.read_credential_binding_hash, credential.binding_hash
        )
        self.assertEqual(
            refreshed.read_context_hash, selected.odoo_company.context_hash
        )
        revision = context.queries.get_mapping_revision(workspace_id)
        self.assertEqual(revision.definition.datasets[0].mode.value, "create")
        self.assertEqual(
            revision.definition.datasets[0]
            .relationships[0]
            .resolver.value_mappings[0]
            .target_value,
            "EX",
        )
        self.assertNotEqual(
            context.queries.get_mapping_validation(
                workspace_id, revision.version
            ).status.value,
            "INVALID",
        )
        context.schema_reader.assert_called_once()
        if saved_model_catalog:
            context.model_catalog_reader.assert_called_once()
            catalogue = context.queries.get_odoo_model_catalog(workspace_id)
            self.assertEqual(catalogue.read_context_hash, selected.odoo_company.context_hash)
            self.assertEqual(catalogue.read_credential_binding_hash, credential.binding_hash)
            if use_session:
                context.schema_read_session_factory.assert_called_once()
        else:
            context.model_catalog_reader.assert_not_called()
        # The request's local phases reuse the registry, linkage, Data version
        # and engine owners. RPCs run after every retained owner has closed.
        self.assertLessEqual(timings.connection_count, 30)
        self.assertLessEqual(timings.schema_check_count, 8)
        self.assertEqual(timings.lock_retry_count, 0)
        replayed = self.client.post(f"/workspaces/{workspace_id}/mapping/save", json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf})
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(context.queries.get_mapping_revision(workspace_id).version, revision.version)
        context.schema_reader.assert_called_once()
        return timings

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
        self.assertNotIn(
            'id="odoo-operation-company"',
            self.client.get(f"/workspaces/{workspace_id}/target").text,
        )
        self.assertNotIn(
            'id="odoo-operation-company"',
            self.client.get(f"/workspaces/{workspace_id}/schema").text,
        )
        self.assertIn(
            'id="odoo-operation-company"',
            self.client.get(f"/workspaces/{workspace_id}/mapping").text,
        )
        physical_paths = []
        connect = duckdb.connect

        def observed_connect(path, *args, **kwargs):
            physical_paths.append(str(path))
            return connect(path, *args, **kwargs)

        with patch(
            "impodo.web.routers.odoo_company.read_company_access", return_value=access
        ) as remote:
            choices = self._post(
                f"/workspaces/{workspace_id}/mapping/company/choices",
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
                    f"/workspaces/{workspace_id}/mapping/company",
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
        self._exercise_company_refresh()

    def test_new_checking_key_and_company_refresh_preserve_matches(self):
        self._exercise_company_refresh(rotated=True)

    def test_key_refresh_is_available_after_company_has_already_been_checked(self):
        workspace_id, draft, schema = self._exercise_company_refresh(rotated=True)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        owner = context.target_credential_workspace(
            workspace_id, workspace_state=state, actor=actor
        )
        credential = store_target_credential(
            self.secrets,
            owner,
            TargetCredentialRole.READ,
            "a-new-checking-generation",
            persistent=False,
        )
        self.assertNotEqual(
            credential.binding_hash, schema.read_credential_binding_hash
        )
        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertIn(
            f'action="/workspaces/{workspace_id}/mapping/company/refresh"', page.text
        )
        response = self._post(
            f"/workspaces/{workspace_id}/mapping/company/refresh",
            {
                "csrf_token": self.csrf,
                "revision": str(state.revision),
            },
        )
        self.assertEqual(response.status_code, 303, response.text[:1000])
        checked = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertIsNone(checked.pending_refresh)
        self.assertEqual(checked.read_credential_binding_hash, credential.binding_hash)
        self.assertEqual(checked.content_hash, schema.content_hash)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            draft,
        )

    def test_changed_checking_user_or_permissions_still_require_review(self):
        for changed in ("principal_hash", "permission_hash"):
            with self.subTest(changed=changed):
                self._exercise_company_refresh(rotated=True, changed_access=changed)

    def _exercise_company_refresh(self, *, rotated=False, changed_access=None):
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
        credential = None
        if rotated:
            owner = context.target_credential_workspace(
                workspace_id, workspace_state=selected, actor=actor
            )
            credential = store_target_credential(
                self.secrets,
                owner,
                TargetCredentialRole.READ,
                "a-new-checking-generation",
                persistent=False,
            )
            self.assertNotEqual(
                credential.binding_hash, schema.read_credential_binding_hash
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
        if changed_access:
            from dataclasses import replace

            identity = replace(identity, **{changed_access: "sha256:" + "9" * 64})
        context.schema_read_session_factory = None
        context.schema_reader = lambda _state, _key: metadata
        context.read_identity_probe = lambda _state, _key, _models: identity
        with collect_duckdb_request_timings() as timings:
            response = self._post(
                f"/workspaces/{workspace_id}/mapping/company/refresh",
                {
                    "csrf_token": self.csrf,
                    "revision": str(selected.revision),
                },
            )
        self.assertEqual(response.status_code, 303, response.text[:1000])
        checked = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertLessEqual(timings.connection_count, 10)
        self.assertLessEqual(timings.schema_check_count, 2)
        self.assertEqual(timings.lock_retry_count, 0)
        if changed_access:
            self.assertIsNotNone(checked.pending_refresh)
            self.assertEqual(
                checked.read_credential_binding_hash,
                schema.read_credential_binding_hash,
            )
            self.assertEqual(
                context.mapping_workspace.mappings.get_mapping_working_draft(
                    workspace_id
                ),
                draft,
            )
            return
        self.assertIsNone(checked.pending_refresh)
        if credential:
            self.assertEqual(
                checked.read_credential_binding_hash, credential.binding_hash
            )
        self.assertEqual(checked.read_context_hash, identity.context_hash)
        self.assertEqual(checked.content_hash, schema.content_hash)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            draft,
        )
        return workspace_id, draft, checked
