"""Refresh Recipe application languages without changing immutable Recipe rules."""

import html
import re
from unittest import TestCase

from tests.integration.web import test_recipe_target_matches as recipe_fixtures
from tests.integration.web.test_selection_choice_refresh import selection_snapshot
from tests.support.identity import resolve_local_actor


class RecipeSelectionRefreshTests(TestCase):
    odoo_version = "19.0"
    def setUp(self):
        self.fixture = recipe_fixtures.RecipeTargetMatchBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.url = self.fixture.review_ready_delivery(odoo_version=self.odoo_version)
        self.client, self.context = self.fixture.client, self.fixture.context
        self.application = self.fixture.application
        self.actor = resolve_local_actor(self.context)
        self.schema = self.context.queries.get_odoo_schema_catalog(self.application.workspace_id)
        self.choices = (("en", "English"), ("fr_FR", "French"), ("it_IT", "Italian"))
        self.context.readiness_reader = lambda _state, _requests, _records: (
            selection_snapshot(self.schema, "res.partner", "lang", self.choices), None)

    def refresh(self, page):
        return self.client.post(self.url + "/refresh", data={
            **self.fixture.form(page),
            "target_model": "res.partner", "target_field": "lang",
        }, headers={"Origin": "http://testserver"})

    def form(self, page):
        form = self.fixture.form(page)
        match = re.search(r'name="selection_refreshes" value=\'([^\']*)\'', page.text)
        self.assertIsNotNone(match, page.text)
        form["selection_refreshes"] = html.unescape(match.group(1))
        return form

    def test_refresh_adds_language_and_accepts_only_run_owned_decisions(self):
        before_recipe = self.context.recipes.read_revision(self.application.recipe_id, self.application.recipe_revision, actor=self.actor)
        page = self.client.get(self.url)
        self.assertIn("Refresh languages from Odoo", page.text)
        refreshed = self.refresh(page)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertIn('value="it_IT"', refreshed.text)
        self.assertEqual(self.context.queries.get_odoo_schema_catalog(self.application.workspace_id), self.schema)
        form = self.form(refreshed)
        match = re.search(r'<select name="(match_[^"]+)"', refreshed.text)
        form[match.group(1)] = "fr_FR"
        accepted = self.client.post(self.url, data=form, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        current = self.context.queries.get_odoo_schema_catalog(self.application.workspace_id)
        self.assertNotEqual(current.content_hash, self.schema.content_hash)
        self.assertEqual(self.context.recipes.read_revision(self.application.recipe_id, self.application.recipe_revision, actor=self.actor), before_recipe)
        working = self.context.queries.get_mapping_working_draft(self.application.workspace_id)
        language = next(field for field in working.definition.datasets[0].fields if field.target_field == "lang")
        self.assertEqual({item.source_value: item.target_value for item in language.value_mappings}, {"French": "fr_FR", "en": "en"})

        self.schema = current
        before_revision = self.context.mapping_workspace.mappings.get_mapping_revision(self.application.workspace_id)
        unchanged = self.refresh(self.client.get(self.url))
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        accepted = self.client.post(self.url, data=self.form(unchanged), headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        self.assertEqual(self.context.mapping_workspace.mappings.get_mapping_revision(self.application.workspace_id), before_revision)
        self.schema = self.context.queries.get_odoo_schema_catalog(self.application.workspace_id)
        self.choices = (("fr_FR", "French"), ("it_IT", "Italian"))
        page = self.client.get(self.url)
        removed = self.refresh(page)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertIn("Previously en · unavailable in this Odoo.", removed.text)
        self.assertIn("1 value needs a match", removed.text)
        form = self.form(removed)
        match = re.search(r'<select name="(match_[^"]+)"', removed.text)
        form[match.group(1)] = "it_IT"
        accepted = self.client.post(self.url, data=form, headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(accepted.status_code, 303, accepted.text)
        working = self.context.queries.get_mapping_working_draft(self.application.workspace_id)
        language = next(field for field in working.definition.datasets[0].fields if field.target_field == "lang")
        self.assertEqual({item.source_value: item.target_value for item in language.value_mappings}, {"French": "fr_FR", "en": "it_IT"})


class RecipeSelectionRefreshOdoo20Tests(RecipeSelectionRefreshTests):
    odoo_version = "20.0"
