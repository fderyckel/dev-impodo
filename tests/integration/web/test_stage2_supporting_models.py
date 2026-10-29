from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import retain_databases_for_read
from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.contracts import SourceSelection
from tests.support.browser_scenarios import (
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
    _browser_model_catalog,
    _browser_schema,
    _register_workspace_through_services,
    _workspace_data_version_id,
)
from tests.support.identity import resolve_local_actor


class StageTwoSupportingModelBrowserTests(ProjectSetupBrowserTestCase):
    def test_stage_two_explains_direct_support_without_widening_write_scope(
        self,
    ) -> None:
        context = self.app.state.context
        created = self.workspaces.create(
            name="Supporting Odoo data",
            source_system="Fictional workbook",
        )
        now = datetime.now(timezone.utc)
        workspace = _register_workspace_through_services(
            context,
            created,
            connection_mode=OdooConnectionMode.LOCAL,
            base_url="http://127.0.0.1:8069",
            database="odoo19_local",
            intended_models=("product.template",),
        )
        context.sources.sources.save_source_selection(
            workspace.workspace_id,
            SourceSelection(
                selection_id=str(uuid4()),
                version=1,
                data_version_id=_workspace_data_version_id(
                    context,
                    workspace.workspace_id,
                ),
                created_at=now,
                created_by=resolve_local_actor(context).identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=resolve_local_actor(context),
        )
        model_snapshot = _browser_model_catalog(workspace)
        model_snapshot = replace(
            model_snapshot,
            records={
                "ir.model": (
                    *model_snapshot.records["ir.model"],
                    _model_record(10, "Product Category", "product.category"),
                )
            },
        )
        credential_hash = "sha256:" + "c" * 64
        context.schema_workspace.discover_models(
            workspace.workspace_id,
            model_snapshot,
            read_credential_binding_hash=credential_hash,
            actor=resolve_local_actor(context),
        )
        context.schema_workspace.capture(
            workspace.workspace_id,
            MetadataSnapshot(
                fingerprint=_browser_schema(workspace).fingerprint,
                models={
                    "product.template": ModelMetadata(
                        model="product.template",
                        description="Product",
                        fields={
                            "default_code": _field(
                                "default_code",
                                "Internal Reference",
                                "char",
                            ),
                            "categ_id": _field(
                                "categ_id",
                                "Product Category",
                                "many2one",
                                required=True,
                                relation="product.category",
                            ),
                            "uom_id": _field(
                                "uom_id",
                                "Unit of Measure",
                                "many2one",
                                required=True,
                                relation="uom.uom",
                            ),
                        },
                    )
                },
            ),
            read_credential_binding_hash=credential_hash,
            actor=resolve_local_actor(context),
        )

        saved_source = context.schema_workspace.sources.get_source_selection(
            workspace.workspace_id
        )
        saved_schema = context.schema_workspace.schemas.get_odoo_schema_catalog(
            workspace.workspace_id
        )
        actor = resolve_local_actor(context)
        with (
            patch.object(
                context.schema_workspace.sources, "get_source_catalogs",
                side_effect=AssertionError("catalogue preview forbidden"),
            ),
            patch.object(
                context.schema_workspace.sources, "get_mapping_source_selection",
                side_effect=AssertionError("mapping projection forbidden"),
            ),
            collect_duckdb_request_timings() as timings,
            retain_databases_for_read(),
        ):
            proposal = context.schema_workspace.supporting_data_plan(
                workspace.workspace_id,
                expected_source_selection_hash=saved_source.content_hash,
                expected_schema_hash=saved_schema.content_hash,
                expected_preparation_hash=None,
                actor=actor,
            )
        self.assertTrue(proposal.complete)
        self.assertEqual(
            {item.related_model for item in proposal.edges},
            {"product.category", "uom.uom"},
        )
        # Current lineage resolution owns the registry, canonical Workspace,
        # Data version package, and workspace engine. Each opens once.
        self.assertEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)

        page = self.client.get(f"/workspaces/{workspace.workspace_id}/schema")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Supporting data Impodo found", page.text)
        self.assertIn("Product Category", page.text)
        self.assertIn("Unit of Measure", page.text)
        self.assertIn("Keep linked value", page.text)
        self.assertIn("Reuse or create identity and required values only", page.text)
        self.assertNotIn("Include incoming data", page.text)
        self.assertNotIn("Reuse existing Odoo records", page.text)
        self.assertIn("1 Must fix", page.text)
        self.assertIn(
            "Impodo cannot find Unit of Measure in the current Odoo record-type list.",
            page.text,
        )
        self.assertIn("Matching rules cannot be confirmed yet", page.text)
        self.assertIn("Fields:", page.text)
        self.assertIn("Product: Unit of Measure", page.text)
        self.assertIn(
            'disabled aria-describedby="matching-rules-blocked-reason"',
            page.text,
        )
        self.assertIn(
            "This preview does not add a related record type to the top-level transfer",
            page.text,
        )
        self.assertEqual(
            context.queries.get(workspace.workspace_id).intended_models,
            ("product.template",),
        )

        unrelated = self.client.get(
            f"/workspaces/{workspace.workspace_id}/schema"
            "?suggested_model=res.company#odoo-data-choices"
        )
        self.assertNotIn("Recommended supporting data", unrelated.text)
        self.assertEqual(
            context.queries.get(workspace.workspace_id).intended_models,
            ("product.template",),
        )

        proposed = self.client.get(
            f"/workspaces/{workspace.workspace_id}/schema"
            "?suggested_model=product.category#odoo-data-choices"
        )
        self.assertNotIn("Recommended supporting data", proposed.text)
        self.assertEqual(
            context.queries.get(workspace.workspace_id).intended_models,
            ("product.template",),
        )


def _model_record(odoo_id: int, label: str, model: str) -> TargetRecord:
    return TargetRecord(
        model="ir.model",
        odoo_id=odoo_id,
        values={
            "name": label,
            "model": model,
            "abstract": False,
            "transient": False,
            "modules": "product",
            "state": "base",
        },
    )


def _field(
    name: str,
    label: str,
    field_type: str,
    *,
    required: bool = False,
    relation: str | None = None,
) -> FieldMetadata:
    return FieldMetadata(
        name=name,
        type=field_type,
        label=label,
        required=required,
        readonly=False,
        relation=relation,
        stored=True,
        computed=False,
        has_inverse=False,
        related=False,
        translated=False,
        company_dependent=False,
        searchable=True,
        sortable=True,
        exportable=True,
    )


if __name__ == "__main__":
    import unittest

    unittest.main()
