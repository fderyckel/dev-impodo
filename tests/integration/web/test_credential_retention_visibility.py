"""Keep saved Odoo access visible without rendering or replacing its secret."""

from dataclasses import replace
from html.parser import HTMLParser

from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _browser_schema,
    _register_workspace_through_services,
)
from tests.support.identity import resolve_local_actor


class _CredentialForm(HTMLParser):
    """Read the named credential controls from one real connection form."""

    def __init__(self, markup, action):
        super().__init__()
        self.action = action
        self.active = False
        self.values = {}
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self.active = values.get("action") == self.action
        if tag != "input" or not self.active or not values.get("name"):
            return
        if values.get("type") == "radio" and "checked" not in values:
            return
        self.values[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False


class CredentialRetentionVisibilityTests(ProjectSetupBrowserTestCase):
    def test_stage_two_key_survives_return_and_blank_connection_recheck(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            self._replace_connection_probes(
                fingerprint_probe=lambda state, _key, major=major: replace(
                    _browser_schema(state).fingerprint,
                    odoo_version=f"{major}.0",
                )
            )
            for storage, label in (
                ("session", "Available this session"),
                ("vault", "Saved on this computer"),
            ):
                with self.subTest(major=major, storage=storage):
                    created = self.workspaces.create(
                        name=f"Source key retention {major} {storage}",
                        source_system=f"Odoo {major}",
                        source_mode="ODOO",
                    )
                    state = _register_workspace_through_services(
                        context,
                        created,
                        connection_mode=OdooConnectionMode.REMOTE,
                        base_url="https://source.example.test",
                        database="source",
                    )
                    workspace_id = state.workspace_id
                    project_id = context.migration_workspaces.get(
                        workspace_id, actor=actor
                    ).project_id
                    schema_url = f"/workspaces/{workspace_id}/schema"
                    saved = self._post(
                        f"/projects/{project_id}/workspaces/{workspace_id}"
                        "/target/read-credential/quick",
                        {
                            "csrf_token": self.csrf,
                            "read_api_key": "fictional-source-key",
                            "read_api_key_storage": storage,
                            "return_to": schema_url,
                        },
                    )
                    self.assertEqual(saved.status_code, 303, saved.text)
                    original = get_target_credential(
                        self.secrets, state, TargetCredentialRole.READ
                    )
                    self.assertIsNotNone(original)
                    stage_two = self.client.get(schema_url)
                    self.assertEqual(stage_two.status_code, 200, stage_two.text)
                    self.assertIn("Source key:</strong>", stage_two.text)
                    self.assertIn(label, stage_two.text)
                    self.assertNotIn("fictional-source-key", stage_two.text)
                    self.assertNotIn('id="odoo-read-key"', stage_two.text)

                    target_url = f"/workspaces/{workspace_id}/target"
                    target = self.client.get(target_url)
                    form = _CredentialForm(target.text, target_url)
                    self.assertEqual(form.values["read_api_key"], "")
                    self.assertEqual(form.values["read_api_key_storage"], storage)
                    self.assertIn(
                        "Leave this field empty to keep the saved key.", target.text
                    )
                    form.values["action"] = "test"
                    checked = self._post(target_url, form.values)
                    self.assertEqual(checked.status_code, 303, checked.text)
                    current = context.queries.get(workspace_id)
                    retained = get_target_credential(
                        self.secrets, current, TargetCredentialRole.READ
                    )
                    self.assertEqual(retained, original)
                    self.assertEqual(retained.persistent, storage == "vault")
                    self.assertIn(
                        (workspace_id, "fictional-source-key", ("res.users",)),
                        self.read_identity_calls,
                    )
                    returned = self.client.get(schema_url)
                    self.assertIn("Source key:</strong>", returned.text)
                    self.assertIn(label, returned.text)
                    self.assertNotIn("fictional-source-key", returned.text)

    def test_destination_recheck_keeps_its_key_and_shows_actual_retention(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state, _schema = self._frozen_odoo_source_workspace()
        state = context.workspace_states.configure_transfer_destination(
            state.workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://destination.example.test",
            odoo_database="destination",
        )
        source = store_target_credential(
            self.secrets,
            state,
            TargetCredentialRole.READ,
            "fictional-distinct-source-key",
            persistent=True,
        )
        destination_url = f"/workspaces/{state.workspace_id}/transfer-destination"
        for storage in ("session", "vault"):
            with self.subTest(storage=storage):
                original = store_target_credential(
                    self.secrets,
                    state,
                    TargetCredentialRole.DESTINATION_TRANSFER,
                    "fictional-destination-key",
                    persistent=storage == "vault",
                )
                page = self.client.get(destination_url)
                self.assertEqual(page.status_code, 200, page.text)
                form = _CredentialForm(page.text, destination_url)
                self.assertEqual(form.values["read_api_key"], "")
                self.assertEqual(form.values["read_api_key_storage"], storage)
                self.assertIn(
                    "Leave this field empty to keep the saved destination key.",
                    page.text,
                )
                self.assertNotIn("fictional-destination-key", page.text)
                self.assertNotIn("fictional-distinct-source-key", page.text)
                checked = self._post(destination_url, form.values)
                self.assertEqual(checked.status_code, 303, checked.text)
                state = context.queries.get(state.workspace_id)
                retained = get_target_credential(
                    self.secrets, state, TargetCredentialRole.DESTINATION_TRANSFER
                )
                self.assertEqual(retained, original)
                self.assertEqual(retained.persistent, storage == "vault")
                self.assertEqual(
                    get_target_credential(
                        self.secrets, state, TargetCredentialRole.READ
                    ),
                    source,
                )

    def test_saved_source_status_remains_visible_on_capture_page(self):
        state, _schema = self._registered_remote_schema_workspace()
        store_target_credential(
            self.secrets,
            state,
            TargetCredentialRole.READ,
            "fictional-capture-key",
            persistent=True,
        )
        page = self.client.get(f"/workspaces/{state.workspace_id}/sources")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Source key:</strong>", page.text)
        self.assertIn("Saved on this computer", page.text)
        self.assertIn("This key will remain available after Impodo closes.", page.text)
        self.assertNotIn("fictional-capture-key", page.text)
