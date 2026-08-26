"""Make the current identity vocabulary and ambiguity gate executable."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


CANONICAL_IDENTITY_FIELDS = {
    ("src/impodo/domain/project/models.py", "MigrationProject"): {
        "project_id",
    },
    ("src/impodo/domain/data_version/models.py", "DataVersion"): {
        "project_id",
        "data_version_id",
    },
    ("src/impodo/domain/run/models.py", "MigrationRun"): {
        "project_id",
        "data_version_id",
        "migration_run_id",
    },
    ("src/impodo/migration_run_setup.py", "MigrationRunTargetSetup"): {
        "project_id",
        "migration_run_id",
    },
    ("src/impodo/domain/workspace/models.py", "MigrationWorkspace"): {
        "project_id",
        "data_version_id",
        "migration_run_id",
        "workspace_id",
        "recipe_application_id",
    },
    ("src/impodo/workspace_access.py", "WorkspaceAccessContext"): {
        "project_id",
        "data_version_id",
        "migration_run_id",
        "workspace_id",
        "recipe_application_id",
    },
}


TRUE_PROJECT_ID_TYPES = frozenset(
    {
        "data_version_sources.py::DataVersionSourcePackage",
        "data_version_sources.py::WorkspaceSourceProjection",
        "domain/data_version/models.py::DataVersion",
        "incompatible_project_storage.py::UnavailableProjectSummary",
        "domain/cutover/models.py::ApplicationQualificationEvidence",
        "domain/cutover/models.py::CutoverPlan",
        "domain/cutover/models.py::CutoverPlanQualification",
        "domain/cutover/models.py::CutoverPlanRevision",
        "domain/cutover/models.py::ProjectCutoverSelection",
        "domain/cutover/models.py::RecipeApplicationQualification",
        "migration_foundation.py::MigrationOperationIntent",
        "migration_production.py::ProductionRunBinding",
        "domain/project/models.py::MigrationProject",
        "domain/project/models.py::MigrationProjectSummary",
        "migration_run_planning.py::MigrationRunRequirementPlan",
        "migration_run_planning.py::RunRecipeApplication",
        "migration_run_planning.py::RunTargetBinding",
        "domain/run/models.py::MigrationRun",
        "migration_run_setup.py::MigrationRunTargetSetup",
        "domain/workspace/models.py::MigrationWorkspace",
        "migration_test.py::TestRunParameterValues",
        "migration_test.py::TestRunSetupBinding",
        "preparation_jobs.py::PreparationWorkspace",
        "domain/recipe/models.py::Recipe",
        "workspace_access.py::WorkspaceAccessContext",
        "application/cutover_plan_service.py::IntegratedQualificationReview",
        "application/run/planning_models.py::IntegratedRunReview",
        "application/recipe_publication_service.py::RecipeDraft",
    }
)


REMOVED_AMBIGUITY_COUNTS = {
    "workspace route declarations using project_id": 0,
    "workspace URL expressions using project.project_id": 0,
    "WorkspaceState values named project": 0,
}


CURRENT_IDENTITY_SURFACES = {
    "semantic hashes and portable payloads": (
        ("src/impodo/workspace_contracts.py", "class SourceSelection"),
        ("src/impodo/workspace_contracts.py", "class OdooModelCatalog"),
        ("src/impodo/workspace_contracts.py", "class OdooSchemaCatalog"),
        ("src/impodo/workspace_contracts.py", "class MappingWorkingDraft"),
    ),
    "persisted workspace identity": (
        ("src/impodo/workspace_state.py", "class WorkspaceState"),
        (
            "src/impodo/adapters/duckdb/workspace_state_repository.py",
            '"workspace_id": workspace.workspace_id',
        ),
        (
            "src/impodo/adapters/duckdb/migration_workspace_state_repository.py",
            "get_migration_workspace(workspace_id)",
        ),
    ),
    "schema generations": (
        (
            "src/impodo/adapters/duckdb/schema/migration_registry.py",
            "MIGRATION_REGISTRY_GENERATION",
        ),
        (
            "src/impodo/adapters/duckdb/schema/data_version_store.py",
            "DATA_VERSION_STORE_GENERATION",
        ),
        (
            "src/impodo/adapters/duckdb/schema/migration_workspace_store.py",
            "MIGRATION_WORKSPACE_GENERATION",
        ),
        (
            "src/impodo/adapters/duckdb/constants.py",
            "SCHEMA_GENERATION",
        ),
    ),
    "operation and revision requests": (
        (
            "src/impodo/migration_foundation.py",
            "class MigrationOperationIntent",
        ),
        (
            "src/impodo/application/workspace/service.py",
            "expected_workspace_revision",
        ),
        (
            "src/impodo/application/data_version/service.py",
            "expected_workspace_revision",
        ),
        (
            "src/impodo/application/run/service.py",
            "expected_workspace_revision",
        ),
    ),
    "background job packets": (
        ("src/impodo/jobs.py", "class JobRequest"),
        ("src/impodo/preparation_jobs.py", "class PreparationWorkspace"),
        ("src/impodo/load_jobs.py", "class LoadJob"),
        ("src/impodo/odoo_capture_jobs.py", "class OdooCaptureJob"),
    ),
    "workspace-scoped browser session state": (
        ("src/impodo/local_stack.py", "def forget_workspace"),
        ("src/impodo/local_stack.py", "workspace_id: str"),
    ),
    "browser forms and links": (
        (
            "src/impodo/web/templates/workspace_sources.html",
            "/workspaces/{{ workspace_id }}",
        ),
        (
            "src/impodo/web/templates/mapping/page.html",
            "/workspaces/{{ workspace_id }}",
        ),
        (
            "src/impodo/web/templates/workspace_summary.html",
            "/workspaces/{{ workspace_id }}",
        ),
    ),
}


class IdentitySemanticsTests(unittest.TestCase):
    def test_canonical_ownership_matrix_uses_exact_identity_names(self) -> None:
        for (path, class_name), expected in CANONICAL_IDENTITY_FIELDS.items():
            fields = _class_annotation_fields(path, class_name)
            self.assertTrue(
                expected.issubset(fields),
                f"{class_name} is missing {sorted(expected - fields)}",
            )

        project_fields = _class_annotation_fields(
            "src/impodo/domain/project/models.py",
            "MigrationProject",
        )
        self.assertNotIn("workspace_id", project_fields)
        self.assertNotIn("recipe_id", project_fields)

    def test_every_typed_project_id_has_one_classified_meaning(self) -> None:
        self.assertEqual(
            _typed_project_identity_types(),
            TRUE_PROJECT_ID_TYPES,
        )

    def test_workspace_state_uses_its_actual_identity(self) -> None:
        fields = _class_annotation_fields(
            "src/impodo/workspace_state.py",
            "WorkspaceState",
        )
        self.assertIn("workspace_id", fields)
        self.assertNotIn("project_id", fields)

    def test_source_and_workspace_evidence_use_their_actual_owners(self) -> None:
        for path, class_name in (
            ("src/impodo/workspace_contracts.py", "SourceSelection"),
            ("src/impodo/domain/source_snapshot.py", "SourceSnapshot"),
            ("src/impodo/domain/odoo_capture.py", "OdooCaptureSelection"),
            ("src/impodo/domain/odoo_provenance.py", "OdooCaptureManifest"),
        ):
            with self.subTest(class_name=class_name):
                fields = _class_annotation_fields(path, class_name)
                self.assertIn("data_version_id", fields)
                self.assertNotIn("project_id", fields)
                self.assertNotIn("workspace_id", fields)

        for path, class_name in (
            ("src/impodo/domain/prepared_snapshot.py", "PreparedSnapshot"),
            ("src/impodo/domain/execution_snapshot.py", "ExecutionSnapshot"),
            ("src/impodo/domain/reconciliation.py", "ReconciliationRun"),
        ):
            with self.subTest(class_name=class_name):
                fields = _class_annotation_fields(path, class_name)
                self.assertIn("workspace_id", fields)
                self.assertNotIn("project_id", fields)

        run_reference_fields = _class_annotation_fields(
            "src/impodo/migration_run_planning.py",
            "MigrationRunReferenceBundle",
        )
        self.assertIn("migration_run_id", run_reference_fields)
        self.assertIn("source_workspace_id", run_reference_fields)
        self.assertNotIn("project_id", run_reference_fields)
        self.assertNotIn("workspace_id", run_reference_fields)

        run_schema_fields = _class_annotation_fields(
            "src/impodo/migration_run_planning.py",
            "MigrationRunTargetSchema",
        )
        self.assertIn("migration_run_id", run_schema_fields)
        self.assertNotIn("project_id", run_schema_fields)
        self.assertNotIn("workspace_id", run_schema_fields)

    def test_artifact_ports_expose_explicit_owners_without_alias_adapter(self) -> None:
        artifact_text = (ROOT / "src/impodo/artifacts.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("class DataVersionSourceArtifactStore", artifact_text)
        self.assertIn("class WorkspaceArtifactStore", artifact_text)
        self.assertNotIn("project_id", artifact_text)
        self.assertFalse(
            (ROOT / "src/impodo/adapters/data_version_artifact_store.py").exists()
        )

    def test_identity_ambiguity_is_removed(self) -> None:
        self.assertEqual(_current_ambiguity_counts(), REMOVED_AMBIGUITY_COUNTS)

    def test_background_jobs_use_workspace_identity(self) -> None:
        for path, class_name in (
            ("src/impodo/jobs.py", "JobRequest"),
            ("src/impodo/load_jobs.py", "LoadJob"),
            ("src/impodo/odoo_capture_jobs.py", "OdooCaptureJob"),
            ("src/impodo/preparation_jobs.py", "PreparationJob"),
        ):
            with self.subTest(class_name=class_name):
                fields = _class_annotation_fields(path, class_name)
                self.assertIn("workspace_id", fields)
                self.assertNotIn("project_id", fields)
                self.assertNotIn("project_name", fields)

    def test_local_stack_session_state_uses_workspace_identity(self) -> None:
        local_stack_text = (ROOT / "src/impodo/local_stack.py").read_text(
            encoding="utf-8"
        )
        service_text = local_stack_text.split("class LocalStackService:", 1)[1]
        self.assertIn("def forget_workspace", service_text)
        self.assertNotIn("def forget_project", service_text)
        self.assertNotIn("project_id", service_text)

    def test_workspace_state_fixtures_do_not_use_project_aliases(self) -> None:
        self.assertEqual(_workspace_state_fixture_aliases(), [])

    def test_workspace_browser_surfaces_have_no_project_alias(self) -> None:
        router_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src" / "impodo" / "web" / "routers").glob(
                "*.py"
            )
        )
        workspace_template_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src" / "impodo" / "web" / "templates").glob(
                "workspace*.html"
            )
        )
        self.assertNotIn("/workspaces/{project_id}", router_text)
        self.assertNotIn("/workspaces/{{ project.", workspace_template_text)
        self.assertNotIn("{{ project.", workspace_template_text)

    def test_project_and_workspace_route_names_cannot_cross(self) -> None:
        python_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src" / "impodo" / "web").rglob("*.py")
        )
        template_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src" / "impodo" / "web" / "templates").rglob(
                "*.html"
            )
        )
        self.assertIn("/projects/{project_id}", python_text)
        self.assertIn("/workspaces/{workspace_id}", python_text)
        self.assertNotIn("/workspaces/{project_id}", python_text)
        self.assertNotIn("/projects/{workspace_id}", python_text)
        self.assertNotIn("/workspaces/{data_version_id}", python_text)
        self.assertNotIn("/projects/{{ workspace_id }}", template_text)
        self.assertNotIn("/workspaces/{{ data_version_id }}", template_text)

    def test_every_current_identity_surface_is_registered(self) -> None:
        for category, entries in CURRENT_IDENTITY_SURFACES.items():
            with self.subTest(category=category):
                self.assertGreater(len(entries), 0)
            for relative_path, marker in entries:
                with self.subTest(category=category, path=relative_path):
                    self.assertIn(
                        marker,
                        (ROOT / relative_path).read_text(encoding="utf-8"),
                    )


def _class_annotation_fields(relative_path: str, class_name: str) -> set[str]:
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
            }
    raise AssertionError(f"{class_name} not found in {relative_path}")


def _current_ambiguity_counts() -> dict[str, int]:
    route_count = 0
    typed_project_count = 0
    for path in (ROOT / "src" / "impodo").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "/workspaces/{project_id}" in node.value
            ):
                route_count += node.value.count("/workspaces/{project_id}")
            if isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id == "project"
                    and "WorkspaceState" in ast.unparse(node.annotation)
                ):
                    typed_project_count += 1
            if isinstance(node, ast.arg) and node.annotation is not None:
                if (
                    node.arg == "project"
                    and "WorkspaceState" in ast.unparse(node.annotation)
                ):
                    typed_project_count += 1

    template_count = sum(
        path.read_text(encoding="utf-8").count(
            "/workspaces/{{ project.project_id }}"
        )
        for path in (ROOT / "src" / "impodo" / "web" / "templates").rglob(
            "*.html"
        )
    )
    return {
        "workspace route declarations using project_id": route_count,
        "workspace URL expressions using project.project_id": template_count,
        "WorkspaceState values named project": typed_project_count,
    }


def _typed_project_identity_types() -> frozenset[str]:
    result: set[str] = set()
    source_root = ROOT / "src" / "impodo"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            fields = {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
            }
            if "project_id" in fields:
                result.add(f"{path.relative_to(source_root).as_posix()}::{node.name}")
    return frozenset(result)


def _workspace_state_fixture_aliases() -> list[str]:
    aliases: list[str] = []
    for path in (ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            function_name = ast.unparse(value.func)
            if not function_name.endswith("WorkspaceState"):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                target_name = ast.unparse(target)
                if target_name == "project" or target_name.endswith(".project"):
                    aliases.append(
                        f"{path.relative_to(ROOT).as_posix()}:{target.lineno}"
                    )
    return sorted(aliases)
