"""Verify multi-Recipe Test planning and isolated application workspaces."""

from __future__ import annotations

import json
import re
import shutil
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient

from impodo.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.adapters.duckdb.cutover_plan_repository import CutoverPlanRepository
from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
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
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.adapters.protected_recipe_store import ProtectedRecipeStore
from impodo.application.mapping_workspace_service import MappingWorkspaceService
from impodo.application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from impodo.application.migration_run_planning_service import (
    MigrationRunPlanningService,
)
from impodo.application.recipe_application_service import (
    RecipeApplicationAssessment,
    RecipeApplicationService,
    RecipeMaterialization,
)
from impodo.application.recipe_compilation_service import CompiledRecipeDefinition
from impodo.application.recipe_publication_service import (
    RecipePublicationService,
)
from impodo.application.test_run_setup_service import (
    FreshDataInputRequirement,
    FreshDataMatchStatus,
    FreshDataParameterRequirement,
    FreshDataRecipeRequirement,
    TestRunSetupService,
)
from impodo.connectors import MetadataSnapshot
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
from impodo.domain.coverage import (
    ReferenceBundle,
    ReferenceDataSet,
    ReferenceEntry,
    ReferenceValueKind,
)
from impodo.domain.recipe_applications import RecipeControlValues
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import FileSourceBinding
from impodo.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.migration_foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationOperationState,
    utc_now,
)
from impodo.migration_projects import MigrationProjectService
from impodo.migration_run_planning import (
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    MigrationRunPlanningError,
    OdooModelRequirement,
    RecipeApplicationStatus,
    RecipeDependency,
    ReferenceRequirement,
)
from impodo.migration_runs import MigrationRunService
from impodo.migration_workspaces import MigrationWorkspaceService
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    target_identity_hash,
)
from impodo.recipes import RecipeService
from impodo.secrets import MemorySecretStore
from impodo.web.app import create_local_app
from impodo.web.run_review import build_integrated_run_review
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.workspace_state import WorkspaceStateService
from tests.workspace_access_helpers import workspace_access_service

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
                    name="customers",
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
        self.assertEqual(datasets[0].approved_write_fields, ())
        self.assertEqual(
            {item.code for item in compiler._quality_issues(definition)},
            {"RECIPE_QUALITY_SCOPE_REVIEW_REQUIRED"},
        )

        class MappingState:
            draft = None
            revision = None
            validation = None
            submission = None

            def get_mapping_revision(self, workspace_id):
                del workspace_id
                return self.revision

            def list_mapping_revisions(self, workspace_id):
                del workspace_id
                return (self.revision,) if self.revision is not None else ()

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

            def save_mapping_revision(
                self,
                workspace_id,
                revision,
                *,
                validation,
                expected_parent_version,
                expected_working_draft_version,
                checked_draft,
                actor,
            ):
                del (
                    workspace_id,
                    expected_parent_version,
                    expected_working_draft_version,
                    actor,
                )
                self.revision = revision
                self.validation = validation
                self.draft = checked_draft

            def save_mapping_validation(
                self,
                workspace_id,
                version,
                validation,
                *,
                actor,
            ):
                del workspace_id, version, actor
                self.validation = validation

            def get_mapping_validation(self, workspace_id, version):
                del workspace_id, version
                return self.validation

            def get_mapping_submission(self, workspace_id, version):
                del workspace_id, version
                return self.submission

            def save_mapping_submission(
                self,
                workspace_id,
                submission,
                *,
                actor,
            ):
                del workspace_id, actor
                self.submission = submission

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
            categorical_coverage=SimpleNamespace(
                collect=lambda *args: SimpleNamespace(
                    issues=(),
                    evidence=SimpleNamespace(to_dict=lambda: {}),
                )
            ),
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

        definition["quality"]["rules"] = []
        definition["mapping"]["datasets"][0]["relationships"] = []
        definition["mapping"]["datasets"][0]["source_identity_column_ids"] = [
            "column:customers.customer_code"
        ]
        ready = materializing_compiler.materialize(
            workspace_id,
            application_id=str(uuid4()),
            recipe_id=str(uuid4()),
            data_version_id=controls.data_version_id,
            definition=definition,
            assessment=assessment,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(ready.status, RecipeApplicationStatus.READY, ready.issues)
        self.assertIsNotNone(mapping_state.revision)
        self.assertIsNotNone(mapping_state.submission)


class FreshDataRecipeMatchingTests(unittest.TestCase):
    """Keep fresh matching automatic, explainable, and fail closed."""

    @staticmethod
    def _requirement(*inputs: FreshDataInputRequirement):
        return (
            FreshDataRecipeRequirement(
                recipe_id="recipe-1",
                recipe_revision=3,
                display_name="Reusable Recipe",
                business_purpose="Load the current business records",
                inputs=tuple(inputs),
                parameters=(),
            ),
        )

    @staticmethod
    def _catalog(
        display_name: str,
        *tables: tuple[str, tuple[str, ...]],
        formulas: bool = False,
    ) -> SourceFileCatalog:
        file_id = f"file-{display_name}"
        return SourceFileCatalog(
            contract_version=CATALOG_CONTRACT_VERSION,
            file_id=file_id,
            display_name=display_name,
            source_sha256="sha256:" + "a" * 64,
            source_size_bytes=100,
            format="CSV" if display_name.endswith(".csv") else "XLSX",
            inspected_at=datetime.now(UTC),
            encoding="utf-8" if display_name.endswith(".csv") else None,
            delimiter="," if display_name.endswith(".csv") else None,
            tables=tuple(
                SourceTableCatalog(
                    table_key=(
                        "csv"
                        if display_name.endswith(".csv")
                        else f"sheet:{name}"
                    ),
                    name=name,
                    kind="CSV" if display_name.endswith(".csv") else "WORKSHEET",
                    hidden=False,
                    header_row=1,
                    row_count=8,
                    column_count=len(columns),
                    columns=tuple(
                        SourceColumnProfile(
                            ordinal=index,
                            name=column,
                            candidate_type="TEXT",
                            null_count=0,
                            non_null_count=8,
                            distinct_count=8,
                            distinct_count_is_exact=True,
                            duplicate_count=0,
                            minimum=None,
                            maximum=None,
                            minimum_length=None,
                            maximum_length=None,
                        )
                        for index, column in enumerate(columns)
                    ),
                    preview_rows=(),
                    formula_cell_count=1 if formulas else 0,
                )
                for name, columns in tables
            ),
        )

    def test_unique_renamed_file_is_matched_from_required_columns(self):
        requirements = self._requirement(
            FreshDataInputRequirement(
                logical_dataset_id="dataset:stock_levels",
                label="Stock levels",
                columns=("Product Code", "On Hand"),
            )
        )
        catalog = self._catalog(
            "warehouse-export-2026.csv",
            ("warehouse-export-2026", ("product-code", "ON_HAND")),
        )

        plan = TestRunSetupService.fresh_data_match_plan(requirements, (catalog,))

        self.assertTrue(plan.ready_to_accept)
        self.assertEqual(plan.inputs[0].status, FreshDataMatchStatus.MATCHED)
        self.assertEqual(plan.inputs[0].dataset_name, "stock_levels")
        self.assertEqual(
            plan.inputs[0].selected_candidate.file_name,
            catalog.display_name,
        )

    def test_two_compatible_tables_ask_for_one_choice(self):
        requirements = self._requirement(
            FreshDataInputRequirement(
                logical_dataset_id="dataset:products",
                label="Products",
                columns=("Product code",),
            )
        )
        catalog = self._catalog(
            "products.xlsx",
            ("Current", ("Product code",)),
            ("Archive", ("Product code",)),
        )

        plan = TestRunSetupService.fresh_data_match_plan(requirements, (catalog,))

        self.assertTrue(plan.can_submit)
        self.assertTrue(plan.needs_choice)
        self.assertEqual(plan.inputs[0].status, FreshDataMatchStatus.AMBIGUOUS)
        selected = plan.inputs[0].candidates[1]
        chosen = TestRunSetupService.fresh_data_match_plan(
            requirements,
            (catalog,),
            overrides={"dataset:products": selected.candidate_id},
        )
        self.assertTrue(chosen.ready_to_accept)
        self.assertEqual(
            chosen.inputs[0].selected_candidate_id,
            selected.candidate_id,
        )

    def test_missing_or_unsafe_table_does_not_match(self):
        requirements = self._requirement(
            FreshDataInputRequirement(
                logical_dataset_id="dataset:stock_levels",
                label="Stock levels",
                columns=("Product code", "On hand"),
            )
        )
        catalog = self._catalog(
            "stock.csv",
            ("stock", ("Product code", "On hand")),
            formulas=True,
        )

        plan = TestRunSetupService.fresh_data_match_plan(requirements, (catalog,))

        self.assertFalse(plan.can_submit)
        self.assertEqual(plan.inputs[0].status, FreshDataMatchStatus.MISSING)
        self.assertEqual(plan.unused_files, ("stock.csv",))

    def test_one_physical_table_cannot_fill_two_recipe_inputs(self):
        requirements = self._requirement(
            FreshDataInputRequirement(
                logical_dataset_id="dataset:customers",
                label="Customers",
                columns=("External ID",),
            ),
            FreshDataInputRequirement(
                logical_dataset_id="dataset:products",
                label="Products",
                columns=("External ID",),
            ),
        )
        catalog = self._catalog(
            "records.csv",
            ("records", ("External ID",)),
        )

        plan = TestRunSetupService.fresh_data_match_plan(requirements, (catalog,))

        self.assertFalse(plan.can_submit)
        self.assertEqual(
            {item.status for item in plan.inputs},
            {FreshDataMatchStatus.CONFLICT},
        )

    def test_worksheet_and_its_excel_table_cannot_both_be_selected(self):
        requirements = self._requirement(
            FreshDataInputRequirement(
                logical_dataset_id="dataset:customers",
                label="Customers",
                columns=("Customer name",),
            ),
            FreshDataInputRequirement(
                logical_dataset_id="dataset:products",
                label="Products",
                columns=("Product code",),
            ),
        )
        catalog = self._catalog(
            "mixed.xlsx",
            ("Sheet1", ("Customer name", "Product code")),
            ("Products", ("Product code",)),
        )
        named_table = replace(
            catalog.tables[1],
            table_key="table:Sheet1:Products",
            kind="NAMED_TABLE",
        )
        catalog = replace(catalog, tables=(catalog.tables[0], named_table))

        plan = TestRunSetupService.fresh_data_match_plan(requirements, (catalog,))

        self.assertFalse(plan.ready_to_accept)
        self.assertEqual(
            {item.status for item in plan.inputs},
            {FreshDataMatchStatus.AMBIGUOUS, FreshDataMatchStatus.CONFLICT},
        )
        self.assertTrue(
            all("same workbook area" in item.explanation for item in plan.inputs)
        )

    def test_shared_run_value_requires_compatible_recipe_meaning(self):
        requirements = tuple(
            FreshDataRecipeRequirement(
                recipe_id=str(uuid4()),
                recipe_revision=1,
                display_name=name,
                business_purpose=f"Prepare {name}",
                inputs=(),
                parameters=(
                    FreshDataParameterRequirement(
                        logical_parameter_id="parameter:stock_date",
                        label="Stock date",
                        value_type=value_type,
                        required=True,
                        constraints={},
                        supplied_value=None,
                    ),
                ),
            )
            for name, value_type in (
                ("Products", "string"),
                ("Stock balances", "date"),
            )
        )

        plan = TestRunSetupService._fresh_data_run_value_plan(
            requirements,
            None,
        )

        self.assertFalse(plan.can_confirm)
        self.assertIn("compatible Recipe versions", plan.values[0].conflict)


class IntegratedRecipeCompiler:
    """Keep the test focused on orchestration and persistence."""

    def __init__(self) -> None:
        self.logical_name = "Customers"
        self.model = "res.partner"
        self.fields = ("name",)
        self.reference_requirement = None
        self.assessed_parameter_values = []

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
            "parameter_definitions": {
                "parameters": [
                    {
                        "logical_parameter_id": "parameter:export_as_of_date",
                        "label": "Data date",
                        "type": "date",
                        "required": True,
                        "constraints": {"not_after_application_date": True},
                    },
                    {
                        "logical_parameter_id": "parameter:batch_reference",
                        "label": "Batch reference",
                        "type": "string",
                        "required": True,
                        "constraints": {"max_length": 20},
                    },
                ]
            },
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

    def assess(
        self,
        *,
        recipe_id,
        definition,
        source_selection,
        target_schema,
        reference_bundle,
        parameter_values,
        control_values,
    ):
        del target_schema, reference_bundle, control_values
        self.assessed_parameter_values.append(dict(parameter_values))
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
            label="August integrated rehearsal",
            export_as_of="2026-08-24",
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
        odoo_plan = service.odoo_check_requirements_for_workspace(
            setup.setup_workspace.workspace_id,
            actor=LOCAL_ACTOR,
        )
        self.assertIsNotNone(odoo_plan)
        self.assertEqual(
            tuple(
                (item.model_name, item.field_names, item.recipe_names)
                for item in odoo_plan.models
            ),
            (
                ("product.template", ("default_code", "name"), ("Product and BOM",)),
                ("res.partner", ("name",), ("Customers",)),
            ),
        )
        self.assertEqual(odoo_plan.supporting_values, ())
        requirements = service.fresh_data_requirements(
            setup.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            tuple(item.display_name for item in requirements),
            ("Customers", "Product and BOM"),
        )
        self.assertEqual(
            tuple(item.inputs[0].label for item in requirements),
            ("Customers", "Products"),
        )
        self.assertTrue(
            all(
                item.parameters[0].supplied_value == "2026-08-24"
                for item in requirements
            )
        )
        run_values = service.fresh_data_run_value_plan(
            setup.binding,
            requirements,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(len(run_values.editable_values), 1)
        self.assertEqual(
            run_values.editable_values[0].recipe_names,
            ("Customers", "Product and BOM"),
        )
        self.assertFalse(run_values.ready_to_continue)
        with self.assertRaisesRegex(
            MigrationFoundationError,
            "20 characters or fewer",
        ):
            service.replace_fresh_data_run_values(
                setup.binding,
                {"parameter:batch_reference": "x" * 21},
                expected_revision=None,
                actor=LOCAL_ACTOR,
            )
        stored_run_values = service.replace_fresh_data_run_values(
            setup.binding,
            {"parameter:batch_reference": "AUGUST-REHEARSAL"},
            expected_revision=None,
            actor=LOCAL_ACTOR,
        )
        self.assertIsNotNone(stored_run_values)
        self.assertEqual(len(stored_run_values.values), 2)
        unchanged_run_values = service.replace_fresh_data_run_values(
            setup.binding,
            {"parameter:batch_reference": "AUGUST-REHEARSAL"},
            expected_revision=stored_run_values.revision,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(unchanged_run_values.revision, 1)
        self.assertTrue(
            service.fresh_data_run_value_plan(
                setup.binding,
                requirements,
                actor=LOCAL_ACTOR,
            ).ready_to_continue
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
            self.compiler.assessed_parameter_values[-2:],
            [
                {
                    "parameter:batch_reference": "AUGUST-REHEARSAL",
                    "parameter:export_as_of_date": "2026-08-24",
                },
                {
                    "parameter:batch_reference": "AUGUST-REHEARSAL",
                    "parameter:export_as_of_date": "2026-08-24",
                },
            ],
        )
        active_binding = service.get(
            setup.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(active_binding.state.value, "ACTIVE")
        with self.assertRaisesRegex(
            MigrationConflictError,
            "accepted with this fresh data",
        ):
            service.replace_fresh_data_run_values(
                active_binding,
                {"parameter:batch_reference": "LATE-CHANGE"},
                expected_revision=stored_run_values.revision,
                actor=LOCAL_ACTOR,
            )
        for application in activated.applications:
            self.assertEqual(
                service.credential_workspace(
                    application.workspace_id,
                    actor=LOCAL_ACTOR,
                ).workspace_id,
                setup.setup_workspace.workspace_id,
            )

    def test_selected_recipe_revisions_use_one_registry_connection(self):
        opened = []
        original_connect = self.database.connect

        def counted(path):
            opened.append(path)
            return original_connect(path)

        with patch.object(self.database, "connect", side_effect=counted):
            revisions = self.recipe_service.read_revisions(
                self.bundle.project.project_id,
                self._selected(),
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(set(revisions), set(self._selected()))
        self.assertEqual(opened, [self.database.registry_path])

    def test_fresh_data_keeps_the_pinned_revision_after_recipe_archive(self):
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
            recipe_revisions=self._selected(),
            dependencies=(),
            label="Pinned Recipe rehearsal",
            export_as_of="2026-08-24",
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        with self.database.connect(self.foundation.registry_path) as connection:
            connection.execute(
                "UPDATE recipe SET archived_at = ? WHERE recipe_id = ?",
                [
                    utc_now().isoformat(),
                    self.customer.recipe.recipe_id,
                ],
            )

        requirements = service.fresh_data_requirements(
            setup.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            {item.display_name for item in requirements},
            {"Customers", "Product and BOM"},
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

    def test_review_projection_orders_recipes_without_workspace_open(self):
        result = self._start()
        opened = []
        original = self.database.connect

        def counted(path):
            opened.append(path)
            return original(path)

        self.database.connect = counted
        recipes = {
            self.customer.recipe.recipe_id: self.customer.recipe,
            self.product.recipe.recipe_id: self.product.recipe,
        }
        view = build_integrated_run_review(
            SimpleNamespace(preparation_jobs=None, load_jobs=None),
            result,
            recipes=recipes,
            issues={item.application_id: () for item in result.applications},
        )

        self.assertEqual(opened, [])
        self.assertEqual(
            tuple(card.recipe_name for card in view.cards),
            ("Customers", "Product and BOM"),
        )
        self.assertEqual(
            tuple(card.state for card in view.cards),
            ("READY_TO_PREPARE", "WAITING"),
        )

    def test_failed_application_remains_the_next_recoverable_recipe(self):
        result = self._start()
        first = next(
            item
            for item in result.applications
            if item.recipe_id == result.requirement_plan.application_order[0]
        )
        running = self.planning_repository.transition_application_status(
            first.application_id,
            expected_statuses=(RecipeApplicationStatus.READY,),
            status=RecipeApplicationStatus.RUNNING,
            actor=LOCAL_ACTOR,
        )
        failed = self.planning_repository.transition_application_status(
            first.application_id,
            expected_statuses=(running.status,),
            status=RecipeApplicationStatus.FAILED,
            actor=LOCAL_ACTOR,
        )

        progress = self.planning_repository.progress(result.run.migration_run_id)

        self.assertEqual(failed.status, RecipeApplicationStatus.FAILED)
        self.assertEqual(progress.next_application_id, first.application_id)


class IntegratedRecipeRunBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        # Keep immutable artifact paths below the Windows portable path limit.
        self.root = ROOT / ".tmp" / f"irb-{uuid4()}"
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
        authoring_page = self.client.get(
            f"/workspaces/{workspace.workspace_id}/files"
        )
        self.assertEqual(authoring_page.status_code, 200)
        for stage_label in (
            "Source data",
            "Odoo data",
            "Match data",
            "Prepare data",
            "Final review",
            "Load into Odoo",
        ):
            self.assertIn(stage_label, authoring_page.text)
        self.assertNotIn("Recipe run", authoring_page.text)
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
            compiled_recipe={
                "contract_versions": {},
                "source_shape": {
                    "datasets": [
                        {
                            "logical_dataset_id": "dataset:customers",
                            "logical_name": "Customers",
                            "required": True,
                            "columns": [
                                {
                                    "logical_column_id": "column:customers.name",
                                    "source_name": "Customer name",
                                    "candidate_type_hint": "STRING",
                                    "required_by": ["mapping"],
                                }
                            ],
                        }
                    ]
                },
                "parameter_definitions": {
                    "parameters": [
                        {
                            "logical_parameter_id": "parameter:export_as_of_date",
                            "label": "Data date",
                            "type": "date",
                            "required": True,
                            "constraints": {"not_after_application_date": True},
                        },
                        {
                            "logical_parameter_id": "parameter:batch_reference",
                            "label": "Batch reference",
                            "type": "string",
                            "required": True,
                            "constraints": {"max_length": 20},
                        },
                    ]
                },
                "odoo_target_contract": {
                    "models": [
                        {
                            "model": "res.partner",
                            "fields": [
                                {"name": "name"},
                                {"name": "email"},
                            ],
                        }
                    ]
                },
                "reference_dependencies": {
                    "references": [
                        {
                            "name": "Countries",
                            "content_hash": content_hash("countries"),
                        }
                    ]
                },
            },
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
        self.assertIn("Odoo target you choose for this Test run", recipe_planning.text)
        self.assertNotIn("pre-production", recipe_planning.text)
        self.assertNotIn("Odoo 19", recipe_planning.text)
        self.assertNotIn("Reviewed Odoo evidence is required", recipe_planning.text)
        today = datetime.now(UTC).astimezone().date().isoformat()
        self.assertIn(
            f'<input type="date" name="export_as_of" value="{today}" required>',
            recipe_planning.text,
        )

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
        invalid_setup = self.client.post(
            f"/projects/{project_id}/test-runs/new",
            data={
                "csrf_token": csrf,
                "operation_id": operation_id,
                "expected_workspace_revision": expected_revision,
                "label": "Fresh customer Test",
                "export_as_of": "206-08-24",
                "recipe_revision": (
                    f"{publication.recipe.recipe_id}:"
                    f"{publication.revision.version}"
                ),
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(invalid_setup.status_code, 422)
        self.assertIn("must start with a year-month-day date", invalid_setup.text)

        setup = self.client.post(
            f"/projects/{project_id}/test-runs/new",
            data={
                "csrf_token": csrf,
                "operation_id": operation_id,
                "expected_workspace_revision": expected_revision,
                "label": "Fresh customer Test",
                "export_as_of": "2026-08-24",
                "recipe_revision": (
                    f"{publication.recipe.recipe_id}:"
                    f"{publication.revision.version}"
                ),
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

        self.assertEqual(setup.status_code, 303)
        self.assertRegex(
            setup.headers["location"],
            rf"^/projects/{project_id}/test-runs/.+/fresh-data$",
        )
        setup_location = setup.headers["location"]
        setup_run_id = setup_location.split("/")[4]
        self.assertEqual(
            self.client.get(
                f"/projects/{project_id}/test-runs/{setup_run_id}/activate"
            ).status_code,
            404,
        )

        fresh_data = self.client.get(setup_location)
        self.assertEqual(fresh_data.status_code, 200)
        self.assertIn("Fresh data", fresh_data.text)
        self.assertIn("Check Odoo", fresh_data.text)
        self.assertIn("Review and load", fresh_data.text)
        self.assertIn("Customers", fresh_data.text)
        self.assertIn("Customer name", fresh_data.text)
        self.assertIn("Recipe v1", fresh_data.text)
        self.assertIn("2026-08-24", fresh_data.text)
        fresh_files_action = f"{setup_location}/files"
        self.assertIn("Add fresh files", fresh_data.text)
        self.assertIn(f'action="{fresh_files_action}"', fresh_data.text)
        self.assertIn('name="source_file"', fresh_data.text)
        self.assertNotIn("Match data", fresh_data.text)
        self.assertNotIn("Prepare data", fresh_data.text)
        self.assertNotIn("Final review", fresh_data.text)
        self.assertNotIn("Load into Odoo", fresh_data.text)

        fresh_csrf = re.search(
            r'name="csrf_token" value="([^"]+)"', fresh_data.text
        ).group(1)
        fresh_revision = re.search(
            r'name="revision" value="([^"]+)"', fresh_data.text
        ).group(1)
        missing_file = self.client.post(
            fresh_files_action,
            data={"csrf_token": fresh_csrf, "revision": fresh_revision},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(missing_file.status_code, 422)
        self.assertIn("Choose a CSV or XLSX file", missing_file.text)
        self.assertIn(f'action="{fresh_files_action}"', missing_file.text)
        uploaded = self.client.post(
            fresh_files_action,
            data={"csrf_token": fresh_csrf, "revision": fresh_revision},
            files={
                "source_file": (
                    "wrong.csv",
                    b"Customer name\nWrong customer\n",
                    "text/csv",
                )
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertEqual(uploaded.headers["location"], setup_location)

        uploaded_page = self.client.get(setup_location)
        self.assertIn("Added 1 fresh file to this Test run.", uploaded_page.text)
        self.assertIn("wrong.csv", uploaded_page.text)
        self.assertIn("Check files and match tables", uploaded_page.text)
        remove_action = re.search(
            r'<form\s+class="source-file-remove-form"\s+method="post"\s+'
            r'action="([^"]+)"',
            uploaded_page.text,
        ).group(1)
        current_revision = re.search(
            r'name="revision" value="([^"]+)"', uploaded_page.text
        ).group(1)
        removed = self.client.post(
            remove_action,
            data={"csrf_token": fresh_csrf, "revision": current_revision},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        self.assertEqual(removed.headers["location"], setup_location)

        empty_again = self.client.get(setup_location)
        self.assertIn("Removed wrong.csv from this Test run.", empty_again.text)
        self.assertNotIn("<strong>wrong.csv</strong>", empty_again.text)
        current_revision = re.search(
            r'name="revision" value="([^"]+)"', empty_again.text
        ).group(1)
        uploaded = self.client.post(
            fresh_files_action,
            data={"csrf_token": fresh_csrf, "revision": current_revision},
            files={
                "source_file": (
                    "customers.csv",
                    b"Customer name,Email\nCurrent customer,current@example.com\n",
                    "text/csv",
                )
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)
        ready_page = self.client.get(setup_location)
        current_revision = re.search(
            r'name="revision" value="([^"]+)"', ready_page.text
        ).group(1)
        registered = self.client.post(
            f"{setup_location}/register",
            data={"csrf_token": fresh_csrf, "revision": current_revision},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        setup_binding = context.test_runs.get(setup_run_id, actor=context.actor)
        setup_workspace_id = setup_binding.setup_workspace_id
        self.assertEqual(registered.headers["location"], setup_location)

        registered_fresh_data = self.client.get(setup_location)
        self.assertIn("Recipe table matches", registered_fresh_data.text)
        self.assertIn("customers.csv", registered_fresh_data.text)
        self.assertIn("Matched", registered_fresh_data.text)
        self.assertIn("Details for this run", registered_fresh_data.text)
        self.assertIn("Batch reference", registered_fresh_data.text)
        self.assertIn('name="parameter_0"', registered_fresh_data.text)
        self.assertIn("Use this fresh data", registered_fresh_data.text)
        self.assertIn('name="source_file"', registered_fresh_data.text)
        self.assertNotIn(
            f'/workspaces/{setup_workspace_id}/sources',
            registered_fresh_data.text,
        )

        missing_run_value = self.client.post(
            f"{setup_location}/accept",
            data={
                "csrf_token": fresh_csrf,
                "parameter_revision": "",
                "parameter_0": "",
                "warnings_acknowledged": "1",
            },
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(missing_run_value.status_code, 422)
        self.assertIn("Enter Batch reference", missing_run_value.text)

        accepted = self.client.post(
            f"{setup_location}/accept",
            data={
                "csrf_token": fresh_csrf,
                "parameter_revision": "",
                "parameter_0": "AUGUST-REHEARSAL",
                "warnings_acknowledged": "1",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        self.assertEqual(accepted.headers["location"], setup_location)
        accepted_page = self.client.get(setup_location)
        self.assertIn(
            "Accepted the fresh data and its Recipe table matches.",
            accepted_page.text,
        )
        self.assertIn("AUGUST-REHEARSAL", accepted_page.text)
        self.assertIn("Continue to Check Odoo", accepted_page.text)
        self.assertNotIn('name="source_file"', accepted_page.text)

        odoo_url = f"/projects/{project_id}/runs/{setup_run_id}/odoo"
        first_odoo_entry = self.client.get(odoo_url, follow_redirects=False)
        self.assertEqual(first_odoo_entry.status_code, 303)
        self.assertEqual(
            first_odoo_entry.headers["location"],
            f"/workspaces/{setup_workspace_id}/target",
        )
        target_page = self.client.get(first_odoo_entry.headers["location"])
        self.assertIn("Connect the Test Odoo server", target_page.text)
        self.assertIn(
            f'href="{setup_location}"',
            target_page.text,
        )
        self.assertIn("Use this Odoo and continue", target_page.text)

        setup_state = context.workspace_states.repository.get(setup_workspace_id)
        context.workspace_states.update_target(
            setup_workspace_id,
            actor=context.actor,
            expected_revision=setup_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://test-odoo.example.test",
            odoo_database="test_odoo",
            intended_applications=(),
            intended_models=context.test_runs.required_models_for_workspace(
                setup_workspace_id,
                actor=context.actor,
            ),
        )
        odoo_page = self.client.get(odoo_url)
        self.assertEqual(odoo_page.status_code, 200)
        self.assertIn("Odoo information this run needs", odoo_page.text)
        self.assertIn("Contacts", odoo_page.text)
        self.assertNotIn("Countries", odoo_page.text)
        self.assertIn("do not require current values", odoo_page.text)
        self.assertIn("Check this Odoo", odoo_page.text)
        self.assertNotIn("Choose the Odoo data you need", odoo_page.text)
        self.assertNotIn("data-model-picker", odoo_page.text)

        check_operation_id = re.search(
            r'name="operation_id" value="([^"]+)"',
            odoo_page.text,
        ).group(1)
        check_project_revision = re.search(
            r'name="expected_workspace_revision" value="([^"]+)"',
            odoo_page.text,
        ).group(1)
        missing_reader = self.client.post(
            f"/projects/{project_id}/test-runs/{setup_run_id}/odoo/check",
            data={
                "csrf_token": fresh_csrf,
                "expected_workspace_revision": check_project_revision,
                "operation_id": check_operation_id,
            },
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(missing_reader.status_code, 422)
        self.assertIn(
            f'name="operation_id" value="{check_operation_id}"',
            missing_reader.text,
        )
        setup_state = context.workspace_states.repository.get(setup_workspace_id)
        credential = store_target_credential(
            context.secret_store,
            setup_state,
            TargetCredentialRole.READ,
            "test-read-key",
            persistent=False,
        )
        target_hash = target_identity_hash(
            connection_mode="REMOTE",
            base_url=setup_state.odoo_base_url,
            database=setup_state.odoo_database,
        )
        fingerprint = TargetFingerprint(
            target_hash=target_hash,
            connection_mode="REMOTE",
            database=setup_state.odoo_database,
            odoo_version="19.0",
            snapshot_timestamp="2026-08-25T00:00:00Z",
        )
        context.schema_reader = lambda _state, _secret: MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                "res.partner": ModelMetadata(
                    "res.partner",
                    "Contacts",
                    {
                        "email": FieldMetadata("email", "char", label="Email"),
                        "name": FieldMetadata("name", "char", label="Name"),
                    },
                )
            },
        )
        context.read_identity_probe = lambda _state, secret, models: (
            OdooReadIdentity(
                target_hash=target_hash,
                principal_hash=content_hash("test-reader"),
                permission_hash=content_hash(tuple(sorted(models))),
                context_hash=content_hash("test-context"),
                readable_models=tuple(sorted(models)),
                observed_at="2026-08-25T00:00:00Z",
            )
        )
        activated_result = SimpleNamespace(applications=(SimpleNamespace(),))
        with patch.object(
            context.test_runs,
            "activate",
            return_value=activated_result,
        ) as activate:
            checked = self.client.post(
                f"/projects/{project_id}/test-runs/{setup_run_id}/odoo/check",
                data={
                    "csrf_token": fresh_csrf,
                    "expected_workspace_revision": check_project_revision,
                    "operation_id": check_operation_id,
                },
                headers={"Origin": "http://testserver"},
                follow_redirects=False,
            )
        self.assertEqual(checked.status_code, 303, checked.text)
        self.assertEqual(
            checked.headers["location"],
            f"/projects/{project_id}/runs/{setup_run_id}",
        )
        activate.assert_called_once()
        self.assertEqual(
            activate.call_args.kwargs["credential_generation"],
            credential.binding_hash,
        )

        copied_setup_url = self.client.get(
            f"/workspaces/{setup_workspace_id}/schema",
            follow_redirects=False,
        )
        self.assertEqual(copied_setup_url.status_code, 303)
        self.assertEqual(copied_setup_url.headers["location"], odoo_url)

        saved_setup_state = context.workspace_states.repository.get(
            setup_workspace_id
        )
        forged_picker = self.client.post(
            f"/workspaces/{setup_workspace_id}/schema",
            data={
                "csrf_token": fresh_csrf,
                "revision": str(saved_setup_state.revision),
                "permitted_models": "product.template",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(forged_picker.status_code, 303)
        self.assertEqual(forged_picker.headers["location"], odoo_url)
        self.assertEqual(
            context.workspace_states.repository.get(
                setup_workspace_id
            ).intended_models,
            ("res.partner",),
        )

        for stale_path in ("overview", "mapping", "summary", "load"):
            stale = self.client.get(
                f"/workspaces/{setup_workspace_id}/{stale_path}",
                follow_redirects=False,
            )
            self.assertEqual(stale.status_code, 303)
            self.assertEqual(
                stale.headers["location"],
                f"/projects/{project_id}/test-runs/{setup_run_id}/fresh-data",
            )


if __name__ == "__main__":
    unittest.main()


