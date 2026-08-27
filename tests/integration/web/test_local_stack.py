"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from tests.support.browser_scenarios import (
    Actor,
    ActorIdentity,
    Capability,
    ConnectorError,
    LocalStackBrowserTestCase,
    POST_HEADERS,
)


class LocalStackBrowserTests(LocalStackBrowserTestCase):
    def test_selects_config_checks_status_and_keeps_profile_session_only(self) -> None:
        target = self.client.get(f"/workspaces/{self.workspace_id}/target")
        self.assertIn("Find local Odoo", target.text)
        self.assertIn("Choose local Odoo setup", target.text)

        selected = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(selected.status_code, 303)
        self.assertEqual(
            selected.headers["location"],
            f"/workspaces/{self.workspace_id}/target?local_stack=1",
        )
        self.assertEqual(self.picker_calls, 1)

        refreshed = self.client.get(selected.headers["location"])
        self.assertIn("PostgreSQL is accepting connections", refreshed.text)
        self.assertIn("Odoo is not responding yet", refreshed.text)
        self.assertIn("status-ready", refreshed.text)
        self.assertIn("status-action", refreshed.text)
        self.assertIn('value="http://127.0.0.1:18069"', refreshed.text)
        self.assertIn('value="odoo19_local"', refreshed.text)
        self.assertNotIn("postgres-secret", refreshed.text)
        self.assertNotIn("master-secret", refreshed.text)
        self.assertIn("Start local Odoo", refreshed.text)

        workspace_state = self.app.state.context.workspace_states.repository.get(self.workspace_id)
        self.assertEqual(workspace_state.odoo_base_url, "")
        self.assertEqual(workspace_state.odoo_database, "")
        config_bytes = str(self.config).encode()
        for path in self.app.state.context.workspace_states.repository.workspace_directory(
            self.workspace_id
        ).rglob("*"):
            if path.is_file():
                self.assertNotIn(config_bytes, path.read_bytes())

    def test_start_requires_confirmation_and_updates_readiness(self) -> None:
        self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        unconfirmed = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/start",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertIn("Confirm the detected paths", unconfirmed.text)
        self.assertEqual(self.start_calls, 0)

        started = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/start",
            data={
                "csrf_token": self.csrf,
                "confirm_start": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(started.status_code, 303)
        self.assertEqual(self.start_calls, 1)
        page = self.client.get(started.headers["location"])
        self.assertIn("Odoo 19.0 is responding", page.text)
        self.assertIn("Control local services started by Impodo", page.text)
        self.assertIn("Stop managed services", page.text)

    def test_local_connection_test_opens_all_green_results(self) -> None:
        self._select_and_start_stack()

        tested = self.client.post(
            f"/workspaces/{self.workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(self.workspace_revision),
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:18069",
                "odoo_database": "odoo19_local",
                "action": "test",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(tested.status_code, 303)
        self.assertEqual(
            tested.headers["location"],
            f"/workspaces/{self.workspace_id}/target?local_stack=1",
        )
        results = self.client.get(tested.headers["location"])
        self.assertIn('data-auto-open="true"', results.text)
        self.assertEqual(results.text.count("status-ready"), 4)
        self.assertIn("Odoo data access", results.text)
        self.assertIn("Read-only database access succeeded", results.text)
        self.local_odoo_reader.get_target_fingerprint.assert_called_once()

    def test_local_connection_test_opens_mixed_failure_results(self) -> None:
        self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        tested = self.client.post(
            f"/workspaces/{self.workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(self.workspace_revision),
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:18069",
                "odoo_database": "odoo19_local",
                "action": "test",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(tested.status_code, 422)
        self.assertIn('data-auto-open="true"', tested.text)
        self.assertEqual(tested.text.count("status-ready"), 2)
        self.assertEqual(tested.text.count("status-error"), 2)
        self.assertIn("Local connection checks failed: Odoo server", tested.text)
        self.assertIn("Read-only database access failed", tested.text)
        self.local_odoo_reader.get_target_fingerprint.assert_not_called()

    def test_local_connection_test_marks_database_access_failure(self) -> None:
        self._select_and_start_stack()
        self.local_odoo_reader.get_target_fingerprint.side_effect = ConnectorError(
            "The configured database could not be opened."
        )

        tested = self.client.post(
            f"/workspaces/{self.workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(self.workspace_revision),
                "odoo_connection_mode": "LOCAL",
                "odoo_base_url": "http://127.0.0.1:18069",
                "odoo_database": "odoo19_local",
                "action": "test",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(tested.status_code, 422)
        self.assertIn('data-auto-open="true"', tested.text)
        self.assertEqual(tested.text.count("status-ready"), 3)
        self.assertEqual(tested.text.count("status-error"), 1)
        self.assertIn("The configured database could not be opened", tested.text)
        self.assertIn("Read-only database access failed", tested.text)

    def test_stop_requires_confirmation_and_stops_only_managed_process(self) -> None:
        self._select_and_start_stack()
        process = self.started_processes[0]

        unconfirmed = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "action": "stop",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertIn("Confirm control", unconfirmed.text)
        process.terminate.assert_not_called()

        stopped = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "stop",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(stopped.status_code, 303)
        process.terminate.assert_called_once_with()
        page = self.client.get(stopped.headers["location"])
        self.assertIn("Start local Odoo", page.text)
        self.assertNotIn("Control local services started by Impodo", page.text)

    def test_restart_stops_the_owned_process_then_starts_a_new_one(self) -> None:
        self._select_and_start_stack()
        first_process = self.started_processes[0]

        restarted = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "restart",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(restarted.status_code, 303)
        first_process.terminate.assert_called_once_with()
        self.assertEqual(self.start_calls, 2)
        self.assertEqual(len(self.started_processes), 2)
        page = self.client.get(restarted.headers["location"])
        self.assertIn("Control local services started by Impodo", page.text)

    def test_stop_requires_its_explicit_capability(self) -> None:
        self._select_and_start_stack()
        process = self.started_processes[0]
        self.app.state.context.actor = Actor(
            identity=ActorIdentity(
                issuer="https://identity.example.test",
                subject_id="stack-starter",
                display_name="Stack starter",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROJECT_EDIT,
                    Capability.LOCAL_STACK_INSPECT,
                    Capability.LOCAL_STACK_START,
                }
            ),
        )

        blocked = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "stop",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 403)
        process.terminate.assert_not_called()

    def test_stop_never_controls_a_stack_started_outside_impodo(self) -> None:
        self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        blocked = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/control",
            data={
                "csrf_token": self.csrf,
                "confirm_control": "1",
                "action": "stop",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 422)
        self.assertIn("does not own any running service", blocked.text)
        self.assertEqual(self.start_calls, 0)

    def test_remote_project_cannot_open_local_assistant(self) -> None:
        saved = self.client.post(
            f"/workspaces/{self.workspace_id}/target",
            data={
                "csrf_token": self.csrf,
                "revision": str(self.workspace_revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.com",
                "odoo_database": "odoo_review",
                "action": "save",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 422)
        self.assertIn("Check the Odoo connection before continuing", saved.text)

        blocked = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn(
            "available only in Local Odoo mode",
            blocked.text,
        )
        self.assertEqual(self.picker_calls, 0)

    def test_registered_project_reconnects_and_returns_to_comparison(self) -> None:
        self._register_local_project()
        self.stack_running = True

        selected = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={
                "csrf_token": self.csrf,
                "return_to": "summary_compare",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(selected.status_code, 303)
        self.assertEqual(
            selected.headers["location"],
            (
                f"/workspaces/{self.workspace_id}/summary?local_stack=1"
                "#compare-with-odoo"
            ),
        )
        self.assertEqual(self.picker_calls, 1)
        self.local_odoo_reader.get_target_fingerprint.assert_called_once()
        status = self.app.state.context.local_stack.get(self.workspace_id)
        self.assertTrue(status.metadata_ready)

        summary = self.client.get(selected.headers["location"])
        self.assertEqual(summary.status_code, 200)
        self.assertIn('data-auto-open="true"', summary.text)
        self.assertIn("Reconnect local Odoo", summary.text)
        self.assertIn("Continue comparison", summary.text)
        self.assertRegex(
            summary.text,
            r'<button class="button primary" type="submit"\s*>\s*Continue comparison',
        )

    def test_registered_recovery_rejects_another_local_database(self) -> None:
        self._register_local_project(database="another_database")
        self.stack_running = True

        rejected = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={
                "csrf_token": self.csrf,
                "return_to": "summary_compare",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(rejected.status_code, 422)
        self.assertIn("Local Odoo is not ready yet", rejected.text)
        self.assertIn("points to database odoo19_local", rejected.text)
        self.assertIn("Choose another local setup", rejected.text)
        self.local_odoo_reader.get_target_fingerprint.assert_not_called()

    def test_local_assistant_requires_its_explicit_capability(self) -> None:
        self.app.state.context.actor = Actor(
            identity=ActorIdentity(
                issuer="https://identity.example.test",
                subject_id="target-editor",
                display_name="Target editor",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROJECT_EDIT,
                }
            ),
        )

        blocked = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.picker_calls, 0)

    def test_start_requires_its_explicit_capability(self) -> None:
        self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/select-config",
            data={"csrf_token": self.csrf},
            headers=POST_HEADERS,
        )
        self.app.state.context.actor = Actor(
            identity=ActorIdentity(
                issuer="https://identity.example.test",
                subject_id="stack-inspector",
                display_name="Stack inspector",
            ),
            capabilities=frozenset(
                {
                    Capability.PROJECT_VIEW,
                    Capability.PROJECT_EDIT,
                    Capability.LOCAL_STACK_INSPECT,
                }
            ),
        )

        blocked = self.client.post(
            f"/workspaces/{self.workspace_id}/local-stack/start",
            data={
                "csrf_token": self.csrf,
                "confirm_start": "1",
            },
            headers=POST_HEADERS,
        )

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.start_calls, 0)
