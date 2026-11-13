"""Save multiple labelled scope choices through the real Stage 2 route."""

from dataclasses import replace
from html.parser import HTMLParser

from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor
from tests.support.stage2_matching_rules import (
    install_stage2_schema,
    matching_rule_form,
)


class ScopeCheckboxes(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.selected = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (
            tag == "input"
            and "data-scope-field" in attributes
            and "checked" in attributes
        ):
            self.selected.append(attributes["value"])


class Stage2ScopeSelectionTests(ProjectSetupBrowserTestCase):
    def test_odoo19_parent_and_company_scopes_save_clear_and_reload(self):
        self.exercise(19)

    def test_odoo20_parent_and_company_scopes_save_clear_and_reload(self):
        self.exercise(20)

    def exercise(
        self, major, *, model="stock.location", scopes=("location_id", "company_id")
    ):
        workspace, _, _ = self._mapping_ready_workspace(scalar_field_count=1)
        fields = (
            SchemaField("name", "Location Name", "char", True, False, None, None, ()),
            SchemaField(
                scopes[0], "Parent Location", "many2one", False, False, model, None, ()
            ),
            SchemaField(
                scopes[1], "Company", "many2one", False, False, "res.company", None, ()
            ),
        )
        schema = install_stage2_schema(
            self,
            workspace,
            (SchemaModel(model, "Inventory Location", fields),),
            hash_character="6",
        )
        schema = replace(schema, odoo_version=f"{major}.0")
        self.app.state.context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace, schema, actor=resolve_local_actor(self.app.state.context)
        )
        before = (
            len(self.schema_calls),
            len(self.read_identity_calls),
            len(self.readiness_calls),
        )
        page = self.client.get(f"/workspaces/{workspace}/schema")
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-scope-field", page.text)
        self.assertNotIn('name="primary_scope_field_0"', page.text)
        selections = {model: (("name",), scopes, "")}
        form = matching_rule_form(self.csrf, schema, selections)
        form.pop("primary_scope_field_0")
        form.update(
            {"scope_choices_present_0": "1", "scope_choice_0": list(reversed(scopes))}
        )
        saved = self.client.post(
            f"/workspaces/{workspace}/schema/govern",
            data=form,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303, saved.text[:500])
        governance = self.app.state.context.queries.get_schema_governance(workspace)
        self.assertEqual(governance.business_keys[0].scope_fields, scopes)
        reopened = self.client.get(f"/workspaces/{workspace}/schema")
        self.assertEqual(set(ScopeCheckboxes(reopened.text).selected), set(scopes))
        self.assertIn("Parent Location, Company", reopened.text)
        prior_hash = governance.content_hash

        invalid = dict(form)
        invalid["scope_choice_0"] = ["name", scopes[0]]
        rejected = self.client.post(
            f"/workspaces/{workspace}/schema/govern",
            data=invalid,
            headers=POST_HEADERS,
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertIn("choose each field only once", rejected.text)
        self.assertEqual(
            set(ScopeCheckboxes(rejected.text).selected), {"name", scopes[0]}
        )
        self.assertEqual(
            self.app.state.context.queries.get_schema_governance(
                workspace
            ).content_hash,
            prior_hash,
        )
        # A native form can clear the checkboxes while its old read-only mirror
        # still contains both saved names. An empty selection must win.
        cleared = dict(form)
        cleared.pop("scope_choice_0")
        saved = self.client.post(
            f"/workspaces/{workspace}/schema/govern",
            data=cleared,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303, saved.text[:500])
        governance = self.app.state.context.queries.get_schema_governance(workspace)
        self.assertEqual(governance.business_keys[0].scope_fields, ())
        self.assertEqual(
            (
                len(self.schema_calls),
                len(self.read_identity_calls),
                len(self.readiness_calls),
            ),
            before,
        )

    def test_multiple_scopes_are_generic_for_custom_record_types(self):
        self.exercise(20, model="x.storage_bin", scopes=("zone_id", "organization_id"))
