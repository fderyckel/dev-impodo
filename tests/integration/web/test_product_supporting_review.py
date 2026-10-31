"""Keep generated Product variants reviewable through schema and Match data."""

from dataclasses import replace

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.workspace.contracts import SchemaField
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import (
    POST_HEADERS,
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.identity import resolve_local_actor


def product_variant_workspace(fixture, version="20.0+e"):
    """Install a captured standard Product shape that previously had no choice."""
    workspace_id, dataset, key = fixture._mapping_ready_workspace(
        scalar_field_count=2,
        target_model="product.template",
        connection_mode=OdooConnectionMode.REMOTE,
    )
    context = fixture.app.state.context
    actor = resolve_local_actor(context)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    variants = SchemaField(
        "product_variant_ids",
        "Products",
        "one2many",
        True,
        False,
        "product.product",
        "product_tmpl_id",
        (),
        stored=True,
        computed=False,
        related=False,
    )
    schema = replace(
        schema,
        odoo_version=version,
        content_hash="sha256:" + "8" * 64,
        models=(
            replace(
                schema.models[0],
                label="Product",
                fields=(*schema.models[0].fields, variants),
            ),
        ),
    )
    context.schema_workspace.schemas.save_odoo_schema_catalog(
        workspace_id, schema, actor=actor
    )
    return workspace_id, dataset, key, schema


class ProductSupportingReviewTests(ProjectSetupBrowserTestCase):
    def test_generated_variants_have_a_savable_choice_and_allow_matching(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for version in ("19.0+e", "20.0+e"):
            with self.subTest(version=version):
                workspace_id, dataset, key, schema = product_variant_workspace(
                    self, version
                )
                before = len(self.readiness_calls)
                with collect_duckdb_request_timings() as timings:
                    page = self.client.get(f"/workspaces/{workspace_id}/schema")
                self.assertEqual(page.status_code, 200, page.text)
                self.assertLessEqual(timings.connection_count, 6)
                self.assertLessEqual(timings.schema_check_count, 6)
                self.assertEqual(timings.lock_retry_count, 0)
                view = page.context["supporting_review"]
                self.assertEqual(len(view["rows"]), 1)
                row = view["rows"][0]
                self.assertEqual(row["actions"], (("ODOO_MANAGED", "Handled by Odoo"),))
                self.assertTrue(row["managed"])
                self.assertIn("Odoo supplies this relationship", page.text)
                self.assertIn("matching-rules-blocked-reason", page.text)
                saved = self.client.post(
                    f"/workspaces/{workspace_id}/schema/supporting/save",
                    headers=POST_HEADERS,
                    follow_redirects=False,
                    data={
                        "csrf_token": self.csrf,
                        "binding_hash": view["binding_hash"],
                        "expected_version": str(view["version"]),
                        "operation_id": view["operation_id"],
                        "choice_count": "1",
                        "edge_0": "product.template|product_variant_ids",
                        "action_0": "ODOO_MANAGED",
                    },
                )
                self.assertEqual(saved.status_code, 303, saved.text)
                reopened = self.client.get(saved.headers["location"])
                self.assertTrue(reopened.context["supporting_review"]["current"])
                self.assertNotIn("matching-rules-blocked-reason", reopened.text)
                self.assertEqual(
                    context.queries.get(workspace_id).intended_models,
                    ("product.template",),
                )
                run_local_operation_sync(
                    context.schema_workspace.govern_complete,
                    workspace_id,
                    business_keys=(key,),
                    expected_catalog_hash=schema.content_hash,
                    actor=actor,
                )
                run_local_operation_sync(
                    context.mapping_workspace.supporting_decisions.require_mapping,
                    workspace_id,
                    (DatasetMapping(dataset.dataset_id, "product.template"),),
                    schema,
                    None,
                )
                self.assertEqual(len(self.readiness_calls), before)
