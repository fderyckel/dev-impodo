"""Show the real failed check and exact linked-field correction instead of a save loop."""

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.mapping.contracts import (
    ConstantBusinessReference,
    ConstantReferenceComponent,
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetRecord,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.diagnostics import (
    LocalDiagnosticRecorder,
    RequestDiagnosticsMiddleware,
    create_diagnostic_bundle,
)
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
)
from tests.support.hierarchy_correction import MappingForm
from tests.support.identity import resolve_local_actor


class SupportingConflictRecoveryTests(ProjectSetupBrowserTestCase):
    def configure(self, major, mode):
        workspace_id, source, key = self._mapping_ready_workspace(
            scalar_field_count=1,
            target_model="mrp.bom",
            odoo_major=major,
            connection_mode=mode,
            relationship_field_count=6,
            relationship_model="product.template",
            relationship_field_names=(
                *(f"x_optional_{i}" for i in range(5)),
                "product_tmpl_id",
            ),
            relationship_field_labels=(
                *(f"A optional {i}" for i in range(5)),
                "Product",
            ),
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        schema = replace(
            schema, models=(replace(schema.models[0], label="Bill of Material"),)
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        state = context.queries.get(workspace_id)
        state = context.workspace_states.select_odoo_company(
            workspace_id,
            expected_revision=state.revision,
            actor=actor,
            selection=OdooCompanySelection(
                schema.connection_target_hash, 105, "Example Factory"
            ),
        )
        schema = replace(schema, read_context_hash=state.odoo_company.context_hash)
        context.schema_workspace.schemas.save_odoo_create_defaults(
            workspace_id,
            schema,
            expected_content_hash=schema.content_hash,
            expected_company_context_hash=state.odoo_company.context_hash,
            expected_read_credential_binding_hash=schema.read_credential_binding_hash,
            actor=actor,
        )
        packet = context.schema_workspace.supporting_review_inputs(
            workspace_id, actor=actor
        )
        from impodo.domain.workspace.supporting_review import SupportingChoice

        run_local_operation_sync(
            context.schema_workspace.supporting.save,
            packet,
            expected_binding_hash=packet.binding_hash,
            expected_version=0,
            operation_id=str(uuid4()),
            choices=(SupportingChoice("mrp.bom", "product_tmpl_id", "REUSE"),),
            actor=actor,
        )
        run_local_operation_sync(
            context.schema_workspace.govern_complete,
            workspace_id,
            business_keys=(key,),
            expected_catalog_hash=schema.content_hash,
            actor=actor,
        )
        mapping = DatasetMapping(
            source.dataset_id,
            "mrp.bom",
            source_identity_column_keys=(source.columns[0].stable_key,),
            target_identity=(
                IdentityComponentMapping((source.columns[0].stable_key,), ("ref",)),
            ),
            fields=(
                ScalarFieldMapping(
                    "field_0000",
                    value_source=ScalarValueSource.CONSTANT,
                    literal_value="Keep this unsaved title",
                ),
            ),
        )
        run_local_operation_sync(
            context.mapping_workspace.save_working_draft,
            workspace_id,
            datasets=(mapping,),
            expected_version=None,
            actor=actor,
        )
        fingerprint = replace(
            _browser_schema(state).fingerprint, odoo_version=f"{major}.0"
        )
        context.readiness_reader = lambda *_args: (
            MetadataSnapshot(
                fingerprint,
                {
                    "product.template": ModelMetadata(
                        "product.template",
                        "Product",
                        {
                            "name": FieldMetadata(
                                "name", "char", label="Name", required=True
                            )
                        },
                    )
                },
            ),
            RecordSnapshot(
                fingerprint,
                {
                    "product.template": (
                        TargetRecord("product.template", 211, {"name": "Example"}),
                    )
                },
                {"product.template": ("name",)},
            ),
        )
        context.read_identity_probe = lambda _state, _secret, models: OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash,
            readable_models=tuple(models),
            observed_at="2026-11-05T10:00:00Z",
        )
        return workspace_id, source, mapping

    def test_confirm_check_conflict_names_field_and_reopens_real_blocker_for_both_majors(
        self,
    ):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        recorder = LocalDiagnosticRecorder(Path(self.temporary.name) / "diagnostics")
        self.addCleanup(recorder.close)
        self.app.middleware_stack = RequestDiagnosticsMiddleware(
            self.app.middleware_stack, recorder=recorder
        )
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, _source, mapping = self.configure(major, mode)
                    url = f"/workspaces/{workspace_id}/mapping?relation_page_size=3"
                    page = self.client.get(url)
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("Bill of Material · Product", page.text)
                    self.assertNotIn(
                        "Saved changes have not been checked yet", page.text
                    )
                    form = MappingForm(page.text)
                    rejected = self.client.post(
                        form.action,
                        json={"entries": [*form.entries, ["action", "draft"]]},
                        headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                    )
                    self.assertEqual(rejected.status_code, 422, rejected.text[:1000])
                    self.assertEqual(
                        rejected.json()["failure_code"], "SUPPORTING_MAPPING_CONFLICT"
                    )
                    item = rejected.json()["supporting_conflicts"][0]
                    self.assertEqual(item["title"], "Bill of Material · Product")
                    self.assertEqual(item["field"], "product_tmpl_id")
                    self.assertIn("relation_target=product_tmpl_id", item["fix_url"])
                    self.assertIn("step=tables&task=links", item["fix_url"])
                    fragment = self.client.get(
                        f"/workspaces/{workspace_id}/mapping/supporting-review?problem=MAPPING_CONFLICT"
                    )
                    metrics = next(
                        json.loads(line)
                        for line in recorder.path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if json.loads(line).get("request_id")
                        == fragment.headers["X-Impodo-Request-ID"]
                    )
                    self.assertGreater(metrics["database_connection_count"], 0)
                    self.assertLessEqual(metrics["database_connection_count"], 6)
                    self.assertLessEqual(metrics["database_schema_check_count"], 6)
                    self.assertEqual(metrics["database_lock_retry_count"], 0)
                    self.assertIn("Review this field", fragment.text)
                    self.assertNotIn("Confirm choices and check matches", fragment.text)
                    self.assertIn("Bill of Material · Product", fragment.text)
                    reopened = self.client.get(url)
                    self.assertNotIn(
                        "Saved changes have not been checked yet", reopened.text
                    )
                    self.assertEqual(
                        reopened.context["next_step"]["blockers"][0]["title"],
                        "Correct the linked fields",
                    )
                    self.assertEqual(
                        context.queries.get_mapping_working_draft(
                            workspace_id
                        ).definition.datasets[0],
                        mapping,
                    )
                    target = self.client.get(item["fix_url"])
                    self.assertIn('id="relationship-0-product_tmpl_id"', target.text)
                    self.assertIn("Keep this unsaved title", target.text)
                    # Correct the actual provider, then retry through the same HTTP check.
                    draft = context.queries.get_mapping_working_draft(workspace_id)
                    corrected = replace(
                        mapping,
                        relationships=(
                            RelationshipMapping(
                                "product_tmpl_id",
                                "many2one",
                                (),
                                RelationshipResolver(
                                    ResolverOrigin.TARGET_CATALOG,
                                    model="product.template",
                                ),
                                value_source=RelationshipValueSource.CONSTANT_EXISTING,
                                constant_reference=ConstantBusinessReference(
                                    key_values=(
                                        ConstantReferenceComponent("name", "Example"),
                                    )
                                ),
                            ),
                        ),
                    )
                    run_local_operation_sync(
                        context.mapping_workspace.save_working_draft,
                        workspace_id,
                        datasets=(corrected,),
                        expected_version=draft.version,
                        actor=actor,
                    )
                    current = self.client.get(item["fix_url"])
                    corrected_form = MappingForm(current.text)
                    checked = self.client.post(
                        corrected_form.action,
                        json={
                            "entries": [*corrected_form.entries, ["action", "draft"]]
                        },
                        headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
                    )
                    self.assertEqual(checked.status_code, 200, checked.text[:1000])
                    self.assertFalse(
                        self.client.get(url).context["related_data_review"][
                            "mapping_conflict"
                        ]
                    )
        with ZipFile(
            BytesIO(
                create_diagnostic_bundle(
                    recorder.path.parent, build_contract=PROCESS_BUILD_CONTRACT
                )
            )
        ) as archive:
            failures = json.loads(archive.read("recent-failures.json"))
        conflicts = [
            item["validation_failure"]
            for item in failures
            if item.get("validation_failure", {}).get("failure_code")
            == "SUPPORTING_MAPPING_CONFLICT"
        ]
        self.assertEqual(len(conflicts), 4)
        self.assertTrue(
            all(item["issues"][0]["field"] == "product_tmpl_id" for item in conflicts)
        )
