"""Checked metadata never claims that target Python constraints were checked."""

from dataclasses import replace

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class MappingConstraintCoverageBrowserTests(ProjectSetupBrowserTestCase):
    def configure(self, major=19, mode=OdooConnectionMode.LOCAL):
        workspace_id, dataset, key = self._mapping_ready_workspace(
            scalar_field_count=0, connection_mode=mode
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        schema = replace(
            schema,
            odoo_version=f"{major}.0",
            content_hash="sha256:" + str(major)[-1] * 64,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        context.schema_workspace.govern_complete(
            workspace_id,
            business_keys=(key,),
            expected_catalog_hash=schema.content_hash,
            actor=actor,
        )
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            mode=MappingTargetMode.CREATE,
            on_existing="block",
            source_identity_column_keys=(dataset.columns[0].stable_key,),
            target_identity=(
                IdentityComponentMapping((dataset.columns[0].stable_key,), ("ref",)),
            ),
        )
        _revision, validation = run_local_operation_sync(
            context.mapping_workspace.check_definition,
            workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=actor,
        )
        self.assertNotEqual(validation.status.value, "INVALID", validation.issues)
        return workspace_id

    def test_checked_page_identifies_unknown_rules_and_reviewed_exclusion_path(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id = self.configure(major, mode)
                    page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertIn("What this match check covers", page.text)
                    self.assertIn("Not checked:</strong> server VAT rules", page.text)
                    self.assertIn("#rows-to-use-0", page.text)
                    self.assertIn("Exclude their exact source keys", page.text)
                    self.assertNotIn("VAT values passed", page.text)

    def test_recipe_coverage_does_not_offer_mutation_of_frozen_selection(self):
        template = self.app.state.templates.get_template(
            "mapping/_odoo_validation_coverage.html"
        )
        rendered = template.render(
            migration_workspace=type(
                "RecipeWorkspace", (), {"recipe_application_id": "recipe-application"}
            )(),
            dataset_views=(),
        )
        self.assertIn("revise its", rendered)
        self.assertNotIn("#rows-to-use", rendered)
