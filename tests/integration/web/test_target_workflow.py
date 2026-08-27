"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorError,
    ConnectorTransportError,
    LocalStackService,
    OdooConnectionMode,
    POST_HEADERS,
    Path,
    ProjectSetupBrowserTestCase,
    SchemaOrigin,
    SourceSelection,
    TargetCredentialRole,
    WorkspaceStatus,
    _browser_model_catalog,
    _browser_schema,
    _created_workspace_id,
    _replace_run_target_setup,
    _workspace_data_version_id,
    datetime,
    get_target_credential,
    replace,
    timezone,
    uuid4,
)


class TargetWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_remote_connection_status_is_visible_persistent_and_target_bound(
        self,
    ) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Remote connection feedback",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        workspace_state = self._complete_setup_before_target(workspace_id)
        target_form = self.client.get(f"/workspaces/{workspace_id}/target")
        self.assertIn('name="read_api_key"', target_form.text)
        self.assertIn('name="read_api_key_storage"', target_form.text)
        self.assertIn('name="keep_api_key_for_loading"', target_form.text)
        self.assertIn("Use this key for checking and loading", target_form.text)
        self.assertIn('value="vault"', target_form.text)
        self.assertIn("Not available", target_form.text)
        self.assertNotIn('name="write_api_key"', target_form.text)

        tested = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://edu-ucaps.odoo.com",
                "odoo_database": "edu-ucaps",
                "read_api_key": "remote-secret-key",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(tested.status_code, 303)
        self.assertEqual(
            tested.headers["location"],
            f"/workspaces/{workspace_id}/target#remote-connection-status",
        )
        result = self.client.get(tested.headers["location"])
        self.assertIn("connection-state-ready", result.text)
        self.assertIn("The Odoo connection is ready.", result.text)
        self.assertIn("Read-only access to edu-ucaps succeeded.", result.text)
        self.assertIn("Supported Odoo version 19.0.", result.text)
        self.assertIn(
            "The authenticated read-only Odoo principal was identified.",
            result.text,
        )
        self.assertIn("Checked during this Impodo session.", result.text)
        self.assertIn("Available this session", result.text)
        self.assertIn("Impodo will forget this key when Impodo closes.", result.text)
        self.assertIn("Forget checking key", result.text)
        self.assertIn(">Check again</button>", result.text)
        self.assertRegex(result.text, r"data-local-stack-entry\s+hidden")
        self.assertNotIn("remote-secret-key", result.text)
        self.assertEqual(
            self.read_identity_calls,
            [(workspace_id, "remote-secret-key", ("res.users",))],
        )
        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        self.assertEqual(
            get_target_credential(
                self.secrets,
                workspace_state,
                TargetCredentialRole.READ,
            ).secret,
            "remote-secret-key",
        )
        self.assertIsNone(
            get_target_credential(
                self.secrets,
                workspace_state,
                TargetCredentialRole.WRITE,
            )
        )

        refreshed = self.client.get(f"/workspaces/{workspace_id}/target")
        self.assertIn("The Odoo connection is ready.", refreshed.text)
        self.assertEqual(len(self.connection_calls), 1)

        forgotten = self.client.post(
            f"/workspaces/{workspace_id}/target/read-credential/delete",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(forgotten.status_code, 303)
        forgotten_page = self.client.get(forgotten.headers["location"])
        self.assertIn("The read-only Odoo key was forgotten.", forgotten_page.text)
        self.assertIn("Not available", forgotten_page.text)
        self.assertNotIn("The Odoo connection is ready.", forgotten_page.text)
        self.assertEqual(self.secrets.values, {})

        changed = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://other.example.com",
                "odoo_database": "other_database",
                "action": "save",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 422)
        changed_target = changed
        self.assertIn("connection-state-unknown", changed_target.text)
        self.assertIn(
            "The Odoo connection has not been checked.",
            changed_target.text,
        )
        self.assertNotIn("Read-only access to edu-ucaps succeeded.", changed_target.text)
        self.assertEqual(self.secrets.values, {})

        self.assertIn('/static/target-connection.js', changed_target.text)
        script = self.client.get("/static/target-connection.js")
        self.assertIn("resetRemoteConnectionStatus", script.text)
        self.assertIn('window.location.hash === "#remote-connection-status"', script.text)
        self.assertIn('/static/target-connection.css', changed_target.text)
        styles = self.client.get("/static/target-connection.css")
        self.assertIn("[data-local-stack-entry][hidden]", styles.text)

    def test_stage_two_can_keep_the_checking_key_for_loading(self) -> None:
        context = self.app.state.context
        created = self.workspaces.create(
            name="One approved Odoo key",
            source_system="Other",
        )
        now = datetime.now(timezone.utc)
        workspace_state = replace(
            created,
            status=WorkspaceStatus.REGISTERED,
            revision=created.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            workspace_state,
            expected_revision=created.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        workspace_id = workspace_state.workspace_id

        tested = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://edu-ucaps.odoo.com",
                "odoo_database": "edu-ucaps",
                "read_api_key": "approved-check-and-load-key",
                "read_api_key_storage": "vault",
                "keep_api_key_for_loading": "1",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(tested.status_code, 303, tested.text)
        workspace_state = self.app.state.context.workspace_states.repository.get(
            workspace_id
        )
        read_credential = get_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
        )
        write_credential = get_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.WRITE,
        )
        assert read_credential is not None
        assert write_credential is not None
        self.assertEqual(read_credential.secret, "approved-check-and-load-key")
        self.assertEqual(write_credential.secret, "approved-check-and-load-key")
        self.assertTrue(read_credential.persistent)
        self.assertTrue(write_credential.persistent)
        repository = self.app.state.context.workspace_states.repository
        self.assertTrue(
            repository.has_audit_event(
                workspace_id,
                "ODOO_READ_CREDENTIAL_STORED",
            )
        )
        self.assertTrue(
            repository.has_audit_event(
                workspace_id,
                "ODOO_WRITE_CREDENTIAL_STORED",
            )
        )

        status_page = self.client.get(tested.headers["location"])
        self.assertIn("Checking key: Saved on this computer", status_page.text)
        self.assertIn(
            "Loading key:</strong> Saved on this computer",
            status_page.text,
        )
        self.assertIn(
            "Stage 6 without asking you to enter it again",
            status_page.text,
        )
        self.assertIn("Forget loading key", status_page.text)
        self.assertNotIn("approved-check-and-load-key", status_page.text)

        forgotten = self.client.post(
            f"/workspaces/{workspace_id}/target/write-credential/delete",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(forgotten.status_code, 303)
        self.assertIsNone(
            get_target_credential(
                self.secrets,
                workspace_state,
                TargetCredentialRole.WRITE,
            )
        )
        self.assertIsNotNone(
            get_target_credential(
                self.secrets,
                workspace_state,
                TargetCredentialRole.READ,
            )
        )
        forgotten_page = self.client.get(forgotten.headers["location"])
        self.assertIn("The Odoo loading key was forgotten.", forgotten_page.text)

        kept_again = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://edu-ucaps.odoo.com",
                "odoo_database": "edu-ucaps",
                "keep_api_key_for_loading": "1",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(kept_again.status_code, 303, kept_again.text)
        current = self.app.state.context.workspace_states.repository.get(workspace_id)
        restored_write = get_target_credential(
            self.secrets,
            current,
            TargetCredentialRole.WRITE,
        )
        assert restored_write is not None
        self.assertEqual(restored_write.secret, "approved-check-and-load-key")
        self.assertTrue(restored_write.persistent)

    def test_remote_connection_failure_shows_safe_red_checks(self) -> None:
        def rejected_connection(_project, _api_key):
            raise ConnectorAuthenticationError(
                "raw remote response and secret must not be displayed"
            )

        self._replace_connection_probes(
            fingerprint_probe=rejected_connection,
        )
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Rejected remote connection",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        workspace_state = self._complete_setup_before_target(workspace_id)

        tested = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.com",
                "odoo_database": "migration",
                "api_key": "never-render-this-key",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(tested.status_code, 303)
        result = self.client.get(tested.headers["location"])
        self.assertIn("connection-state-error", result.text)
        self.assertIn("The Odoo connection is not ready.", result.text)
        self.assertIn("Odoo responded to the read-only check.", result.text)
        self.assertIn(
            "Odoo rejected the access key, database name, or API entitlement.",
            result.text,
        )
        self.assertIn("ODOO_READ_KEY_REJECTED", result.text)
        self.assertIn(">Try again</button>", result.text)
        self.assertNotIn("never-render-this-key", result.text)
        self.assertNotIn("raw remote response", result.text)

    def test_remote_connection_reports_missing_principal_model_access(self) -> None:
        def denied_identity(_project, _api_key, _models):
            raise ConnectorAuthorizationError(
                "internal group and model details must not be rendered"
            )

        self._replace_connection_probes(identity_probe=denied_identity)
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Read principal permission",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        workspace_state = self._complete_setup_before_target(workspace_id)

        tested = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.com",
                "odoo_database": "migration",
                "read_api_key": "never-render-this-key",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(tested.status_code, 303)
        result = self.client.get(tested.headers["location"])
        self.assertIn("ODOO_READ_ACCESS_MISSING", result.text)
        self.assertIn(
            "The authenticated principal lacks the required read access.",
            result.text,
        )
        self.assertNotIn("never-render-this-key", result.text)
        self.assertNotIn("internal group", result.text)

    def test_remote_connection_distinguishes_api_version_and_network_failures(
        self,
    ) -> None:
        def wrong_version(workspace_state, _api_key):
            return replace(
                _browser_schema(workspace_state).fingerprint,
                odoo_version="18.0",
            )

        def missing_api(_project, _api_key):
            raise ConnectorTransportError("Odoo JSON-2 read failed with HTTP 404")

        def unreachable(_project, _api_key):
            raise ConnectorTransportError(
                "Odoo JSON-2 read timed out or was unreachable"
            )

        cases = (
            (
                wrong_version,
                "Impodo requires Odoo 19; this target reported Odoo 18.0.",
                "ODOO_VERSION_UNSUPPORTED",
            ),
            (
                missing_api,
                "The JSON-2 API was not available at this address.",
                "ODOO_API_HTTP_404",
            ),
            (
                unreachable,
                "Impodo could not reach Odoo. Check the address and network connection.",
                "ODOO_TARGET_UNREACHABLE",
            ),
        )

        for index, (tester, message, support_code) in enumerate(cases, start=1):
            with self.subTest(support_code=support_code):
                self._replace_connection_probes(fingerprint_probe=tester)
                created = self._post(
                    "/projects/new",
                    {
                        "csrf_token": self.csrf,
                        "display_name": f"Remote failure {index}",
                        "source_mode": "FILE",
                        "source_system_identity": "Other",
                    },
                )
                workspace_id = _created_workspace_id(self.app, created)
                workspace_state = self._complete_setup_before_target(workspace_id)
                tested = self.client.post(
                    f"/workspaces/{workspace_id}/target",
                    data={
                        "csrf_token": self.csrf,
                        "revision": str(workspace_state.revision),
                        "odoo_connection_mode": "REMOTE",
                        "odoo_base_url": "https://odoo.example.com",
                        "odoo_database": f"migration_{index}",
                        "api_key": f"secret-{index}",
                        "action": "test",
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(tested.status_code, 303)
                result = self.client.get(tested.headers["location"])
                self.assertIn("connection-state-error", result.text)
                self.assertIn(message, result.text)
                self.assertIn(support_code, result.text)
                self.assertNotIn(f"secret-{index}", result.text)

    def test_local_schema_draft_does_not_call_the_odoo_api(self) -> None:
        context = self.app.state.context
        created = self.workspaces.create(
            name="Local draft",
            source_system="CSV",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            created,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
            revision=2,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            registered,
            expected_revision=created.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        _replace_run_target_setup(
            context,
            registered.workspace_id,
            connection_mode=OdooConnectionMode.LOCAL,
            base_url="http://127.0.0.1:8069",
            database="odoo19_local",
        )
        context.sources.sources.save_source_selection(
            registered.workspace_id,
            SourceSelection(
                selection_id=str(uuid4()),
                version=1,
                data_version_id=_workspace_data_version_id(
                    context,
                    registered.workspace_id,
                ),
                created_at=now,
                created_by=context.actor.identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=context.actor,
        )

        drafted = self._post(
            f"/workspaces/{registered.workspace_id}/schema/local-draft",
            {
                "csrf_token": self.csrf,
                "acknowledge_local_draft": "1",
                "manual_model_label_0": "Contact",
                "manual_fields_0": "name | Name | char | yes | no",
            },
        )
        self.assertEqual(drafted.status_code, 303, drafted.text)
        self.assertEqual(self.schema_calls, [])
        schema = context.schema_workspace.schemas.get_odoo_schema_catalog(
            registered.workspace_id
        )
        self.assertIsNotNone(schema)
        self.assertEqual(schema.origin, SchemaOrigin.LOCAL_MANUAL)
        schema_page = self.client.get(drafted.headers["location"])
        self.assertIn("Needs Odoo check", schema_page.text)
        self.assertIn("name | Name | char", schema_page.text)

    def test_registered_local_schema_uses_selected_config_without_api_key(
        self,
    ) -> None:
        context = self.app.state.context
        created = self.workspaces.create(
            name="Keyless local schema",
            source_system="CSV",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            created,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:18069",
            odoo_database="odoo19_local",
            intended_applications=("Contacts",),
            status=WorkspaceStatus.REGISTERED,
            revision=2,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            registered,
            expected_revision=created.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        _replace_run_target_setup(
            context,
            registered.workspace_id,
            connection_mode=OdooConnectionMode.LOCAL,
            base_url="http://127.0.0.1:18069",
            database="odoo19_local",
            intended_applications=("Contacts",),
        )
        context.sources.sources.save_source_selection(
            registered.workspace_id,
            SourceSelection(
                selection_id=str(uuid4()),
                version=1,
                data_version_id=_workspace_data_version_id(
                    context,
                    registered.workspace_id,
                ),
                created_at=now,
                created_by=context.actor.identity.display_name,
                datasets=(),
                content_hash="sha256:" + "b" * 64,
            ),
            actor=context.actor,
        )
        unconfigured_page = self.client.get(
            f"/workspaces/{registered.workspace_id}/schema"
        )
        self.assertIn(
            "Find local Odoo first",
            unconfigured_page.text,
        )
        self.assertIn(
            f'action="/workspaces/{registered.workspace_id}/schema/local-config"',
            unconfigured_page.text,
        )
        blocked = self.client.post(
            f"/workspaces/{registered.workspace_id}/schema/models/refresh",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("Impodo could not check Odoo", blocked.text)

        workspace = Path(self.temporary.name) / "local-odoo"
        config = workspace / "config" / "odoo.conf"
        config.parent.mkdir(parents=True)
        config.write_text(
            "\n".join(
                (
                    "[options]",
                    "http_interface = 127.0.0.1",
                    "http_port = 18069",
                    "db_host = 127.0.0.1",
                    "db_port = 5544",
                    "db_user = odoo",
                    "db_name = odoo19_local",
                )
            ),
            encoding="utf-8",
        )
        for relative_path in (
            "venv/Scripts/python.exe",
            "odoo/odoo-bin",
        ):
            executable = workspace / relative_path
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
        status = context.local_stack.select_config(
            registered.workspace_id,
            config,
        )
        self.assertIsNotNone(status.profile)
        configured_local_stack = context.local_stack

        self.local_odoo_reader.get_model_catalog.return_value = (
            _browser_model_catalog(registered)
        )
        self.local_odoo_reader.get_model_metadata.return_value = (
            _browser_schema(registered)
        )

        page = self.client.get(f"/workspaces/{registered.workspace_id}/schema")
        self.assertIn("Local Odoo is ready to check", page.text)
        self.assertNotIn("Access key", page.text)
        self.assertIn("Show available Odoo data", page.text)
        self.assertNotIn("Verify access and load models", page.text)
        refreshed = self._post(
            f"/workspaces/{registered.workspace_id}/schema/models/refresh",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(refreshed.status_code, 303)
        self.local_odoo_reader.get_model_catalog.assert_called_once()
        self.assertEqual(self.model_catalog_calls, [])
        verified_page = self.client.get(refreshed.headers["location"])
        self.assertIn("The Odoo list is ready", verified_page.text)
        self.assertIn(
            "The connection to local Odoo was checked during this session",
            verified_page.text,
        )
        self.assertIn("Update available choices", verified_page.text)
        self.assertIn("Has Odoo changed?", verified_page.text)
        self.assertIn("Save choices and continue", verified_page.text)
        self.assertNotIn("Save Odoo choices", verified_page.text)
        context.local_stack = LocalStackService()
        cached_page = self.client.get(
            f"/workspaces/{registered.workspace_id}/schema"
        )
        self.assertIn("The Odoo list is ready", cached_page.text)
        self.assertIn(
            "Support details",
            cached_page.text,
        )
        self.assertIn("The saved Odoo list is still available", cached_page.text)
        self.assertIn("res.partner", cached_page.text)
        self.local_odoo_reader.get_model_catalog.assert_called_once()

        context.local_stack = configured_local_stack
        workspace_state = context.workspace_states.repository.get(registered.workspace_id)
        scoped = self._post(
            f"/workspaces/{registered.workspace_id}/schema",
            {
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "permitted_models": "res.partner",
            },
        )
        self.assertEqual(scoped.status_code, 303)
        self.assertEqual(
            scoped.headers["location"],
            f"/workspaces/{registered.workspace_id}/schema#odoo-details",
        )
        self.local_odoo_reader.get_model_catalog.assert_called_once()
        self.local_odoo_reader.get_model_metadata.assert_called_once()
        metadata_call = (
            self.local_odoo_reader.get_model_metadata.call_args.args
        )
        self.assertEqual(metadata_call[2], ("res.partner",))
        self.assertEqual(self.schema_calls, [])
        scoped_page = self.client.get(scoped.headers["location"])
        self.assertIn(
            "Odoo data is ready",
            scoped_page.text,
        )
        unchanged_project = context.workspace_states.repository.get(
            registered.workspace_id
        )
        unchanged_scope = self._post(
            f"/workspaces/{registered.workspace_id}/schema",
            {
                "csrf_token": self.csrf,
                "revision": str(unchanged_project.revision),
                "permitted_models": "res.partner",
            },
        )
        self.assertEqual(unchanged_scope.status_code, 303)
        self.local_odoo_reader.get_model_metadata.assert_called_once()
        context.local_stack = LocalStackService()
        cached_schema_page = self.client.get(
            f"/workspaces/{registered.workspace_id}/schema"
        )
        self.assertIn(
            "Odoo details are ready",
            cached_schema_page.text,
        )
        self.assertIn('id="odoo-details"', cached_schema_page.text)
        self.assertIn(
            "The snapshot includes inherited fields and is used without another Odoo call",
            cached_schema_page.text,
        )
        self.assertIn(
            "Check for Odoo changes",
            cached_schema_page.text,
        )
        self.local_odoo_reader.get_model_metadata.assert_called_once()

        context.local_stack = configured_local_stack
        current_schema = context.queries.get_odoo_schema_catalog(
            registered.workspace_id
        )
        assert current_schema is not None
        self.local_odoo_reader.get_model_metadata.return_value = _browser_schema(
            registered
        )
        unchanged_check = self._post(
            f"/workspaces/{registered.workspace_id}/schema/capture",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(unchanged_check.status_code, 303)
        unchanged_page = self.client.get(unchanged_check.headers["location"])
        self.assertIn("Odoo details are unchanged", unchanged_page.text)
        unchanged_schema = context.queries.get_odoo_schema_catalog(
            registered.workspace_id
        )
        assert unchanged_schema is not None
        self.assertEqual(unchanged_schema.content_hash, current_schema.content_hash)
        self.assertIsNone(unchanged_schema.pending_refresh)

        changed_snapshot = _browser_schema(registered)
        changed_partner = changed_snapshot.models["res.partner"]
        self.local_odoo_reader.get_model_metadata.return_value = replace(
            changed_snapshot,
            models={
                "res.partner": replace(
                    changed_partner,
                    fields={
                        **changed_partner.fields,
                        "name": replace(
                            changed_partner.fields["name"],
                            required=False,
                        ),
                    },
                )
            },
        )
        changed_check = self._post(
            f"/workspaces/{registered.workspace_id}/schema/capture",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(changed_check.status_code, 303)
        changed_page = self.client.get(changed_check.headers["location"])
        self.assertIn("Odoo details changed", changed_page.text)
        self.assertIn("Field behavior changed", changed_page.text)
        self.assertIn("Use updated Odoo details", changed_page.text)
        self.assertIn("Needs attention", changed_page.text)
        reviewed_schema = context.queries.get_odoo_schema_catalog(
            registered.workspace_id
        )
        assert reviewed_schema is not None
        pending = reviewed_schema.pending_refresh
        assert pending is not None
        unconfirmed = self._post(
            f"/workspaces/{registered.workspace_id}/schema/capture/confirm",
            {
                "csrf_token": self.csrf,
                "expected_current_content_hash": (
                    pending.expected_current_content_hash
                ),
                "candidate_id": pending.candidate_id,
                "candidate_semantic_hash": pending.semantic_hash,
            },
        )
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertIn("Confirm that Impodo may replace", unconfirmed.text)
        still_pending = context.queries.get_odoo_schema_catalog(
            registered.workspace_id
        )
        assert still_pending is not None
        self.assertEqual(still_pending.pending_refresh, pending)
        confirmed = self._post(
            f"/workspaces/{registered.workspace_id}/schema/capture/confirm",
            {
                "csrf_token": self.csrf,
                "expected_current_content_hash": (
                    pending.expected_current_content_hash
                ),
                "candidate_id": pending.candidate_id,
                "candidate_semantic_hash": pending.semantic_hash,
                "confirm_schema_refresh": "1",
            },
        )
        self.assertEqual(confirmed.status_code, 303)
        confirmed_schema = context.queries.get_odoo_schema_catalog(
            registered.workspace_id
        )
        assert confirmed_schema is not None
        self.assertIsNone(confirmed_schema.pending_refresh)
        self.assertNotEqual(
            confirmed_schema.content_hash,
            current_schema.content_hash,
        )

        self.local_odoo_reader.get_model_metadata.side_effect = ConnectorError(
            "raw local reader failure"
        )
        workspace_state = context.workspace_states.repository.get(registered.workspace_id)
        failed_load = self.client.post(
            f"/workspaces/{registered.workspace_id}/schema",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "permitted_models": "product.template",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(failed_load.status_code, 422)
        self.assertIn(
            "Your Odoo choices were saved, but their details could not be loaded",
            failed_load.text,
        )
        self.assertIn("Try loading details again", failed_load.text)
        self.assertGreater(
            failed_load.text.index('id="odoo-load-error"'),
            failed_load.text.index('id="odoo-details"'),
        )
        self.assertLess(
            failed_load.text.index('id="odoo-load-error"'),
            failed_load.text.index("Try loading details again"),
        )
        self.assertNotIn("We could not complete that action", failed_load.text)
        saved_after_failure = context.workspace_states.repository.get(
            registered.workspace_id
        )
        self.assertEqual(
            saved_after_failure.intended_models,
            ("product.template",),
        )
        product_snapshot = _browser_schema(saved_after_failure)
        product_model = replace(
            product_snapshot.models["res.partner"],
            model="product.template",
        )
        self.local_odoo_reader.get_model_metadata.side_effect = None
        self.local_odoo_reader.get_model_metadata.return_value = replace(
            product_snapshot,
            models={"product.template": product_model},
        )
        recovered = self._post(
            f"/workspaces/{registered.workspace_id}/schema/capture",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(recovered.status_code, 303)
        recovered_page = self.client.get(recovered.headers["location"])
        self.assertIn("Odoo data is ready", recovered_page.text)
        self.assertIn("Check for Odoo changes", recovered_page.text)

    def test_saved_key_is_not_reused_after_target_change(self) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Credential binding",
                "source_mode": "FILE",
                "source_system_identity": "Other",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        workspace_state = self._complete_setup_before_target(workspace_id)
        local = self._post(
            f"/workspaces/{workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:8069",
                "odoo_database": "odoo19_local",
                "api_key": "local-only-key",
                "action": "test",
            },
        )
        self.assertEqual(local.status_code, 303)

        remote = self.client.post(
            f"/workspaces/{workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision + 1),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.com",
                "odoo_database": "odoo_review",
                "action": "test",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(remote.status_code, 200)
        self.assertIn(
            "Enter an Odoo access key for this remote target.",
            remote.text,
        )
        self.assertEqual(len(self.connection_calls), 1)
        self.assertEqual(self.connection_calls[0][1], "local-only-key")
        self.assertEqual(self.secrets.values, {})
