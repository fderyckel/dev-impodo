"""Verify Recipe Phase R1 persistence, protection, and recovery."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.recipe_repository import RecipeRepository
from impodo.adapters.duckdb.schema.registry import RECIPE_REGISTRY_MIGRATION_ID
from impodo.adapters.protected_recipe_store import ProtectedRecipeStore
from impodo.application.recipe_service import RecipeService
from impodo.domain.serialization import content_hash
from impodo.projects import ProjectError, ProjectNotFoundError, ProjectService
from impodo.recipes import (
    DataVersionPurpose,
    DataVersionState,
    RecipeIdentifierConfusionError,
    RecipeIntegrityError,
    RecipeIntentState,
    RecipeState,
)
from impodo.secrets import MemorySecretStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "recipes" / "phase-r0" / "customer-recipe-v3.json"


class RecipePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbDatabase(self.temporary.name)
        self.projects = ProjectRepository(self.database)
        self.recipe_repository = RecipeRepository(self.database)
        self.project_service = ProjectService(
            self.projects,
            CapabilityAuthorizationPolicy(),
        )
        self.secrets = MemorySecretStore()
        self.store = ProtectedRecipeStore(self.temporary.name, self.secrets)
        self.service = RecipeService(
            self.recipe_repository,
            self.store,
            CapabilityAuthorizationPolicy(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _project_and_recipe(self, name: str = "Customer migration"):
        project = self.project_service.create_project(
            actor=LOCAL_ACTOR,
            name=name,
            source_system="CSV export",
        )
        resolution = self.recipe_repository.resolve_workspace(project.project_id)
        return project, self.recipe_repository.get(resolution.recipe_id)

    @staticmethod
    def _envelope(recipe_id: str, version: int) -> bytes:
        envelope = deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
        envelope["provenance"]["recipe_id"] = recipe_id
        envelope["provenance"]["recipe_revision"] = version
        envelope["payload_hash"] = content_hash(
            {key: value for key, value in envelope.items() if key != "payload_hash"}
        )
        return json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _publish_first_revision(self, recipe_id: str, expected_revision: int = 1):
        recipe = self.recipe_repository.get(recipe_id)
        project_id = self.recipe_repository.data_versions(recipe_id)[
            0
        ].workspace_project_id
        project = self.projects.get(project_id)
        if recipe.setup_hydration_state.value != "READY":
            recipe = self.service.hydrate_legacy_project(
                project,
                actor=LOCAL_ACTOR,
            )
            expected_revision = recipe.optimistic_revision
        return self.service.publish_revision(
            recipe_id,
            expected_recipe_revision=expected_revision,
            envelope_bytes=self._envelope(recipe_id, 1),
            actor=LOCAL_ACTOR,
        )

    def test_registry_backfill_is_bounded_and_ids_are_not_interchangeable(self) -> None:
        project, recipe = self._project_and_recipe()
        versions = self.recipe_repository.data_versions(recipe.recipe_id)

        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].workspace_project_id, project.project_id)
        self.assertEqual(versions[0].purpose, DataVersionPurpose.AUTHORING)
        self.assertEqual(versions[0].state, DataVersionState.ACTIVE)
        self.assertEqual(
            len({recipe.recipe_id, versions[0].data_version_id, project.project_id}),
            3,
        )
        with patch.object(
            self.database,
            "_ensure_project_database_schema",
            side_effect=AssertionError("Recipe list opened a project database"),
        ):
            summaries = self.recipe_repository.list()
        self.assertEqual(summaries[0].recipe_id, recipe.recipe_id)
        self.assertEqual(summaries[0].data_version_count, 1)

        with self.assertRaises(RecipeIdentifierConfusionError):
            self.recipe_repository.resolve_workspace(recipe.recipe_id)
        with self.assertRaises(RecipeIdentifierConfusionError):
            self.recipe_repository.resolve_workspace(versions[0].data_version_id)

        with self.recipe_repository._connect(
            self.recipe_repository.registry_path
        ) as connection:
            ledger = connection.execute(
                """
                SELECT count(*) FROM registry_schema_migration
                 WHERE migration_id = ?
                """,
                [RECIPE_REGISTRY_MIGRATION_ID],
            ).fetchone()
        self.assertEqual(ledger, (1,))

        self.projects.get(project.project_id)
        database_path = (
            self.projects.project_directory(project.project_id) / "project.duckdb"
        )
        with self.projects._connect(database_path) as connection:
            linkage = connection.execute(
                """
                SELECT recipe_id, data_version_id, data_version_number
                  FROM recipe_workspace_linkage
                """
            ).fetchone()
        self.assertEqual(
            linkage,
            (recipe.recipe_id, versions[0].data_version_id, 1),
        )

    def test_protected_store_encrypts_authenticates_and_contains_paths(self) -> None:
        _, recipe = self._project_and_recipe()
        payload = b'{"business_rule":"German -> de_DE"}'
        logical_hash = "sha256:" + "a" * 64
        stored = self.store.put(
            recipe.recipe_id,
            kind="revisions",
            object_id="v1",
            logical_hash=logical_hash,
            payload=payload,
        )
        encrypted_path = (
            Path(self.temporary.name) / ".recipes-protected" / stored.storage_key
        )
        self.assertNotIn(payload, encrypted_path.read_bytes())
        self.assertEqual(
            self.store.read(
                recipe.recipe_id,
                storage_key=stored.storage_key,
                logical_hash=logical_hash,
                expected_artifact_hash=stored.artifact_hash,
            ),
            payload,
        )

        encrypted = encrypted_path.read_bytes()
        encrypted_path.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
        with self.assertRaises(RecipeIntegrityError):
            self.store.read(
                recipe.recipe_id,
                storage_key=stored.storage_key,
                logical_hash=logical_hash,
                expected_artifact_hash=stored.artifact_hash,
            )
        with self.assertRaises(RecipeIntegrityError):
            self.store.exists("../outside/revisions/payload.ipr")

    def test_publication_recovers_after_payload_write_without_duplicate_revision(self):
        project, recipe = self._project_and_recipe()
        recipe = self.service.hydrate_legacy_project(project, actor=LOCAL_ACTOR)
        operation_id = str(uuid4())

        def crash(stage: str) -> None:
            if stage == "PAYLOAD_WRITTEN":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.service.publish_revision(
                recipe.recipe_id,
                expected_recipe_revision=recipe.optimistic_revision,
                envelope_bytes=self._envelope(recipe.recipe_id, 1),
                actor=LOCAL_ACTOR,
                operation_id=operation_id,
                fault=crash,
            )
        self.assertEqual(
            self.recipe_repository.get_intent(operation_id).state,
            RecipeIntentState.RESERVED,
        )

        recovered = self.service.recover_incomplete(actor=LOCAL_ACTOR)
        self.assertEqual(recovered[0].state, RecipeIntentState.COMPLETE)
        published = self.recipe_repository.get(recipe.recipe_id)
        self.assertEqual(published.current_recipe_revision, 1)
        self.assertEqual(published.optimistic_revision, 3)
        read_back = self.service.read_revision(
            recipe.recipe_id,
            1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(read_back["provenance"]["recipe_id"], recipe.recipe_id)
        with self.recipe_repository._connect(
            self.recipe_repository.registry_path
        ) as connection:
            revisions = connection.execute(
                "SELECT count(*) FROM recipe_revision WHERE recipe_id = ?",
                [recipe.recipe_id],
            ).fetchone()
        self.assertEqual(revisions, (1,))

    def test_publication_rejects_nonportable_runtime_envelopes(self) -> None:
        project, recipe = self._project_and_recipe()
        recipe = self.service.hydrate_legacy_project(project, actor=LOCAL_ACTOR)
        envelope = json.loads(
            self._envelope(recipe.recipe_id, 1).decode("utf-8")
        )

        for mutation in (
            lambda value: value.update({"recipe_contract_version": 3}),
            lambda value: value["recipe"].update(
                {"project_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
            ),
            lambda value: value["recipe"]["source_shape"].update(
                {"portable_note": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
            ),
            lambda value: value["recipe"]["source_shape"].update(
                {"record_id": 42}
            ),
        ):
            invalid = deepcopy(envelope)
            mutation(invalid)
            invalid["semantic_hash"] = content_hash(invalid["recipe"])
            invalid["payload_hash"] = content_hash(
                {
                    key: value
                    for key, value in invalid.items()
                    if key != "payload_hash"
                }
            )
            with self.subTest(mutation=mutation):
                with self.assertRaises(RecipeIntegrityError):
                    self.service.publish_revision(
                        recipe.recipe_id,
                        expected_recipe_revision=recipe.optimistic_revision,
                        envelope_bytes=json.dumps(invalid).encode("utf-8"),
                        actor=LOCAL_ACTOR,
                    )

    def test_data_version_recovery_adopts_workspace_and_seals_predecessor(self):
        first_project, recipe = self._project_and_recipe()
        self._publish_first_revision(recipe.recipe_id)
        recipe = self.recipe_repository.get(recipe.recipe_id)
        second_project = self.project_service.create_project(
            actor=LOCAL_ACTOR,
            name="Customer rollout workspace",
            source_system="CSV export",
        )
        bootstrap = self.recipe_repository.resolve_workspace(second_project.project_id)
        self.projects.get(second_project.project_id)
        operation_id = str(uuid4())

        def crash(stage: str) -> None:
            if stage == "REGISTRY_COMMITTED":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.service.create_data_version(
                recipe.recipe_id,
                expected_recipe_revision=recipe.optimistic_revision,
                workspace_project_id=second_project.project_id,
                purpose=DataVersionPurpose.PRODUCTION,
                label="Rollout export",
                actor=LOCAL_ACTOR,
                operation_id=operation_id,
                fault=crash,
            )
        self.assertEqual(
            self.recipe_repository.get_intent(operation_id).state,
            RecipeIntentState.REGISTRY_COMMITTED,
        )
        self.service.recover_incomplete(actor=LOCAL_ACTOR)

        versions = self.recipe_repository.data_versions(recipe.recipe_id)
        self.assertEqual(
            [(item.version_number, item.state) for item in versions],
            [(1, DataVersionState.SEALED), (2, DataVersionState.ACTIVE)],
        )
        self.assertEqual(versions[1].workspace_project_id, second_project.project_id)
        with self.assertRaises(ProjectError):
            self.projects.assert_workspace_mutable(first_project.project_id)
        self.projects.assert_workspace_mutable(second_project.project_id)
        with self.assertRaises(ProjectNotFoundError):
            self.recipe_repository.resolve_workspace(bootstrap.recipe_id)
        self.assertEqual(len(self.recipe_repository.list()), 1)

    def test_exact_application_qualification_and_cutover_are_separate(self) -> None:
        project, recipe = self._project_and_recipe()
        self._publish_first_revision(recipe.recipe_id)
        recipe = self.recipe_repository.get(recipe.recipe_id)
        data_version = self.recipe_repository.data_versions(recipe.recipe_id)[0]
        application_id = str(uuid4())
        application_hash = "sha256:" + "1" * 64
        target_binding_hash = "sha256:" + "2" * 64
        self.service.record_application_projection(
            actor=LOCAL_ACTOR,
            application_id=application_id,
            recipe_id=recipe.recipe_id,
            recipe_revision=1,
            data_version_id=data_version.data_version_id,
            workspace_project_id=project.project_id,
            source_selection_hash="sha256:" + "3" * 64,
            parameter_values_hash="sha256:" + "4" * 64,
            target_binding_hash=target_binding_hash,
            credential_generation="test-read-generation-3",
            binding_hash="sha256:" + "5" * 64,
            issue_hash="sha256:" + "6" * 64,
            mapping_id=str(uuid4()),
            mapping_content_hash="sha256:" + "7" * 64,
            status="APPLIED",
            evidence_storage_key="protected/application-evidence",
            evidence_hash=application_hash,
            created_at=datetime.now(timezone.utc),
        )
        evidence = {
            "application_evidence_hash": application_hash,
            "application_id": application_id,
            "comparison_hash": "sha256:" + "8" * 64,
            "control_hash": "sha256:" + "9" * 64,
            "environment": "TEST",
            "execution_hash": "sha256:" + "a" * 64,
            "findings": [],
            "preparation_hash": "sha256:" + "b" * 64,
            "quality_hash": "sha256:" + "c" * 64,
            "read_back_hash": "sha256:" + "d" * 64,
            "recipe_revision": 1,
            "reconciliation_hash": "sha256:" + "e" * 64,
            "status": "TEST_QUALIFIED",
            "test_target_binding_hash": target_binding_hash,
        }
        qualification = self.service.publish_qualification(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            evidence=evidence,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(qualification.state, RecipeIntentState.COMPLETE)
        qualification_id = str(qualification.detail["qualification_id"])
        qualification_hash = str(qualification.detail["evidence_hash"])

        recipe = self.recipe_repository.get(recipe.recipe_id)
        cutover = self.service.select_cutover_candidate(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            recipe_revision=1,
            qualification_id=qualification_id,
            qualification_evidence_hash=qualification_hash,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(cutover.state, RecipeIntentState.COMPLETE)
        summary = self.recipe_repository.list()[0]
        self.assertEqual(summary.qualification_status, "TEST_QUALIFIED")
        self.assertEqual(summary.cutover_recipe_revision, 1)
        self.assertNotIn("credential", json.dumps(cutover.detail).casefold())
        self.assertNotIn("production", json.dumps(cutover.detail).casefold())

    def test_deletion_tombstone_persists_only_exact_targets(self) -> None:
        project, recipe = self._project_and_recipe()
        publication = self._publish_first_revision(recipe.recipe_id)
        recipe = self.recipe_repository.get(recipe.recipe_id)
        with self.assertRaises(ProjectError):
            self.projects.assert_standalone_project_deletion_allowed(
                project.project_id
            )
        operation_id = str(uuid4())

        def crash(stage: str) -> None:
            if stage == "INTENT_RESERVED":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.service.begin_deletion(
                recipe.recipe_id,
                expected_recipe_revision=recipe.optimistic_revision,
                actor=LOCAL_ACTOR,
                operation_id=operation_id,
                fault=crash,
            )
        recovered = self.service.recover_incomplete(actor=LOCAL_ACTOR)
        deletion = next(
            item for item in recovered if item.operation_id == operation_id
        )
        self.assertEqual(deletion.state, RecipeIntentState.TARGETS_ENUMERATED)
        self.assertEqual(
            self.recipe_repository.get(recipe.recipe_id).state,
            RecipeState.DELETING,
        )
        targets = self.recipe_repository.deletion_targets(operation_id)
        self.assertIn(("RECIPE", recipe.recipe_id), targets)
        self.assertIn(("PROJECT", project.project_id), targets)
        self.assertIn(
            ("PROTECTED_KEY", str(publication.detail["storage_key"])),
            targets,
        )
        self.assertEqual(targets, self.recipe_repository.deletion_targets(operation_id))


if __name__ == "__main__":
    unittest.main()
