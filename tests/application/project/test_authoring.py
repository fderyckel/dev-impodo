"""Verify Project-native creation and optional Recipe publication."""

from __future__ import annotations

import re
import shutil
import unittest
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.migration_workspace_engine_database import (
    MigrationWorkspaceEngineDatabase,
)
from impodo.adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from impodo.adapters.duckdb.recipe_repository import (
    RecipeRepository,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.adapters.protected_recipe_store import ProtectedRecipeStore
from impodo.application.data_version.service import DataVersionService
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackageService,
)
from impodo.application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from impodo.application.project.service import MigrationProjectService
from impodo.application.recipe_compilation_service import CompiledRecipeDefinition
from impodo.application.recipe_publication_service import (
    RecipePublicationService,
)
from impodo.application.run.service import MigrationRunService
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.domain.data_version.models import DataVersionPurpose, DataVersionState
from impodo.domain.project.foundation import MigrationOperationState, utc_now
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.workbench import WorkspaceStateService
from impodo.web.app import create_local_app
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


class SimulatedCrash(RuntimeError):
    pass


class FixedCompiler:
    def __init__(self) -> None:
        self.generation = 1

    def compile_workspace(self, workspace_id):
        return (
            CompiledRecipeDefinition(
                recipe={
                    "contract_versions": {"mapping": 1},
                    "source_shape": {"datasets": ["customers"]},
                    "parameter_definitions": {"parameters": []},
                    "source_preparation": {},
                    "mapping": {
                        "res.partner": {
                            "name": "customer_name",
                            "generation": self.generation,
                        }
                    },
                    "odoo_target_contract": {"models": ["res.partner"]},
                    "target_governance": {},
                    "quality": {},
                    "reference_dependencies": [],
                    "control_definitions": [],
                },
                compatibility_hints={"logical_datasets": ["customers"]},
                source_selection_hash=content_hash({"workspace": workspace_id}),
                mapping_id=str(uuid4()),
                mapping_version=1,
                mapping_content_hash=content_hash(
                    {"mapping": workspace_id, "generation": self.generation}
                ),
                schema_hash=content_hash({"schema": "odoo-19"}),
                quality_ruleset_hash=content_hash({"quality": "default"}),
            ),
            (),
        )


class ProjectAuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"project-authoring-{uuid4()}"
        self.root.mkdir()
        self.authorization = CapabilityAuthorizationPolicy()
        self.database = MigrationFoundationDatabase(self.root)
        self.foundation = MigrationFoundationRepository(self.database)
        self.projects = MigrationProjectService(
            self.foundation,
            self.authorization,
        )
        self.data_versions = DataVersionService(
            self.foundation,
            self.authorization,
        )
        self.runs = MigrationRunService(self.foundation, self.authorization)
        self.migration_workspaces = MigrationWorkspaceService(
            self.foundation,
            self.authorization,
        )
        engine_database = MigrationWorkspaceEngineDatabase(self.database)
        self.workspace_repository = MigrationWorkspaceStateRepository(
            engine_database,
            self.foundation,
        )
        self.workspace_states = WorkspaceStateService(
            self.workspace_repository,
            self.authorization,
        )
        self.authoring = MigrationProjectAuthoringService(
            self.projects,
            self.data_versions,
            self.runs,
            self.migration_workspaces,
            DataVersionSourcePackageService(
                self.foundation,
                self.authorization,
            ),
            self.workspace_states,
        )
        protected = ProtectedRecipeStore(
            self.root,
            MemorySecretStore(),
        )
        self.recipe_repository = RecipeRepository(
            self.foundation,
            protected,
        )
        self.compiler = FixedCompiler()
        self.publication = RecipePublicationService(
            self.recipe_repository,
            self.compiler,
            self.authorization,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _bundle(self, request_id: str | None = None):
        return self.authoring.create(
            actor=LOCAL_ACTOR,
            display_name="Customer migration",
            source_mode="FILE",
            creation_request_id=request_id or str(uuid4()),
        )

    def _freeze_data_version(self, bundle) -> None:
        current = self.foundation.get_data_version(
            bundle.data_version.data_version_id
        )
        self.foundation.save_data_version(
            replace(
                current,
                state=DataVersionState.FROZEN,
                source_package_hash=content_hash({"package": "authoring"}),
                updated_at=utc_now(),
                frozen_at=utc_now(),
            ),
            expected_revision=current.optimistic_revision,
            event_type="TEST_DATA_VERSION_FROZEN",
            actor=LOCAL_ACTOR,
        )

    def _add_target_binding_schema(self, bundle) -> str:
        target_binding_id = str(uuid4())
        with self.database.connect(self.database.registry_path) as connection:
            connection.execute(
                """
                INSERT INTO target_binding (
                    target_binding_id, project_id, migration_run_id, environment,
                    connection_target_hash, credential_role, credential_generation,
                    principal_hash, permission_hash, context_hash,
                    schema_dependency_hash, reference_snapshot_hashes_json,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    target_binding_id,
                    bundle.project.project_id,
                    bundle.run.migration_run_id,
                    "TEST",
                    "target-hash",
                    "READ_ONLY",
                    "generation-1",
                    "principal-hash",
                    "permission-hash",
                    "context-hash",
                    "schema-dependency-hash",
                    "[]",
                    "binding-content-hash",
                    utc_now().isoformat(),
                ],
            )
            connection.execute(
                """
                INSERT INTO migration_run_target_schema (
                    migration_run_id, target_binding_id, requirement_plan_hash,
                    schema_hash, schema_json, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    bundle.run.migration_run_id,
                    target_binding_id,
                    "requirement-plan-hash",
                    "schema-hash",
                    "{}",
                    utc_now().isoformat(),
                ],
            )
        return target_binding_id

    def test_new_project_has_four_distinct_roots_and_no_recipe(self) -> None:
        request_id = str(uuid4())
        bundle = self._bundle(request_id)
        identities = {
            bundle.project.project_id,
            bundle.data_version.data_version_id,
            bundle.run.migration_run_id,
            bundle.workspace.workspace_id,
        }
        self.assertEqual(len(identities), 4)
        self.assertEqual(
            self.recipe_repository.list_recipes(bundle.project.project_id),
            (),
        )
        self.assertEqual(
            bundle.workspace_state.workspace_id,
            bundle.workspace.workspace_id,
        )
        self.assertEqual(
            self.authoring.create(
                actor=LOCAL_ACTOR,
                display_name="Customer migration",
                source_mode="FILE",
                creation_request_id=request_id,
            ).workspace.workspace_id,
            bundle.workspace.workspace_id,
        )

    def test_delete_removes_only_the_selected_project_and_owned_stores(self) -> None:
        selected = self._bundle()
        retained = self._bundle()
        self._freeze_data_version(selected)
        self.data_versions.create(
            selected.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=selected.project.optimistic_revision,
            purpose=DataVersionPurpose.TEST,
            label="Later Test delivery",
            parent_data_version_id=selected.data_version.data_version_id,
        )
        published = self.publication.publish(
            project_id=selected.project.project_id,
            data_version_id=selected.data_version.data_version_id,
            workspace_id=selected.workspace.workspace_id,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )

        selected_project_dir = self.database.project_directory(
            selected.project.project_id
        )
        protected_recipe_dir = (
            self.root / ".recipes-protected" / published.recipe.recipe_id
        )
        self.assertTrue(selected_project_dir.is_dir())
        self.assertTrue(protected_recipe_dir.is_dir())

        deleted = self.projects.delete(
            selected.project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=self.projects.get(
                selected.project.project_id,
                actor=LOCAL_ACTOR,
            ).optimistic_revision,
        )

        self.assertEqual(deleted.project_id, selected.project.project_id)
        self.assertFalse(selected_project_dir.exists())
        self.assertFalse(protected_recipe_dir.exists())
        self.assertEqual(
            tuple(item.project_id for item in self.projects.list(actor=LOCAL_ACTOR)),
            (retained.project.project_id,),
        )
        self.assertEqual(
            self.projects.get(retained.project.project_id, actor=LOCAL_ACTOR),
            retained.project,
        )

    def test_delete_removes_target_binding_after_its_saved_dependants(self) -> None:
        selected = self._bundle()
        target_binding_id = self._add_target_binding_schema(selected)

        self.projects.delete(
            selected.project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=selected.project.optimistic_revision,
        )

        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM target_binding WHERE target_binding_id = ?",
                    [target_binding_id],
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT count(*) FROM migration_run_target_schema
                    WHERE target_binding_id = ?
                    """,
                    [target_binding_id],
                ).fetchone(),
                (0,),
            )

    def test_delete_restores_target_dependants_when_final_cleanup_fails(self) -> None:
        selected = self._bundle()
        target_binding_id = self._add_target_binding_schema(selected)
        project_directory = self.database.project_directory(
            selected.project.project_id
        )

        with patch.object(
            MigrationFoundationRepository,
            "_delete_project_registry_rows",
            side_effect=SimulatedCrash("registry cleanup failed"),
        ), self.assertRaises(SimulatedCrash):
            self.projects.delete(
                selected.project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=selected.project.optimistic_revision,
            )

        self.assertTrue(project_directory.is_dir())
        self.assertEqual(
            self.projects.get(selected.project.project_id, actor=LOCAL_ACTOR),
            selected.project,
        )
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT count(*) FROM migration_run_target_schema
                    WHERE target_binding_id = ?
                    """,
                    [target_binding_id],
                ).fetchone(),
                (1,),
            )

    def test_first_publication_does_not_change_project_or_data_version_identity(self) -> None:
        bundle = self._bundle()
        self._freeze_data_version(bundle)
        before_project = self.projects.get(bundle.project.project_id, actor=LOCAL_ACTOR)
        before_data = self.data_versions.get(
            bundle.data_version.data_version_id,
            actor=LOCAL_ACTOR,
        )
        result = self.publication.publish(
            project_id=bundle.project.project_id,
            data_version_id=bundle.data_version.data_version_id,
            workspace_id=bundle.workspace.workspace_id,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(result.revision.version, 1)
        self.assertEqual(result.recipe.project_id, bundle.project.project_id)
        self.assertEqual(
            self.projects.get(bundle.project.project_id, actor=LOCAL_ACTOR).project_id,
            before_project.project_id,
        )
        self.assertEqual(
            self.data_versions.get(
                bundle.data_version.data_version_id,
                actor=LOCAL_ACTOR,
            ).data_version_id,
            before_data.data_version_id,
        )
        envelope = self.recipe_repository.read_recipe_revision(
            result.recipe.recipe_id,
            1,
        )
        self.assertEqual(
            envelope["provenance"]["origin_workspace_id"],
            bundle.workspace.workspace_id,
        )

    def test_publication_recovers_after_artifact_store_fault_and_adds_one_recipe(self) -> None:
        bundle = self._bundle()
        self._freeze_data_version(bundle)
        operation_id = str(uuid4())

        def fault(stage: str) -> None:
            if stage == "ARTIFACT_STORED":
                raise SimulatedCrash(stage)

        with self.assertRaises(SimulatedCrash):
            self.publication.publish(
                project_id=bundle.project.project_id,
                data_version_id=bundle.data_version.data_version_id,
                workspace_id=bundle.workspace.workspace_id,
                display_name="Customers",
                business_purpose="Prepare customers and contacts",
                operation_id=operation_id,
                actor=LOCAL_ACTOR,
                fault=fault,
            )
        self.assertEqual(
            self.foundation.get_operation_intent(operation_id).state,
            MigrationOperationState.PENDING,
        )
        recovered = self.publication.publish(
            project_id=bundle.project.project_id,
            data_version_id=bundle.data_version.data_version_id,
            workspace_id=bundle.workspace.workspace_id,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            operation_id=operation_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(recovered.revision.version, 1)
        self.assertEqual(
            len(self.recipe_repository.list_recipes(bundle.project.project_id)),
            1,
        )

    def test_successor_revision_keeps_recipe_and_data_version_ownership_separate(self) -> None:
        bundle = self._bundle()
        self._freeze_data_version(bundle)
        first = self.publication.publish(
            project_id=bundle.project.project_id,
            data_version_id=bundle.data_version.data_version_id,
            workspace_id=bundle.workspace.workspace_id,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            actor=LOCAL_ACTOR,
        )
        self.compiler.generation = 2
        second = self.publication.publish(
            project_id=bundle.project.project_id,
            data_version_id=bundle.data_version.data_version_id,
            workspace_id=bundle.workspace.workspace_id,
            recipe_id=first.recipe.recipe_id,
            expected_recipe_revision=first.recipe.optimistic_revision,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(second.recipe.recipe_id, first.recipe.recipe_id)
        self.assertEqual(second.revision.version, 2)
        self.assertEqual(
            self.foundation.get_data_version(
                bundle.data_version.data_version_id
            ).project_id,
            bundle.project.project_id,
        )

    def test_unchanged_successor_returns_current_recipe_revision(self) -> None:
        bundle = self._bundle()
        self._freeze_data_version(bundle)
        first = self.publication.publish(
            project_id=bundle.project.project_id,
            data_version_id=bundle.data_version.data_version_id,
            workspace_id=bundle.workspace.workspace_id,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            actor=LOCAL_ACTOR,
        )

        unchanged = self.publication.publish(
            project_id=bundle.project.project_id,
            data_version_id=bundle.data_version.data_version_id,
            workspace_id=bundle.workspace.workspace_id,
            recipe_id=first.recipe.recipe_id,
            expected_recipe_revision=first.recipe.optimistic_revision,
            display_name="Customers",
            business_purpose="Prepare customers and contacts",
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(unchanged.revision.version, 1)
        self.assertEqual(
            self.recipe_repository.list_recipe_revisions(first.recipe.recipe_id),
            (first.revision,),
        )


class ProjectAuthoringBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"pab-{uuid4().hex}"
        self.root.mkdir()
        self.app = create_local_app(
            self.root,
            launch_token="project-authoring-launch",
            session_secret="project-authoring-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        self.client = TestClient(self.app)
        launched = self.client.get(
            "/launch?token=project-authoring-launch",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)

    def tearDown(self) -> None:
        self.client.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _csrf(page: str) -> str:
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        if match is None:
            raise AssertionError("CSRF token not found")
        return match.group(1)

    def test_browser_creates_project_without_recipe_and_removes_recipe_root_routes(
        self,
    ) -> None:
        listing = self.client.get("/projects")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("No data projects yet", listing.text)
        self.assertIn("New project", listing.text)
        self.assertNotIn("Create Recipe", listing.text)
        form = self.client.get("/projects/new")
        self.assertEqual(form.status_code, 200)
        request_id = re.search(
            r'name="creation_request_id" value="([^"]+)"',
            form.text,
        )
        self.assertIsNotNone(request_id)
        created = self.client.post(
            "/projects/new",
            data={
                "csrf_token": self._csrf(form.text),
                "creation_request_id": request_id.group(1),
                "display_name": "Thailand customer rollout",
                "migration_purpose": "Prepare customers for Odoo 19",
                "source_mode": "FILE",
                "source_system_identity": "Legacy ERP",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)
        self.assertRegex(created.headers["location"], r"^/projects/[0-9a-f-]{36}$")
        overview = self.client.get(created.headers["location"])
        self.assertEqual(overview.status_code, 200)
        self.assertIn(
            "You can complete this migration once without saving a Recipe",
            overview.text,
        )
        summaries = self.app.state.context.migration_projects.list(
            actor=self.app.state.context.actor
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].recipe_count, 0)
        workspaces = self.app.state.context.migration_workspaces.list_for_project(
            summaries[0].project_id,
            actor=self.app.state.context.actor,
        )
        self.assertEqual(len(workspaces), 1)
        workspace_page = self.client.get(
            f"/workspaces/{workspaces[0].workspace_id}",
            follow_redirects=False,
        )
        self.assertEqual(workspace_page.status_code, 303)
        self.assertEqual(
            workspace_page.headers["location"],
            f"/workspaces/{workspaces[0].workspace_id}/files",
        )
        self.assertEqual(self.client.get("/recipes").status_code, 404)
        self.assertEqual(self.client.get("/recipes/new").status_code, 404)

    def test_project_list_survives_a_clean_application_restart(self) -> None:
        context = self.app.state.context
        created = context.project_authoring.create(
            actor=context.actor,
            display_name="Restart-safe Project",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )
        self.client.close()
        restarted_app = create_local_app(
            self.root,
            launch_token="project-restart-launch",
            session_secret="project-restart-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        self.app = restarted_app
        self.client = TestClient(restarted_app)
        launched = self.client.get(
            "/launch?token=project-restart-launch",
            follow_redirects=False,
        )
        self.assertEqual(launched.status_code, 303)

        listing = self.client.get("/projects")

        self.assertEqual(listing.status_code, 200)
        self.assertIn("Restart-safe Project", listing.text)
        self.assertEqual(
            restarted_app.state.context.migration_projects.get(
                created.project.project_id,
                actor=restarted_app.state.context.actor,
            ).project_id,
            created.project.project_id,
        )

    def test_project_entry_shows_and_confirms_permanent_deletion(self) -> None:
        context = self.app.state.context
        created = context.project_authoring.create(
            actor=context.actor,
            display_name="Project to delete",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )
        project_id = created.project.project_id
        listing = self.client.get("/projects")
        self.assertIn("data-project-list-delete-trigger", listing.text)
        self.assertIn(
            'aria-label="Delete project Project to delete"',
            listing.text,
        )
        self.assertIn("#trash3", listing.text)
        self.assertIn(
            f'action="/projects/{project_id}/delete"',
            listing.text,
        )
        overview = self.client.get(f"/projects/{project_id}")

        self.assertEqual(overview.status_code, 200)
        self.assertIn("Delete this project", overview.text)
        self.assertIn("data-project-delete-trigger", overview.text)
        self.assertIn("data-project-delete-dialog", overview.text)
        self.assertIn(
            f'action="/projects/{project_id}/delete"',
            overview.text,
        )

        deleted = self.client.post(
            f"/projects/{project_id}/delete",
            data={
                "csrf_token": self._csrf(overview.text),
                "revision": str(created.project.optimistic_revision),
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(deleted.headers["location"], "/projects")
        listing = self.client.get("/projects")
        self.assertIn(
            "Deleted project &#34;Project to delete&#34;.",
            listing.text,
        )
        self.assertNotIn("Project to delete", listing.text.split("Deleted project")[0])

    def test_project_list_scrolls_after_five_projects(self) -> None:
        context = self.app.state.context
        for number in range(5):
            context.migration_projects.create(
                actor=context.actor,
                display_name=f"Project {number + 1}",
                migration_purpose="Verify the bounded Project list",
                source_system_identity="Fictional ERP",
            )

        five_projects = self.client.get("/projects")

        self.assertEqual(five_projects.status_code, 200)
        self.assertIn('class="project-list-scroll"', five_projects.text)
        self.assertNotIn(
            'class="project-list-scroll is-scrollable"',
            five_projects.text,
        )

        context.migration_projects.create(
            actor=context.actor,
            display_name="Project 6",
            migration_purpose="Verify the bounded Project list",
            source_system_identity="Fictional ERP",
        )
        six_projects = self.client.get("/projects")

        self.assertEqual(six_projects.status_code, 200)
        self.assertIn(
            'class="project-list-scroll is-scrollable"',
            six_projects.text,
        )
        self.assertIn('role="region"', six_projects.text)
        self.assertIn(
            'aria-label="Project rows. Scroll to see more projects."',
            six_projects.text,
        )
        self.assertIn('tabindex="0"', six_projects.text)
        self.assertEqual(six_projects.text.count('class="project-row"'), 6)

        styles = self.client.get("/static/components.css")
        self.assertEqual(styles.status_code, 200)
        self.assertIn(".project-list-scroll.is-scrollable", styles.text)
        self.assertIn("max-block-size: 460px", styles.text)

    def test_file_acceptance_freezes_data_version_and_projects_references(self) -> None:
        context = self.app.state.context
        bundle = context.project_authoring.create(
            actor=context.actor,
            display_name="Customer source ownership",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )
        workspace_id = bundle.workspace.workspace_id
        with patch(
            "impodo.application.data_version.intake.validate_source_file_isolated",
            lambda _path: None,
        ):
            source_file = context.intake.accept(
                workspace_id,
                actor=context.actor,
                expected_revision=bundle.workspace_state.revision,
                display_name="customers.csv",
                stream=BytesIO(b"code,name\nC001,Acme\n"),
            )
        context.workspace_states.register(
            workspace_id,
            actor=context.actor,
            expected_revision=context.queries.get(workspace_id).revision,
        )
        with patch(
            "impodo.application.data_version.source_worker.inspect_source_file_isolated",
            side_effect=lambda path, *, source_file, options, inspector, catalog_from_json, inspection_error: inspector(
                path,
                source_file=source_file,
                options=options,
            ),
        ):
            context.inspections.inspect_project(workspace_id, actor=context.actor)
        context.sources.confirm_source(
            workspace_id,
            source_file.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=context.actor,
        )
        selection = context.sources.freeze_selection(
            workspace_id,
            dataset_names={(source_file.file_id, "csv"): "customers"},
            actor=context.actor,
        )
        projection = context.data_version_source_projection.accept_file_selection(
            workspace_id,
            selection,
            actor=context.actor,
        )

        data_version = context.data_versions.get(
            bundle.data_version.data_version_id,
            actor=context.actor,
        )
        self.assertIs(data_version.state, DataVersionState.FROZEN)
        package = context.data_version_source_projection.packages.repository.get_source_package(
            data_version.data_version_id
        )
        self.assertEqual(projection.package_hash, package.content_hash)
        self.assertEqual(
            tuple(item.dataset_id for item in projection.datasets),
            tuple(item.dataset_id for item in package.datasets),
        )
        self.assertTrue(
            (
                self.root
                / "artifacts"
                / "dv"
                / data_version.data_version_id
                / "inbox"
                / package.files[0].storage_key
            ).is_file()
        )
        self.assertFalse(
            (self.root / "artifacts" / "ws" / workspace_id / "inbox").exists()
        )

    def test_odoo_acceptance_freezes_the_same_data_version_boundary(self) -> None:
        context = self.app.state.context
        bundle = context.project_authoring.create(
            actor=context.actor,
            display_name="Odoo source ownership",
            source_mode="ODOO",
            creation_request_id=str(uuid4()),
        )
        workspace_id = bundle.workspace.workspace_id
        evidence_hash = content_hash({"capture": workspace_id})
        source = OdooSourceBinding(
            capture_selection_hash=evidence_hash,
            model="res.partner",
            policy_hash=content_hash({"policy": 1}),
            connection_target_hash=content_hash({"target": 1}),
            schema_scope_hash=content_hash({"schema": 1}),
            read_principal_hash=content_hash({"principal": 1}),
            read_permission_hash=content_hash({"permissions": 1}),
            context_hash=content_hash({"context": 1}),
        )
        dataset_id = "dataset:" + "a" * 24
        columns = (
            SourceDatasetColumn(
                ordinal=1,
                source_name="name",
                stable_key="odoo:res.partner:name",
                candidate_type="string",
            ),
        )
        data_version_id = bundle.data_version.data_version_id
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=data_version_id,
            created_at=utc_now(),
            created_by="Local operator",
            datasets=(
                SourceDataset(
                    dataset_id=dataset_id,
                    name="Contacts",
                    source=source,
                    row_count=2,
                    columns=columns,
                ),
            ),
            content_hash=content_hash({"selection": workspace_id}),
        )
        schema = SourceSnapshotSchema.create(
            (
                SourceSnapshotColumn.create(
                    ordinal=1,
                    source_name="name",
                    stable_key="odoo:res.partner:name",
                    candidate_type="string",
                ),
            )
        )
        snapshot = SourceSnapshot.create(
            data_version_id=data_version_id,
            dataset_id=dataset_id,
            dataset_name="Contacts",
            source=source,
            physical_selection_hash=selection.content_hash,
            schema=schema,
            row_count=2,
            data_logical_hash=content_hash({"rows": ("Alice", "Bob")}),
            parquet_sha256=content_hash({"parquet": 1}),
            created_at=utc_now(),
        )
        manifest = SimpleNamespace(
            data_version_id=data_version_id,
            dataset_id=dataset_id,
            dataset_name="Contacts",
            row_count=2,
            data_logical_hash=snapshot.data_logical_hash,
            data_sha256=snapshot.parquet_sha256,
            data_storage_key=snapshot.parquet_storage_key,
            data_size_bytes=512,
            manifest_id=str(uuid4()),
            content_hash=content_hash({"manifest": workspace_id}),
            provenance_logical_hash=content_hash({"origins": 1}),
            provenance_sha256=content_hash({"encrypted": 1}),
            provenance_size_bytes=128,
            provenance_storage_key="captures/origins.iprv",
        )

        projection = context.data_version_source_projection.accept_odoo_capture(
            workspace_id,
            selection,
            snapshot,
            manifest,
            actor=context.actor,
        )
        data_version = context.data_versions.get(
            bundle.data_version.data_version_id,
            actor=context.actor,
        )
        package = (
            context.data_version_source_projection.packages.repository
            .get_source_package(data_version.data_version_id)
        )

        self.assertIs(data_version.state, DataVersionState.FROZEN)
        self.assertEqual(package.origin.value, "ODOO")
        self.assertEqual(projection.package_hash, package.content_hash)
        self.assertEqual(package.datasets[0].source_file_ids, ())
        self.assertEqual(
            package.datasets[0].manifest["capture_manifest_hash"],
            manifest.content_hash,
        )


if __name__ == "__main__":
    unittest.main()
