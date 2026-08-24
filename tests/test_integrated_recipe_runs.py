"""Verify multi-Recipe Test planning and isolated application workspaces."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4, uuid5

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.cutover_plan_repository import CutoverPlanRepository
from impodo.adapters.duckdb.migration_run_planning_repository import (
    MigrationRunPlanningRepository,
)
from impodo.adapters.duckdb.migration_workspace_engine_database import (
    MigrationWorkspaceEngineDatabase,
)
from impodo.adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from impodo.adapters.duckdb.recipe_repository import RecipeRepository
from impodo.adapters.duckdb.test_run_repository import TestRunRepository
from impodo.adapters.protected_recipe_store import ProtectedRecipeStore
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from impodo.application.migration_run_planning_service import (
    MigrationRunPlanningService,
)
from impodo.application.mapping_workspace_service import MappingWorkspaceService
from impodo.application.recipe_application_service import (
    RecipeApplicationService,
    RecipeApplicationAssessment,
    RecipeMaterialization,
)
from impodo.application.recipe_publication_service import (
    RecipePublicationService,
)
from impodo.application.recipe_compilation_service import CompiledRecipeDefinition
from impodo.application.test_run_setup_service import TestRunSetupService
from impodo.data_version_sources import (
    DataVersionSourcePackage,
    DataVersionSourcePackageService,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageDataset,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
    WorkspaceSourceProjectionService,
    source_column_contract_hash,
)
from impodo.data_versions import (
    DataVersionPurpose,
    DataVersionService,
    DataVersionState,
)
from impodo.domain.serialization import content_hash
from impodo.domain.coverage import (
    ReferenceBundle,
    ReferenceDataSet,
    ReferenceEntry,
    ReferenceValueKind,
)
from impodo.domain.recipe_applications import RecipeControlValues
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.source_binding import FileSourceBinding
from impodo.migration_foundation import (
    MigrationConflictError,
    MigrationOperationState,
    utc_now,
)
from impodo.migration_projects import MigrationProjectService
from impodo.migration_run_planning import (
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    MigrationRunPlanningError,
    OdooModelRequirement,
    ReferenceRequirement,
    RecipeApplicationStatus,
    RecipeDependency,
)
from impodo.migration_runs import MigrationRunService
from impodo.migration_workspaces import MigrationWorkspaceService
from impodo.recipes import RecipeService
from impodo.workspace_state import WorkspaceStateService
from impodo.secrets import MemorySecretStore
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from tests.workspace_access_helpers import workspace_access_service
from impodo.web.app import create_local_app
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


class SimulatedCrash(RuntimeError):
    pass


class RecipeApplicationServiceTests(unittest.TestCase):
    """Exercise the retained compiler with the accepted Customer envelope."""

    def test_customer_recipe_assesses_current_sources_target_and_references(self):
        definition = json.loads(
            (
                ROOT
                / "fixtures"
                / "migration-projects"
                / "current-contract"
                / "customer-recipe-v1.json"
            ).read_text(encoding="utf-8")
        )["recipe"]
        reference = ReferenceDataSet(
            reference_id=str(uuid4()),
            version=1,
            name="Customer type",
            key_fields=("source_value",),
            value_kinds={
                "target_value": ReferenceValueKind.ODOO_SELECTION_KEY,
            },
            entries=(
                ReferenceEntry(
                    key=("Company",),
                    values={"target_value": "company"},
                ),
            ),
            owner="Data manager",
            classification="INTERNAL",
            effective_label="Integrated Test",
        )
        definition["reference_dependencies"]["references"][0][
            "content_hash"
        ] = reference.content_hash
        source_shape = definition["source_shape"]["datasets"][0]
        workspace_id = str(uuid4())
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=str(uuid4()),
            created_at=datetime.now(timezone.utc),
            created_by="Data manager",
            datasets=(
                SourceDataset(
                    dataset_id="test-customers",
                    name="Customers",
                    source=FileSourceBinding(
                        file_id=str(uuid4()),
                        table_key="customers",
                        source_sha256=content_hash("customer bytes"),
                        catalog_hash=content_hash("customer catalog"),
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=10,
                    columns=tuple(
                        SourceDatasetColumn(
                            ordinal,
                            item["source_name"],
                            f"current:{item['source_name']}",
                            item["candidate_type_hint"],
                        )
                        for ordinal, item in enumerate(
                            source_shape["columns"],
                            start=1,
                        )
                    ),
                ),
            ),
            content_hash=content_hash("current source selection"),
        )
        models = tuple(
            SchemaModel(
                name=model["model"],
                label=model["model"],
                fields=tuple(
                    SchemaField(
                        name=field["name"],
                        label=field["name"],
                        type=field["field_type"],
                        required=field["required"],
                        readonly=field["readonly"],
                        relation=field.get("relation_model"),
                        relation_field=None,
                        selection=tuple(
                            (value, value)
                            for value in field.get(
                                "required_selection_codes",
                                (),
                            )
                        ),
                    )
                    for field in model["fields"]
                ),
            )
            for model in definition["odoo_target_contract"]["models"]
        )
        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=content_hash("schema policy"),
            captured_at=datetime.now(timezone.utc),
            captured_by="Data manager",
            connection_mode="REMOTE",
            database="fictional_test",
            odoo_version="19.0",
            models=models,
            content_hash=content_hash("schema evidence"),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=content_hash("read credential"),
            read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"),
            read_context_hash=content_hash("context"),
            connection_target_hash=content_hash("target"),
        )
        compiler = RecipeApplicationService(
            sources=SimpleNamespace(),
            schemas=SimpleNamespace(),
            schema_workspace=SimpleNamespace(),
            references=SimpleNamespace(),
            preparation=SimpleNamespace(),
            mappings=SimpleNamespace(),
            categorical=SimpleNamespace(),
            application_state=SimpleNamespace(),
        )

        assessment = compiler.assess(
            recipe_id=str(uuid4()),
            definition=definition,
            source_selection=selection,
            target_schema=schema,
            reference_bundle=ReferenceBundle(
                workspace_id=workspace_id,
                datasets=(reference,),
            ),
            parameter_values={
                "parameter:batch_reference": "TEST-2026-08-22",
                "parameter:export_as_of_date": "2026-08-20",
            },
            control_values={
                "control:customers.open_balance": "100.00",
            },
        )

        self.assertFalse(assessment.blocked, assessment.issues)
        self.assertEqual(assessment.dataset_ids, ("test-customers",))
        self.assertEqual(
            compiler.reference_requirements(definition),
            (
                ReferenceRequirement(
                    name=reference.name,
                    content_hash=reference.content_hash,
                ),
            ),
        )
        self.assertEqual(
            {item.model for item in compiler.requirements(definition)},
            {"res.country", "res.partner"},
        )
        application_definition = compiler._application_definition(definition)
        self.assertEqual(
            application_definition["source_preparation"]["rules"],
            [],
        )
        controls = RecipeControlValues(
            data_version_id=str(uuid4()),
            values={"control:customers.open_balance": "100.00"},
            actor=LOCAL_ACTOR.identity,
            confirmed_at=datetime.now(timezone.utc),
        )
        datasets = compiler._mapping_datasets(
            application_definition,
            compiler._effective_bindings(
                selection,
                assessment.source_bindings,
            ),
            selection,
            controls,
            ReferenceBundle(
                workspace_id=workspace_id,
                datasets=(reference,),
            ),
        )
        name = next(
            item
            for item in datasets[0].fields
            if item.target_field == "name"
        )
        self.assertEqual(name.source_column_key, "current:name")
        self.assertTrue(name.transform.trim)
        self.assertTrue(name.transform.collapse_whitespace)
        self.assertEqual(
            {item.code for item in compiler._quality_issues(definition)},
            {"RECIPE_QUALITY_SCOPE_REVIEW_REQUIRED"},
        )

        class MappingState:
            draft = None

            def get_mapping_revision(self, workspace_id):
                del workspace_id
                return None

            def get_mapping_working_draft(self, workspace_id):
                del workspace_id
                return self.draft

            def save_mapping_working_draft(
                self,
                workspace_id,
                draft,
                *,
                expected_version,
                actor,
            ):
                del workspace_id, expected_version, actor
                self.draft = draft

        governance_state = {"value": None}

        def govern(current_workspace_id, *, business_keys, actor):
            governance = SchemaGovernance(
                governance_id=str(uuid4()),
                version=1,
                workspace_id=current_workspace_id,
                catalog_hash=schema.content_hash,
                permitted_models=tuple(item.name for item in schema.models),
                business_keys=business_keys,
                recorded_at=datetime.now(timezone.utc),
                recorded_by=actor.identity.display_name,
            )
            governance_state["value"] = governance
            return governance

        sources = SimpleNamespace(
            get_source_selection=lambda current_workspace_id: selection,
            get_mapping_source_selection=lambda current_workspace_id: selection,
        )
        schemas = SimpleNamespace(
            get_odoo_schema_catalog=lambda current_workspace_id: schema,
            get_schema_governance=lambda current_workspace_id: governance_state[
                "value"
            ],
        )
        mapping_state = MappingState()
        mapping_workspace = MappingWorkspaceService(
            sources,
            schemas,
            mapping_state,
            workspace_access_service(),
            categorical_coverage=SimpleNamespace(),
        )
        quality_seed = {}
        materializing_compiler = RecipeApplicationService(
            sources=sources,
            schemas=schemas,
            schema_workspace=SimpleNamespace(govern=govern),
            references=SimpleNamespace(
                get_reference_bundle=lambda current_workspace_id: ReferenceBundle(
                    workspace_id=current_workspace_id,
                    datasets=(reference,),
                )
            ),
            preparation=SimpleNamespace(),
            mappings=mapping_workspace,
            categorical=SimpleNamespace(
                collect=lambda *args: SimpleNamespace(issues=())
            ),
            application_state=SimpleNamespace(
                save_quality_seed=lambda *args, **kwargs: quality_seed.update(
                    kwargs
                )
            ),
        )
        materialized = materializing_compiler.materialize(
            workspace_id,
            application_id=str(uuid4()),
            recipe_id=str(uuid4()),
            data_version_id=controls.data_version_id,
            definition=definition,
            assessment=assessment,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(materialized.status, RecipeApplicationStatus.BLOCKED)
        self.assertIsNotNone(materialized.mapping_id)
        self.assertIsNotNone(materialized.mapping_content_hash)
        self.assertIn(
            "RECIPE_QUALITY_SCOPE_REVIEW_REQUIRED",
            {item.code for item in materialized.issues},
        )
        self.assertIsNotNone(mapping_state.draft)


class IntegratedRecipeCompiler:
    """Keep the test focused on orchestration and persistence."""

    def __init__(self) -> None:
        self.logical_name = "Customers"
        self.model = "res.partner"
        self.fields = ("name",)
        self.reference_requirement = None

    def compile_workspace(self, workspace_id):
        del workspace_id
        definition = self._definition(
            logical_name=self.logical_name,
            model=self.model,
            fields=self.fields,
        )
        return (
            CompiledRecipeDefinition(
                recipe=definition,
                compatibility_hints={
                    "datasets": [
                        {
                            "logical_dataset_id": (
                                f"dataset:{self.logical_name.casefold()}"
                            ),
                            "prior_display_name": self.logical_name,
                        }
                    ]
                },
                source_selection_hash=content_hash(self.logical_name),
                mapping_id=str(uuid4()),
                mapping_version=1,
                mapping_content_hash=content_hash(definition),
                schema_hash=content_hash("odoo-19"),
                quality_ruleset_hash=content_hash("quality"),
            ),
            (),
        )

    @staticmethod
    def _definition(*, logical_name, model, fields):
        token = logical_name.casefold()
        return {
            "contract_versions": {
                "mapping_recipe": 2,
                "odoo_target_contract": 2,
            },
            "source_shape": {
                "datasets": [
                    {
                        "logical_dataset_id": f"dataset:{token}",
                        "logical_name": logical_name,
                        "required": True,
                        "columns": [
                            {
                                "logical_column_id": f"column:{token}.name",
                                "source_name": "Name",
                                "candidate_type": "STRING",
                                "required_by": ["mapping"],
                            }
                        ],
                    }
                ]
            },
            "parameter_definitions": {"parameters": []},
            "source_preparation": {"rules": []},
            "mapping": {
                "datasets": [
                    {
                        "logical_dataset_id": f"dataset:{token}",
                        "target_model": model,
                        "mode": "UPSERT",
                        "fields": [],
                        "relationships": [],
                        "identity": [],
                        "approved_write_fields": list(fields),
                    }
                ]
            },
            "odoo_target_contract": {
                "odoo_major_version": 19,
                "reference_policy_hash": content_hash("reference-policy"),
                "required_applications": [],
                "models": [
                    {
                        "model": model,
                        "fields": [
                            {
                                "name": field,
                                "field_type": "char",
                                "required": False,
                                "readonly": False,
                                "write_use": True,
                            }
                            for field in fields
                        ],
                    }
                ],
                "business_keys": [],
                "approved_write_fields": {model: list(fields)},
            },
            "target_governance": {},
            "quality": {"rules": []},
            "reference_dependencies": {"references": []},
            "control_definitions": {"controls": []},
        }

    @staticmethod
    def requirements(definition):
        result = []
        for model in definition["odoo_target_contract"]["models"]:
            result.append(
                OdooModelRequirement(
                    model=model["model"],
                    fields=tuple(field["name"] for field in model["fields"]),
                )
            )
        return tuple(result)

    @staticmethod
    def write_claims(definition):
        return tuple(
            sorted(
                (model, field)
                for model, fields in definition["odoo_target_contract"][
                    "approved_write_fields"
                ].items()
                for field in fields
            )
        )

    def reference_requirements(self, definition):
        return (
            (self.reference_requirement,)
            if self.reference_requirement is not None
            and "res.partner"
            in definition["odoo_target_contract"]["approved_write_fields"]
            else ()
        )

    @staticmethod
    def assess(
        *,
        recipe_id,
        definition,
        source_selection,
        target_schema,
        reference_bundle,
        parameter_values,
        control_values,
    ):
        del target_schema, reference_bundle, parameter_values, control_values
        shape = definition["source_shape"]["datasets"][0]
        dataset = next(
            (item for item in source_selection.datasets if item.name == shape["logical_name"]),
            None,
        )
        issues = ()
        dataset_ids = ()
        bindings = {}
        if dataset is None:
            issues = (
                MigrationRunPlanIssue(
                    code="RECIPE_SOURCE_DATASET_MISSING",
                    level=MigrationRunPlanIssueLevel.BLOCKER,
                    message=f"{shape['logical_name']} is missing.",
                    recovery_action="Accept a compatible Test DataVersion.",
                    recipe_ids=(recipe_id,),
                ),
            )
        else:
            dataset_ids = (dataset.dataset_id,)
            bindings = {shape["logical_dataset_id"]: dataset.dataset_id}
        binding_hash = content_hash(bindings)
        return RecipeApplicationAssessment(
            dataset_ids=dataset_ids,
            source_bindings=bindings,
            parameter_values={},
            control_values={},
            physical_binding_hash=binding_hash,
            parameter_values_hash=content_hash({}),
            issues=issues,
        )

    @staticmethod
    def materialize(
        workspace_id,
        *,
        application_id,
        recipe_id,
        data_version_id,
        definition,
        assessment,
        actor,
    ):
        del recipe_id, data_version_id, definition, actor
        if assessment.blocked:
            status = RecipeApplicationStatus.BLOCKED
            mapping_id = None
            mapping_hash = None
        else:
            status = RecipeApplicationStatus.READY
            mapping_id = str(uuid5(UUID(workspace_id), "integrated-run-mapping"))
            mapping_hash = content_hash(
                {"application_id": application_id, "workspace_id": workspace_id}
            )
        return RecipeMaterialization(
            status=status,
            mapping_id=mapping_id,
            mapping_content_hash=mapping_hash,
            issues=assessment.issues,
            evidence_hash=content_hash(
                {
                    "application_id": application_id,
                    "mapping_content_hash": mapping_hash,
                    "status": status.value,
                }
            ),
        )


class IntegratedRecipeRunTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"integrated-multi-recipe-{uuid4()}"
        self.root.mkdir()
        self.authorization = CapabilityAuthorizationPolicy()
        self.database = MigrationFoundationDatabase(self.root)
        self.foundation = MigrationFoundationRepository(self.database)
        self.projects = MigrationProjectService(self.foundation, self.authorization)
        self.data_versions = DataVersionService(self.foundation, self.authorization)
        self.runs = MigrationRunService(self.foundation, self.authorization)
        self.workspaces = MigrationWorkspaceService(
            self.foundation,
            self.authorization,
        )
        self.packages = DataVersionSourcePackageService(
            self.foundation,
            self.authorization,
        )
        engine_database = MigrationWorkspaceEngineDatabase(self.database)
        workspace_repository = MigrationWorkspaceStateRepository(
            engine_database,
            self.foundation,
        )
        self.workspace_states = WorkspaceStateService(
            workspace_repository,
            self.authorization,
        )
        self.authoring = MigrationProjectAuthoringService(
            self.projects,
            self.data_versions,
            self.runs,
            self.workspaces,
            self.packages,
            self.workspace_states,
        )
        self.bundle = self.authoring.create(
            actor=LOCAL_ACTOR,
            display_name="Integrated master data",
            migration_purpose="Rehearse Customers and Products together",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )
        self._replace_and_freeze(self.bundle.data_version, expected_package_revision=1)
        self.secret_store = MemorySecretStore()
        protected = ProtectedRecipeStore(self.root, self.secret_store)
        recipe_repository = RecipeRepository(self.foundation, protected)
        self.recipe_service = RecipeService(
            recipe_repository,
            self.authorization,
        )
        self.compiler = IntegratedRecipeCompiler()
        publication = RecipePublicationService(
            recipe_repository,
            self.compiler,
            self.authorization,
        )
        self.customer = publication.publish(
            project_id=self.bundle.project.project_id,
            data_version_id=self.bundle.data_version.data_version_id,
            workspace_id=self.bundle.workspace.workspace_id,
            display_name="Customers",
            business_purpose="Prepare customer companies",
            actor=LOCAL_ACTOR,
        )
        self.compiler.logical_name = "Products"
        self.compiler.model = "product.template"
        self.compiler.fields = ("default_code", "name")
        self.product = publication.publish(
            project_id=self.bundle.project.project_id,
            data_version_id=self.bundle.data_version.data_version_id,
            workspace_id=self.bundle.workspace.workspace_id,
            display_name="Product and BOM",
            business_purpose="Prepare products and their structures",
            actor=LOCAL_ACTOR,
        )
        project = self.projects.get(self.bundle.project.project_id, actor=LOCAL_ACTOR)
        self.test_data_version = self.data_versions.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            purpose=DataVersionPurpose.TEST,
            label="Integrated Test export",
        )
        self._replace_and_freeze(self.test_data_version)
        self.planning_repository = MigrationRunPlanningRepository(self.foundation)
        self.cutover_repository = CutoverPlanRepository(
            self.foundation,
            ProtectedProjectEvidenceStore(self.root, self.secret_store),
        )
        self.planning = MigrationRunPlanningService(
            projects=self.projects,
            data_versions=self.data_versions,
            recipes=self.recipe_service,
            repository=self.planning_repository,
            source_packages=self.packages,
            source_projections=WorkspaceSourceProjectionService(
                self.foundation,
                self.authorization,
            ),
            workspace_states=self.workspace_states,
            compiler=self.compiler,
            cutover_plans=self.cutover_repository,
            authorization=self.authorization,
        )
        self.schema = self._schema()
        self.plan_workspace_revision = self.projects.get(
            self.bundle.project.project_id,
            actor=LOCAL_ACTOR,
        ).optimistic_revision

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _replace_and_freeze(self, data_version, *, expected_package_revision=None):
        revision = 1 if expected_package_revision is None else expected_package_revision + 1
        self.packages.replace_draft(
            self._package(data_version, revision=revision),
            actor=LOCAL_ACTOR,
            expected_package_revision=expected_package_revision,
        )
        return self.packages.freeze(
            data_version.data_version_id,
            actor=LOCAL_ACTOR,
            expected_data_version_revision=data_version.optimistic_revision,
            expected_package_revision=revision,
            operation_id=str(uuid4()),
        )

    def _package(self, data_version, *, revision):
        now = utc_now()
        file_id = str(uuid4())
        file_hash = content_hash({"data_version": data_version.data_version_id})
        catalog = SourcePackageCatalog(
            file_id=file_id,
            source_sha256=file_hash,
            payload={"format": "CSV", "tables": ["Customers", "Products"]},
        )
        columns = (
            SourceDatasetColumn(1, "Legacy ID", "column:legacy_id", "INTEGER"),
            SourceDatasetColumn(2, "Name", "column:name", "STRING"),
        )
        source_file = SourcePackageFile(
            file_id=file_id,
            display_name="integrated-test.csv",
            storage_key=f"source/{file_hash}/integrated-test.csv",
            size_bytes=2048,
            sha256=file_hash,
            received_at=now,
        )
        configuration = SourcePackageConfiguration(
            file_id=file_id,
            catalog_hash=catalog.content_hash,
            payload={
                "encoding": "utf-8",
                "selected_tables": ["Customers", "Products"],
            },
        )
        datasets = tuple(
            SourcePackageDataset(
                dataset_id=name.casefold(),
                display_name=name,
                source_file_ids=(file_id,),
                source=FileSourceBinding(
                    file_id=file_id,
                    table_key=name,
                    source_sha256=file_hash,
                    catalog_hash=catalog.content_hash,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                ),
                row_count=20,
                columns=columns,
                schema_hash=source_column_contract_hash(columns),
                snapshot_hash=content_hash({"snapshot": name, "version": data_version.data_version_id}),
                snapshot_storage_key=f"snapshots/{name.casefold()}.parquet",
                manifest={"logical_name": name.casefold()},
            )
            for name in ("Customers", "Products")
        )
        return DataVersionSourcePackage(
            data_version_id=data_version.data_version_id,
            project_id=data_version.project_id,
            revision=revision,
            origin=SourcePackageOrigin.FILE,
            state=SourcePackageState.DRAFT,
            files=(source_file,),
            catalogs=(catalog,),
            configurations=(configuration,),
            datasets=datasets,
            updated_at=now,
        )

    def _schema(self):
        field = lambda name: SchemaField(
            name=name,
            label=name.replace("_", " ").title(),
            type="char",
            required=False,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
        )
        return OdooSchemaCatalog(
            workspace_id=self.bundle.workspace.workspace_id,
            policy_hash=content_hash("schema-policy"),
            captured_at=utc_now(),
            captured_by="Test operator",
            connection_mode="REMOTE",
            database="integrated_test",
            odoo_version="19.0",
            models=(
                SchemaModel("res.partner", "Contacts", (field("name"),)),
                SchemaModel(
                    "product.template",
                    "Products",
                    (field("default_code"), field("name")),
                ),
            ),
            content_hash=content_hash("integrated-test-schema"),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=content_hash("credential-generation"),
            read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"),
            read_context_hash=content_hash("context"),
            connection_target_hash=content_hash("target"),
        )

    def _selected(self):
        return (
            (self.customer.recipe.recipe_id, self.customer.revision.version),
            (self.product.recipe.recipe_id, self.product.revision.version),
        )

    def _start(self, *, operation_id=None, fault=None, reference_bundle=None):
        return self.planning.start_test_run(
            self.bundle.project.project_id,
            expected_workspace_revision=self.plan_workspace_revision,
            data_version_id=self.test_data_version.data_version_id,
            recipe_revisions=self._selected(),
            dependencies=(
                RecipeDependency(
                    before_recipe_id=self.customer.recipe.recipe_id,
                    after_recipe_id=self.product.recipe.recipe_id,
                ),
            ),
            target_schema=self.schema,
            target_reference_bundle=reference_bundle,
            credential_generation=self.schema.read_credential_binding_hash,
            label="Integrated Test run",
            operation_id=operation_id or str(uuid4()),
            actor=LOCAL_ACTOR,
            fault=fault,
        )

    def test_guided_setup_pins_recipes_and_creates_fresh_test_evidence(self):
        service = TestRunSetupService(
            projects=self.projects,
            data_versions=self.data_versions,
            runs=self.runs,
            migration_workspaces=self.workspaces,
            source_packages=self.packages,
            workspace_states=self.workspace_states,
            recipes=self.recipe_service,
            test_runs=TestRunRepository(self.foundation),
            run_planning=self.planning,
            authorization=self.authorization,
        )
        project = self.projects.get(
            self.bundle.project.project_id,
            actor=LOCAL_ACTOR,
        )

        setup = service.start_setup(
            project.project_id,
            expected_workspace_revision=project.optimistic_revision,
            recipe_revisions=(
                (self.customer.recipe.recipe_id, 1),
                (self.product.recipe.recipe_id, 1),
            ),
            dependencies=(
                RecipeDependency(
                    before_recipe_id=self.customer.recipe.recipe_id,
                    after_recipe_id=self.product.recipe.recipe_id,
                ),
            ),
            label="August pre-production rehearsal",
            export_as_of="2026-08-24 18:00 Bangkok time",
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(setup.data_version.purpose, DataVersionPurpose.TEST)
        self.assertEqual(setup.data_version.state.value, "DRAFT")
        self.assertEqual(setup.run.migration_run_id, setup.binding.migration_run_id)
        self.assertEqual(
            setup.setup_workspace.workspace_id,
            setup.binding.setup_workspace_id,
        )
        self.assertEqual(
            service.required_models_for_workspace(
                setup.setup_workspace.workspace_id,
                actor=LOCAL_ACTOR,
            ),
            ("product.template", "res.partner"),
        )
        self.assertEqual(
            self.packages.repository.get_source_package(
                setup.data_version.data_version_id
            ).state,
            SourcePackageState.DRAFT,
        )
        self._replace_and_freeze(
            setup.data_version,
            expected_package_revision=1,
        )
        setup_state = self.workspace_states.repository.get(
            setup.setup_workspace.workspace_id
        )
        self.workspace_states.update_target(
            setup.setup_workspace.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=setup_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://preprod.example.test",
            odoo_database="preprod",
            intended_applications=(),
            intended_models=("product.template", "res.partner"),
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        activated = service.activate(
            project.project_id,
            setup.run.migration_run_id,
            expected_workspace_revision=project.optimistic_revision,
            target_schema=replace(
                self.schema,
                workspace_id=setup.setup_workspace.workspace_id,
            ),
            target_reference_bundle=None,
            credential_generation=self.schema.read_credential_binding_hash,
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(activated.run.migration_run_id, setup.run.migration_run_id)
        self.assertEqual(len(activated.applications), 2)
        self.assertEqual(len(activated.workspaces), 3)
        self.assertEqual(
            service.get(setup.run.migration_run_id, actor=LOCAL_ACTOR).state.value,
            "ACTIVE",
        )
        for application in activated.applications:
            self.assertEqual(
                service.credential_workspace(
                    application.workspace_id,
                    actor=LOCAL_ACTOR,
                ).workspace_id,
                setup.setup_workspace.workspace_id,
            )

    def test_two_recipes_share_one_run_target_and_keep_isolated_workspaces(self):
        result = self._start()

        self.assertEqual(result.run.purpose.value, "TEST")
        self.assertEqual(len(result.applications), 2)
        self.assertEqual(len(result.workspaces), 2)
        self.assertEqual(
            {item.target_binding_id for item in result.applications},
            {result.target_binding.target_binding_id},
        )
        self.assertEqual(
            [
                item.recipe_id
                for item in result.applications
                if item.status is RecipeApplicationStatus.READY
            ],
            [item.recipe_id for item in result.applications],
        )
        selected_by_recipe = {
            item.recipe_id: self.planning_repository.application_dataset_ids(
                item.application_id
            )
            for item in result.applications
        }
        self.assertEqual(
            selected_by_recipe[self.customer.recipe.recipe_id],
            ("customers",),
        )
        self.assertEqual(
            selected_by_recipe[self.product.recipe.recipe_id],
            ("products",),
        )
        self.assertEqual(
            len({item.workspace_id for item in result.applications}),
            2,
        )
        run_schema = self.planning_repository.get_run_target_schema(
            result.run.migration_run_id
        )
        self.assertEqual(run_schema.migration_run_id, result.run.migration_run_id)
        self.assertEqual(
            run_schema.source_schema.workspace_id,
            self.bundle.workspace.workspace_id,
        )
        with self.database.connect(self.foundation.registry_path) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM target_binding").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_run_target_schema"
                ).fetchone(),
                (1,),
            )

    def test_run_captures_references_once_and_projects_only_the_recipe_need(self):
        reference = ReferenceDataSet(
            reference_id=str(uuid4()),
            version=1,
            name="Customer type",
            key_fields=("source_value",),
            value_kinds={
                "target_value": ReferenceValueKind.ODOO_SELECTION_KEY,
            },
            entries=(
                ReferenceEntry(
                    key=("Company",),
                    values={"target_value": "company"},
                ),
            ),
            owner="Data manager",
            classification="INTERNAL",
            effective_label="Integrated Test",
        )
        bundle = ReferenceBundle(
            workspace_id=self.bundle.workspace.workspace_id,
            datasets=(reference,),
        )
        self.compiler.reference_requirement = ReferenceRequirement(
            name=reference.name,
            content_hash=reference.content_hash,
        )
        result = self._start(reference_bundle=bundle)
        customer_application = next(
            item
            for item in result.applications
            if item.recipe_id == self.customer.recipe.recipe_id
        )
        product_application = next(
            item
            for item in result.applications
            if item.recipe_id == self.product.recipe.recipe_id
        )

        stored = self.planning_repository.get_run_reference_bundle(
            result.run.migration_run_id
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.migration_run_id, result.run.migration_run_id)
        self.assertEqual(
            stored.source_workspace_id,
            self.bundle.workspace.workspace_id,
        )
        self.assertEqual(
            result.target_binding.reference_snapshot_hashes,
            (reference.content_hash,),
        )
        customer_projection = (
            self.planning_repository.get_workspace_reference_bundle(
                customer_application.workspace_id
            )
        )
        product_projection = (
            self.planning_repository.get_workspace_reference_bundle(
                product_application.workspace_id
            )
        )
        self.assertEqual(customer_projection.datasets, (reference,))
        self.assertEqual(product_projection.datasets, ())
        with self.database.connect(self.foundation.registry_path) as connection:
            connection.execute(
                "UPDATE recipe_application_reference_requirement "
                "SET content_hash = ? WHERE application_id = ?",
                [
                    content_hash("tampered requirement"),
                    customer_application.application_id,
                ],
            )
        with self.assertRaises(MigrationConflictError):
            self.planning_repository.get_workspace_reference_bundle(
                customer_application.workspace_id
            )

    def test_write_collision_blocks_before_run_or_workspace_creation(self):
        envelope = self.recipe_service.read_revision(
            self.product.recipe.recipe_id,
            self.product.revision.version,
            actor=LOCAL_ACTOR,
        )
        envelope["recipe"]["odoo_target_contract"]["approved_write_fields"] = {
            "res.partner": ["name"]
        }
        original = self.recipe_service.read_revision

        def read(recipe_id, version, *, actor):
            if recipe_id == self.product.recipe.recipe_id:
                return envelope
            return original(recipe_id, version, actor=actor)

        self.recipe_service.read_revision = read
        review = self.planning.review_test_run(
            self.bundle.project.project_id,
            data_version_id=self.test_data_version.data_version_id,
            recipe_revisions=self._selected(),
            dependencies=(),
            target_schema=self.schema,
            target_reference_bundle=None,
            parameter_values={},
            control_values={},
            actor=LOCAL_ACTOR,
        )
        self.assertFalse(review.can_start)
        self.assertIn(
            "RUN_RECIPE_WRITE_COLLISION",
            {item.code for item in review.planning_issues},
        )
        with self.assertRaises(MigrationRunPlanningError):
            self._start()
        self.assertEqual(
            [
                item
                for item in self.runs.list(
                    self.bundle.project.project_id,
                    actor=LOCAL_ACTOR,
                )
                if item.purpose.value == "TEST"
            ],
            [],
        )

    def test_dependency_cycle_names_both_recipes_and_creates_nothing(self):
        dependencies = (
            RecipeDependency(
                before_recipe_id=self.customer.recipe.recipe_id,
                after_recipe_id=self.product.recipe.recipe_id,
            ),
            RecipeDependency(
                before_recipe_id=self.product.recipe.recipe_id,
                after_recipe_id=self.customer.recipe.recipe_id,
            ),
        )
        review = self.planning.review_test_run(
            self.bundle.project.project_id,
            data_version_id=self.test_data_version.data_version_id,
            recipe_revisions=self._selected(),
            dependencies=dependencies,
            target_schema=self.schema,
            target_reference_bundle=None,
            parameter_values={},
            control_values={},
            actor=LOCAL_ACTOR,
        )
        issue = next(
            item
            for item in review.planning_issues
            if item.code == "RUN_RECIPE_DEPENDENCY_CYCLE"
        )
        self.assertEqual(set(issue.recipe_ids), {item[0] for item in self._selected()})
        self.assertFalse(review.can_start)

    def test_same_operation_recovers_after_registry_fault_without_duplicates(self):
        operation_id = str(uuid4())

        def fault(stage):
            if stage == "REGISTRY_COMMITTED":
                raise SimulatedCrash(stage)

        with self.assertRaises(SimulatedCrash):
            self._start(operation_id=operation_id, fault=fault)
        self.assertEqual(
            self.foundation.get_operation_intent(operation_id).state,
            MigrationOperationState.PENDING,
        )
        recovered = self._start(operation_id=operation_id)
        replayed = self._start(operation_id=operation_id)
        self.assertEqual(
            recovered.run.migration_run_id,
            replayed.run.migration_run_id,
        )
        self.assertEqual(len(recovered.applications), 2)
        with self.database.connect(self.foundation.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM recipe_application"
                ).fetchone(),
                (2,),
            )

    def test_integrated_progress_reads_registry_without_workspace_open(self):
        result = self._start()
        opened = []
        original = self.database.connect

        def counted(path):
            opened.append(path)
            return original(path)

        self.database.connect = counted
        progress = self.planning_repository.progress(result.run.migration_run_id)
        self.assertEqual(progress.total_applications, 2)
        self.assertTrue(opened)
        self.assertTrue(all(path == self.foundation.registry_path for path in opened))


class IntegratedRecipeRunBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"integrated-browser-{uuid4()}"
        self.root.mkdir()
        self.app = create_local_app(
            self.root,
            launch_token="integrated-launch",
            session_secret="integrated-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        self.client = TestClient(self.app)
        launched = self.client.get(
            "/launch?token=integrated-launch",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)

    def tearDown(self) -> None:
        self.client.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_project_exposes_integrated_test_planning_without_recipe_root(self):
        form = self.client.get("/projects/new")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
        request_id = re.search(
            r'name="creation_request_id" value="([^"]+)"',
            form.text,
        ).group(1)
        created = self.client.post(
            "/projects/new",
            data={
                "csrf_token": csrf,
                "creation_request_id": request_id,
                "display_name": "Integrated browser Project",
                "migration_purpose": "Test several Recipes together",
                "source_mode": "FILE",
                "source_system_identity": "Fictional ERP",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)
        project_id = created.headers["location"].rsplit("/", 1)[-1]

        planning = self.client.get(f"/projects/{project_id}/test-runs/new")

        self.assertEqual(planning.status_code, 200)
        self.assertIn("Test with new data", planning.text)
        self.assertIn("Save at least one Recipe first", planning.text)
        self.assertEqual(self.client.get("/recipes").status_code, 404)

        context = self.app.state.context
        workspace = context.migration_workspaces.list_for_project(
            project_id,
            actor=context.actor,
        )[0]
        data_version = context.data_versions.get(
            workspace.data_version_id,
            actor=context.actor,
        )
        context.data_versions.repository.save_data_version(
            replace(
                data_version,
                state=DataVersionState.FROZEN,
                source_package_hash=content_hash({"delivery": "authoring"}),
                updated_at=utc_now(),
                frozen_at=utc_now(),
            ),
            expected_revision=data_version.optimistic_revision,
            event_type="TEST_DATA_VERSION_FROZEN",
            actor=context.actor,
        )
        publication = context.recipes.repository.publish_recipe(
            project_id=project_id,
            data_version_id=data_version.data_version_id,
            workspace_id=workspace.workspace_id,
            recipe_id=None,
            expected_recipe_revision=None,
            display_name="Customers",
            business_purpose="Prepare customers from the newer delivery",
            compiled_recipe={"contract_versions": {}},
            compatibility_hints={},
            compilation_provenance={},
            operation_id=str(uuid4()),
            request_hash=content_hash({"request": "browser Recipe"}),
            actor=context.actor,
        )

        recipe_planning = self.client.get(f"/projects/{project_id}/test-runs/new")

        self.assertEqual(recipe_planning.status_code, 200)
        self.assertIn("Customers", recipe_planning.text)
        self.assertIn("Create Test setup", recipe_planning.text)
        self.assertNotIn("Reviewed Odoo evidence is required", recipe_planning.text)

        csrf = re.search(
            r'name="csrf_token" value="([^"]+)"', recipe_planning.text
        ).group(1)
        operation_id = re.search(
            r'name="operation_id" value="([^"]+)"', recipe_planning.text
        ).group(1)
        expected_revision = re.search(
            r'name="expected_workspace_revision" value="([^"]+)"',
            recipe_planning.text,
        ).group(1)
        setup = self.client.post(
            f"/projects/{project_id}/test-runs/new",
            data={
                "csrf_token": csrf,
                "operation_id": operation_id,
                "expected_workspace_revision": expected_revision,
                "label": "Fresh customer Test",
                "export_as_of": "2026-08-24 20:00 Bangkok time",
                "recipe_revision": (
                    f"{publication.recipe.recipe_id}:"
                    f"{publication.revision.version}"
                ),
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

        self.assertEqual(setup.status_code, 303)
        self.assertRegex(setup.headers["location"], r"^/workspaces/.+/files$")


if __name__ == "__main__":
    unittest.main()


