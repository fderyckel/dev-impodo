"""Bind manufacturing practice to controlled metadata or actual local Odoo.

Only the controlled fixture substitutes the Odoo boundary. Live capture,
access, company selection, defaults, readers and writers use the same normal
application services as the customer qualification. No browser or server is
started here.
"""

from dataclasses import asdict
from datetime import UTC, datetime

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.web.composition.odoo_company import read_company_access
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.routers.schema import (
    _capture_selected_schema_sync,
    _refresh_model_catalog_sync,
)
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from scripts.practice_customer_live import LivePracticeCustomerWorkflow
from scripts.practice_customer_workflow import COMPANY_NAMES
from scripts.practice_manufacturing_workflow import PracticeManufacturingWorkflow
from tests.support.browser_scenarios import _browser_schema

MANUFACTURING_MODELS = (
    "res.company",
    "product.category",
    "product.template",
    "product.product",
    "product.tag",
    "uom.uom",
    "mrp.workcenter",
    "mrp.bom",
    "mrp.bom.line",
    "mrp.routing.workcenter",
)


class ControlledManufacturingWorkflow(PracticeManufacturingWorkflow):
    """Publish independent exact-major boundary metadata in isolated app storage."""

    def install_manufacturing_schema(
        self, workspace_id, *, target, major, mode, company, business_keys
    ):
        context, actor = self.context, self.actor
        state = context.queries.get(workspace_id)
        state = context.workspace_states.update_target(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode,
            odoo_base_url="http://127.0.0.1:8069"
            if mode == "LOCAL"
            else "https://odoo.example.test",
            odoo_database=f"fictional_morrow_manufacturing_{major}",
            intended_applications=(),
            intended_models=MANUFACTURING_MODELS,
        )
        target_hash = _browser_schema(state).fingerprint.target_hash
        selected = OdooCompanySelection(
            target_hash, target.company_ids[company], COMPANY_NAMES[company]
        )
        state = context.workspace_states.select_odoo_company(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=selected,
        )
        target.operation_company = target.company_ids[company]
        owner = context.target_credential_workspace(
            workspace_id, workspace_state=state, actor=actor
        )
        read = store_target_credential(
            context.secret_store,
            owner,
            TargetCredentialRole.READ,
            "fictional-manufacturing-read-key",
            persistent=False,
        )
        models = tuple(
            SchemaModel(
                model,
                model,
                tuple(
                    SchemaField(
                        name=name,
                        label=name,
                        type=field["type"],
                        required=field.get("required", False),
                        readonly=field.get("readonly", False),
                        relation=field.get("relation"),
                        relation_field=field.get("relation_field"),
                        selection=tuple(
                            tuple(choice) for choice in field.get("selection", ())
                        ),
                        stored=field.get("store"),
                    )
                    for name, field in fields.items()
                ),
            )
            for model, fields in target.fields.items()
        )
        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=odoo_source_policy_hash(major),
            captured_at=datetime.now(UTC),
            captured_by="Controlled manufacturing operator",
            connection_mode=mode,
            database=state.odoo_database,
            odoo_version=f"{major}.0",
            models=models,
            content_hash=content_hash((major, [asdict(item) for item in models])),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=read.binding_hash,
            read_principal_hash=content_hash(("manufacturing-read", major, mode)),
            read_permission_hash=content_hash(
                ("manufacturing-permissions", major, mode)
            ),
            read_context_hash=selected.context_hash,
            connection_target_hash=target_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        context.schema_workspace.govern(
            workspace_id, actor=actor, business_keys=business_keys
        )
        return schema


class LiveManufacturingWorkflow(
    PracticeManufacturingWorkflow, LivePracticeCustomerWorkflow
):
    """Capture the actual version, fields, defaults, access and company context."""

    def install_manufacturing_schema(
        self, workspace_id, *, target, major, mode, company, business_keys
    ):
        del target
        self.assertEqual((major, mode), (self.connection.major, self.connection.mode))
        context, actor = self.context, self.actor
        state = run_local_operation_sync(
            lambda: context.workspace_states.update_target(
                workspace_id,
                actor=actor,
                expected_revision=context.queries.get(workspace_id).revision,
                odoo_connection_mode=mode,
                odoo_base_url=self.connection.base_url,
                odoo_database=self.connection.database,
                intended_applications=(),
                intended_models=MANUFACTURING_MODELS,
            )
        )
        self.bind_local(workspace_id)
        owner = run_local_operation_sync(
            context.target_credential_workspace,
            workspace_id,
            workspace_state=state,
            actor=actor,
        )
        store_target_credential(
            self.secrets,
            owner,
            TargetCredentialRole.READ,
            self.connection.read_key,
            persistent=True,
        )
        if mode == "LOCAL":
            profile = context.local_stack.get(workspace_id).profile
            access = context.local_odoo_reader.get_company_access(state, profile)
        else:
            access = read_company_access(state, self.connection.read_key)
        choices = [
            choice for choice in access.choices if choice.name == COMPANY_NAMES[company]
        ]
        self.assertEqual(len(choices), 1, "MANUFACTURING_COMPANY_AMBIGUOUS")
        state = run_local_operation_sync(
            context.workspace_states.select_odoo_company,
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=choices[0],
        )
        _refresh_model_catalog_sync(context, state, actor=actor)
        schema = _capture_selected_schema_sync(context, state, actor=actor)
        version = recognize_odoo_version(schema.odoo_version)
        self.assertEqual(
            (version.major, version.release_stage), (major, OdooReleaseStage.FINAL)
        )
        run_local_operation_sync(
            context.schema_workspace.govern,
            workspace_id,
            actor=actor,
            business_keys=business_keys,
        )
        return schema
