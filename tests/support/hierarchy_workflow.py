"""Build accepted fictional files and real hierarchy workflow evidence."""

from dataclasses import asdict
from datetime import UTC, datetime

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
    _created_workspace_id,
)
from tests.support.identity import resolve_local_actor

PRODUCT_ROWS = (
    ("PX101", "Machines", "Pumps", "Service", "Seals"),
    ("PX102", "Machines", "Valves", "Service", "Seals"),
    ("PX103", "Maintenance", "Pumps", "Service", "Seals"),
    ("PX104", "Machines", "Pumps", "", ""),
    ("PX105", "Machines", "", "", ""),
    ("PX106", "Tools", "", "", ""),
)
EXISTING_CATEGORY_PATHS = (
    ("Machines",),
    ("Machines", "Pumps"),
    ("Machines", "Pumps", "Service"),
    ("Machines", "Pumps", "Service", "Seals"),
    ("Maintenance",),
)


def product_csv(rows=PRODUCT_ROWS):
    return (
        "Reference,Level1,Level2,Level3,Level4\n"
        + "\n".join(",".join(row) for row in rows)
        + "\n"
    ).encode()


class HierarchyWorkflowFixture(ProjectSetupBrowserTestCase):
    """Keep each scenario in isolated real repositories and an authenticated app."""

    def setUp(self):
        super().setUp()
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)

    def freeze_files(self, files):
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Fictional hierarchy",
                "source_mode": "FILE",
                "source_system_identity": "Fictional workshop",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files=[
                ("source_file", (name + ".csv", value, "text/csv"))
                for name, value in files.items()
            ],
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {
                "csrf_token": self.csrf,
                "revision": str(self.context.queries.get(workspace_id).revision),
            },
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        catalogs = self.context.queries.get_source_catalogs(workspace_id)
        for catalog in catalogs:
            result = self._post(
                f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
                {
                    "csrf_token": self.csrf,
                    "action": "confirm",
                    "encoding": "utf-8",
                    "delimiter": ",",
                    "header_row_0": "1",
                    "selected_0": "1",
                },
            )
            self.assertEqual(result.status_code, 303, result.text)
        frozen = self._post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            {
                "csrf_token": self.csrf,
                **{
                    f"dataset_name_{index}": catalog.display_name.removesuffix(".csv")
                    for index, catalog in enumerate(catalogs)
                },
            },
        )
        self.assertEqual(frozen.status_code, 303, frozen.text)
        return workspace_id

    def schema(
        self, workspace_id, *, major=19, mode="LOCAL", extra_models=(), extra_keys=()
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
            odoo_database=f"fictional_odoo{major}",
            intended_applications=(),
            intended_models=(
                "product.category",
                "product.template",
                *(model.name for model in extra_models),
            ),
        )

        def field(name, kind="char", relation=None, required=False):
            return SchemaField(name, name, kind, required, False, relation, None, ())

        models = (
            SchemaModel(
                "product.category",
                "Category",
                (
                    field("name", required=True),
                    field("parent_id", "many2one", "product.category"),
                ),
            ),
            SchemaModel(
                "product.template",
                "Product",
                (
                    field("default_code"),
                    field("name", required=True),
                    field("categ_id", "many2one", "product.category"),
                ),
            ),
            *extra_models,
        )
        owner = context.target_credential_workspace(
            workspace_id, workspace_state=state, actor=actor
        )
        read = store_target_credential(
            context.secret_store,
            owner,
            TargetCredentialRole.READ,
            "fictional-read-key",
            persistent=False,
        )
        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=content_hash("policy"),
            captured_at=datetime.now(UTC),
            captured_by="Fictional operator",
            connection_mode=mode,
            database=state.odoo_database,
            odoo_version=f"{major}.0",
            content_hash=content_hash(
                (workspace_id, major, [asdict(model) for model in models])
            ),
            models=models,
            origin=SchemaOrigin.LIVE_API,
            connection_target_hash=_browser_schema(state).fingerprint.target_hash,
            read_credential_binding_hash=read.binding_hash,
            read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"),
            read_context_hash=content_hash("context"),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        context.schema_workspace.govern(
            workspace_id,
            actor=actor,
            business_keys=(
                BusinessKeyDefinition(
                    "category:name-parent",
                    "product.category",
                    ("name",),
                    scope_fields=("parent_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "product:reference",
                    "product.template",
                    ("default_code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                *extra_keys,
            ),
        )
        return schema

    def submit(self, workspace_id, mappings):
        current = self.context.mapping_workspace.mappings.get_mapping_revision(
            workspace_id
        )
        working = self.context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        revision, validation = run_local_operation_sync(
            self.context.mapping_workspace.check_definition,
            workspace_id,
            datasets=tuple(mappings),
            expected_parent_version=current.version if current else None,
            expected_working_draft_version=working.version if working else None,
            actor=self.actor,
        )
        self.assertNotEqual(validation.status.value, "INVALID", validation.issues)
        draft = self.context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        run_local_operation_sync(
            self.context.mapping_workspace.submit_current,
            workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=draft.version,
            actor=self.actor,
            warning_acknowledgements=tuple(
                mapping_issue_fingerprint(issue)
                for issue in validation.issues
                if issue.severity == "warning"
            ),
        )
        return revision

    def hierarchy(self, *, major=19, mode="LOCAL", rows=PRODUCT_ROWS):
        workspace_id = self.freeze_files({"products": product_csv(rows)})
        self.schema(workspace_id, major=major, mode=mode)
        physical = self.context.queries.get_source_selection(workspace_id)
        products = physical.datasets[0]
        columns = {column.source_name: column.stable_key for column in products.columns}
        plan, _rule = run_local_operation_sync(
            self.context.derived_entities.save_hierarchy,
            workspace_id,
            output_dataset_name="categories",
            source_dataset_id=products.dataset_id,
            source_level_column_keys=tuple(
                columns[f"Level{index}"] for index in range(1, 5)
            ),
            target_model="product.category",
            target_name_field="name",
            external_id_namespace="fictional_workshop",
            missing_parent_mode="promote",
            missing_parent_value=None,
            missing_leaf="use_deepest",
            all_blank_mode="emit_null_reference",
            all_blank_value=None,
            expected_parent_version=None,
            actor=self.actor,
        )
        link = derived_dataset_links(plan)[0]
        parent = RelationshipResolver(
            origin=ResolverOrigin.DATASET, dataset_id=link.derived_dataset_id
        )
        revision = self.submit(
            workspace_id,
            (
                DatasetMapping(
                    products.dataset_id,
                    "product.template",
                    source_identity_column_keys=(columns["Reference"],),
                    target_identity=(
                        IdentityComponentMapping(
                            (columns["Reference"],), ("default_code",)
                        ),
                    ),
                    fields=(
                        ScalarFieldMapping("name", columns["Reference"], required=True),
                    ),
                    relationships=(
                        RelationshipMapping(
                            "categ_id",
                            "many2one",
                            (link.source_column_key,),
                            resolver=parent,
                            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                        ),
                    ),
                ),
                DatasetMapping(
                    link.derived_dataset_id,
                    "product.category",
                    source_identity_column_keys=(link.canonical_key_column_key,),
                    target_identity=(
                        IdentityComponentMapping((link.name_column_key,), ("name",)),
                    ),
                    target_scope=(
                        IdentityComponentMapping(
                            (link.parent_key_column_key,),
                            ("parent_id",),
                            resolver=parent,
                            null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                        ),
                    ),
                ),
            ),
        )
        return workspace_id, revision

    def prepare(self, workspace_id):
        run_local_operation_sync(
            self.context.preparation.prepare, workspace_id, actor=self.actor
        )
        summary = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        staging = self.context.preparation.staging.get_canonical_staging_run(
            workspace_id, summary.run_id, expected_content_hash=summary.content_hash
        )
        return staging, self.context.quality.current_summary(workspace_id)

    def approve(self, workspace_id):
        summary, evaluation, _ = self.context.normalization.current_review(workspace_id)
        for group in evaluation.groups:
            if group.requires_decision:
                summary = self.context.normalization.decide_group(
                    workspace_id,
                    summary.run_id,
                    group.group_id,
                    approve=True,
                    expected_version=summary.lifecycle_version,
                    actor=self.actor,
                )
        return self.context.normalization.approve(
            workspace_id,
            summary.run_id,
            expected_version=summary.lifecycle_version,
            actor=self.actor,
        )
