"""Verify capture-editor defaults and deliberate audit-field saves."""

from dataclasses import replace
from html.parser import HTMLParser

from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class _CaptureFieldChoices(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.choices = {}
        self.capture_form = False
        self.advanced_open = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.capture_form = "data-capture-plan-form" in attributes
        if (
            self.capture_form
            and tag == "details"
            and "data-capture-advanced-options" in attributes
        ):
            self.advanced_open = "open" in attributes
        if (
            self.capture_form
            and tag == "input"
            and attributes.get("name") == "field_names"
        ):
            self.choices[attributes["value"]] = "checked" in attributes

    def handle_endtag(self, tag):
        if tag == "form":
            self.capture_form = False


class OdooCaptureFieldDefaultBrowserTests(ProjectSetupBrowserTestCase):
    def test_new_defaults_and_saved_creation_timestamp_for_both_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                workspace, schema = self._registered_remote_schema_workspace()
                context = self.app.state.context
                model = schema.models[0]
                name = next(field for field in model.fields if field.name == "name")
                created_on = replace(
                    name,
                    name="create_date",
                    label="Created on",
                    type="datetime",
                    readonly=True,
                    relation=None,
                    stored=True,
                    computed=False,
                    related=False,
                    company_dependent=False,
                    exportable=True,
                )
                schema = replace(
                    schema,
                    odoo_version=f"{major}.0",
                    policy_hash=odoo_source_policy_hash(major),
                    models=(replace(model, fields=(*model.fields, created_on)),),
                )
                context.schema_workspace.schemas.save_odoo_schema_catalog(
                    workspace.workspace_id,
                    schema,
                    actor=resolve_local_actor(context),
                )
                sources_url = f"/workspaces/{workspace.workspace_id}/sources"
                edit_url = f"{sources_url}?model={model.name}&edit=1#capture-plan"
                first = self.client.get(edit_url)
                self.assertEqual(first.status_code, 200, first.text)
                first_form = _CaptureFieldChoices(first.text)
                defaults = first_form.choices
                self.assertFalse(first_form.advanced_open)
                self.assertIn("Save fields and continue", first.text)
                self.assertTrue(defaults["name"])
                self.assertFalse(defaults["create_date"])
                self.assertNotIn("write_date", defaults)

                saved = self._post(
                    f"{sources_url}/odoo-selection",
                    {
                        "csrf_token": self.csrf,
                        "dataset_name": "business_backup",
                        "model": model.name,
                        "field_names": ["create_date", "name"],
                        "page_size": "100",
                    },
                )
                self.assertEqual(saved.status_code, 303, saved.text)
                selection = context.queries.get_current_odoo_capture_selections(
                    workspace.workspace_id,
                )[0]
                self.assertEqual(selection.field_names, ("create_date", "name"))
                self.assertEqual(selection.policy_hash, odoo_source_policy_hash(major))
                returned = self.client.get(edit_url)
                self.assertEqual(returned.status_code, 200, returned.text)
                choices = _CaptureFieldChoices(returned.text).choices
                self.assertEqual(
                    {name for name, checked in choices.items() if checked},
                    {"create_date", "name"},
                )
