from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.contracts import SourceSelection
from tests.support.browser_scenarios import (
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
    WorkspaceStatus,
    _browser_model_catalog,
    _browser_schema,
    _replace_run_target_setup,
    _workspace_data_version_id,
)


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
        workspace = replace(
            created,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("product.template",),
            status=WorkspaceStatus.REGISTERED,
            revision=created.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        context.workspace_states.repository.save(
            workspace,
            expected_revision=created.revision,
            event_type="WORKSPACE_REGISTERED",
            event_detail="",
            actor=context.actor,
        )
        _replace_run_target_setup(
            context,
            workspace.workspace_id,
            connection_mode=OdooConnectionMode.LOCAL,
            base_url=workspace.odoo_base_url,
            database=workspace.odoo_database,
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
                created_by=context.actor.identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=context.actor,
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
            actor=context.actor,
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
            actor=context.actor,
        )

        page = self.client.get(f"/workspaces/{workspace.workspace_id}/schema")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Supporting data Impodo found", page.text)
        self.assertIn("Product Category", page.text)
        self.assertIn("Unit of Measure", page.text)
        self.assertIn("Review in Match data", page.text)
        self.assertIn("Include incoming data", page.text)
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
            "Reusing related Odoo records does not authorize Impodo to update them",
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
        self.assertIn("Recommended supporting data", proposed.text)
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
