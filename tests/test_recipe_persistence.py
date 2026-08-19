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

import duckdb

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.recipe_repository import RecipeRepository
from impodo.adapters.duckdb.schema.registry import (
    RECIPE_CLEAN_ROOT_MIGRATION_ID,
    RECIPE_REGISTRY_MIGRATION_ID,
)
from impodo.adapters.protected_recipe_store import ProtectedRecipeStore
from impodo.application.recipe_service import RecipeService
from impodo.domain.serialization import content_hash
from impodo.projects import ProjectError, ProjectNotFoundError, ProjectService
from impodo.recipes import (
    DataVersionPurpose,
    DataVersionState,
    RecipeConflictError,
    RecipeIdentifierConfusionError,
    RecipeIntegrityError,
    RecipeIntentState,
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

    def _publish_first_revision(self, recipe_id: str):
        recipe = self.recipe_repository.get(recipe_id)
        return self.service.publish_revision(
            recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            envelope_bytes=self._envelope(recipe_id, 1),
            actor=LOCAL_ACTOR,
        )

    def _unlinked_workspace(self, name: str):
        return self.project_service.create_data_version_workspace(
            actor=LOCAL_ACTOR,
            name=name,
            source_system="CSV export",
            data_manager="Data Manager",
            functional_owner="Functional Owner",
            business_unit="Operations",
            data_classification="INTERNAL",
            retention_days=90,
            support_access=False,
        )

    def test_clean_root_migration_removes_bootstrap_state_and_cutover_uniqueness(
        self,
    ) -> None:
        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        registry_path = Path(self.temporary.name) / "registry.duckdb"
        recipe_id = str(uuid4())
        data_version_id = str(uuid4())
        project_id = str(uuid4())
        first_cutover_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with duckdb.connect(str(registry_path)) as connection:
            connection.execute(
                """
                CREATE TABLE recipe (
                    recipe_id VARCHAR PRIMARY KEY,
                    display_name VARCHAR NOT NULL,
                    business_purpose VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    data_classification VARCHAR NOT NULL,
                    retention_days INTEGER NOT NULL,
                    current_recipe_revision INTEGER,
                    current_data_version_id VARCHAR,
                    pending_data_version_id VARCHAR,
                    cutover_candidate_id VARCHAR,
                    setup_hydration_state VARCHAR NOT NULL,
                    setup_hydration_hash VARCHAR,
                    optimistic_revision INTEGER NOT NULL,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE data_version (
                    data_version_id VARCHAR PRIMARY KEY,
                    recipe_id VARCHAR NOT NULL,
                    version_number INTEGER NOT NULL,
                    workspace_project_id VARCHAR NOT NULL UNIQUE,
                    parent_data_version_id VARCHAR,
                    purpose VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    pinned_recipe_revision INTEGER,
                    label VARCHAR NOT NULL,
                    export_as_of_date VARCHAR,
                    parameter_values_hash VARCHAR,
                    intake_status VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    sealed_at VARCHAR,
                    UNIQUE (recipe_id, version_number)
                );
                CREATE TABLE cutover_candidate (
                    cutover_candidate_id VARCHAR PRIMARY KEY,
                    recipe_id VARCHAR NOT NULL UNIQUE,
                    recipe_revision INTEGER NOT NULL,
                    qualification_id VARCHAR NOT NULL,
                    expected_recipe_revision INTEGER NOT NULL,
                    actor_issuer VARCHAR NOT NULL,
                    actor_subject VARCHAR NOT NULL,
                    actor_display_name VARCHAR NOT NULL,
                    selected_at VARCHAR NOT NULL,
                    content_hash VARCHAR NOT NULL
                );
                CREATE TABLE recipe_intent (
                    operation_id VARCHAR PRIMARY KEY,
                    recipe_id VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    expected_recipe_revision INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL,
                    detail_json VARCHAR NOT NULL,
                    last_error VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO recipe VALUES (?, 'Legacy', 'Legacy', 'DELETING', "
                "'INTERNAL', 90, 1, ?, NULL, ?, 'READY', NULL, 4, ?, ?)",
                [recipe_id, data_version_id, first_cutover_id, now, now],
            )
            connection.execute(
                "INSERT INTO data_version VALUES (?, ?, 1, ?, NULL, 'AUTHORING', "
                "'PENDING', 1, 'Legacy', NULL, NULL, 'LEGACY_BACKFILL', ?, NULL)",
                [data_version_id, recipe_id, project_id, now],
            )
            connection.execute(
                "INSERT INTO cutover_candidate VALUES (?, ?, 1, ?, 3, "
                "'issuer', 'subject', 'Actor', ?, ?)",
                [
                    first_cutover_id,
                    recipe_id,
                    str(uuid4()),
                    now,
                    "sha256:" + "a" * 64,
                ],
            )

        migrated = DuckDbDatabase(self.temporary.name)
        with migrated._connect(migrated.registry_path) as connection:
            recipe_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info('recipe')").fetchall()
            }
            data_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('data_version')"
                ).fetchall()
            }
            intent_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('recipe_intent')"
                ).fetchall()
            }
            data_state = connection.execute(
                "SELECT state FROM data_version WHERE data_version_id = ?",
                [data_version_id],
            ).fetchone()
            connection.execute(
                "INSERT INTO cutover_candidate VALUES (?, ?, 2, ?, 4, "
                "'issuer', 'subject', 'Actor', ?, ?)",
                [
                    str(uuid4()),
                    recipe_id,
                    str(uuid4()),
                    now,
                    "sha256:" + "b" * 64,
                ],
            )
            history_count = connection.execute(
                "SELECT count(*) FROM cutover_candidate WHERE recipe_id = ?",
                [recipe_id],
            ).fetchone()
            migration = connection.execute(
                "SELECT count(*) FROM registry_schema_migration "
                "WHERE migration_id = ?",
                [RECIPE_CLEAN_ROOT_MIGRATION_ID],
            ).fetchone()

        self.assertNotIn("pending_data_version_id", recipe_columns)
        self.assertNotIn("setup_hydration_state", recipe_columns)
        self.assertNotIn("state", recipe_columns)
        self.assertNotIn("intake_status", data_columns)
        self.assertNotIn("retry_count", intent_columns)
        self.assertEqual(data_state, ("SEALED",))
        self.assertEqual(history_count, (2,))
        self.assertEqual(migration, (1,))

    def test_native_recipe_registry_is_bounded_and_ids_are_not_interchangeable(self) -> None:
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
        _project, recipe = self._project_and_recipe()
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
        self.assertEqual(published.optimistic_revision, 2)
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
        with self.assertRaisesRegex(RecipeConflictError, "already"):
            self.service.publish_revision(
                recipe.recipe_id,
                expected_recipe_revision=published.optimistic_revision,
                envelope_bytes=self._envelope(recipe.recipe_id, 2),
                actor=LOCAL_ACTOR,
            )

    def test_publication_rejects_nonportable_runtime_envelopes(self) -> None:
        _project, recipe = self._project_and_recipe()
        envelope = json.loads(self._envelope(recipe.recipe_id, 1).decode("utf-8"))

        for mutation in (
            lambda value: value.update({"recipe_contract_version": 3}),
            lambda value: value["recipe"].update(
                {"project_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
            ),
            lambda value: value["recipe"]["source_shape"].update(
                {"portable_note": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
            ),
            lambda value: value["recipe"]["source_shape"].update({"record_id": 42}),
        ):
            invalid = deepcopy(envelope)
            mutation(invalid)
            invalid["semantic_hash"] = content_hash(invalid["recipe"])
            invalid["payload_hash"] = content_hash(
                {key: value for key, value in invalid.items() if key != "payload_hash"}
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
        second_project = self._unlinked_workspace("Customer rollout workspace")
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
        self.assertEqual(len(self.recipe_repository.list()), 1)

    def test_restart_preserves_reserved_workspace_and_discards_true_orphan(self):
        _first_project, recipe = self._project_and_recipe()
        self._publish_first_revision(recipe.recipe_id)
        recipe = self.recipe_repository.get(recipe.recipe_id)
        recoverable = self._unlinked_workspace("Reserved rollout workspace")
        orphan = self._unlinked_workspace("Interrupted provisional workspace")
        operation_id = str(uuid4())

        def crash(stage: str) -> None:
            if stage == "INTENT_RESERVED":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.service.create_data_version(
                recipe.recipe_id,
                expected_recipe_revision=recipe.optimistic_revision,
                workspace_project_id=recoverable.project_id,
                purpose=DataVersionPurpose.TEST,
                label="Reserved rollout workspace",
                actor=LOCAL_ACTOR,
                operation_id=operation_id,
                fault=crash,
            )

        restarted_projects = ProjectRepository(self.database)
        self.assertTrue(
            restarted_projects.project_directory(recoverable.project_id).is_dir()
        )
        self.assertFalse(
            restarted_projects.project_directory(orphan.project_id).exists()
        )

        restarted_repository = RecipeRepository(self.database)
        restarted_service = RecipeService(
            restarted_repository,
            self.store,
            CapabilityAuthorizationPolicy(),
        )
        recovered = restarted_service.recover_incomplete(actor=LOCAL_ACTOR)

        self.assertEqual(recovered[0].state, RecipeIntentState.COMPLETE)
        versions = restarted_repository.data_versions(recipe.recipe_id)
        self.assertEqual(versions[-1].workspace_project_id, recoverable.project_id)
        with self.assertRaises(ProjectNotFoundError):
            restarted_projects.get(orphan.project_id)

    def test_failed_data_version_commit_is_abandoned_before_orphan_cleanup(self):
        _first_project, recipe = self._project_and_recipe()
        workspace = self._unlinked_workspace("Rejected rollout workspace")
        operation_id = str(uuid4())

        with (
            patch.object(
                self.recipe_repository,
                "commit_data_version",
                side_effect=RecipeConflictError("injected registry conflict"),
            ),
            self.assertRaisesRegex(RecipeConflictError, "registry conflict"),
        ):
            self.service.create_data_version(
                recipe.recipe_id,
                expected_recipe_revision=recipe.optimistic_revision,
                workspace_project_id=workspace.project_id,
                purpose=DataVersionPurpose.TEST,
                label="Rejected rollout workspace",
                actor=LOCAL_ACTOR,
                operation_id=operation_id,
            )

        self.assertEqual(
            self.recipe_repository.get_intent(operation_id).state,
            RecipeIntentState.ABANDONED,
        )
        restarted_projects = ProjectRepository(self.database)
        self.assertFalse(
            restarted_projects.project_directory(workspace.project_id).exists()
        )

    def test_active_data_version_parameter_hash_updates_optimistically(self):
        _first_project, recipe = self._project_and_recipe()
        self._publish_first_revision(recipe.recipe_id)
        recipe = self.recipe_repository.get(recipe.recipe_id)
        workspace = self._unlinked_workspace("Customer parameter rehearsal")
        first_hash = "sha256:" + "1" * 64
        second_hash = "sha256:" + "2" * 64
        self.service.create_data_version(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            workspace_project_id=workspace.project_id,
            purpose=DataVersionPurpose.TEST,
            label="Customer parameter rehearsal",
            parameter_values_hash=first_hash,
            actor=LOCAL_ACTOR,
        )
        data_version = self.recipe_repository.data_versions(recipe.recipe_id)[-1]

        updated = self.service.update_data_version_parameter_values_hash(
            recipe.recipe_id,
            data_version.data_version_id,
            expected_hash=first_hash,
            parameter_values_hash=second_hash,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(updated.parameter_values_hash, second_hash)
        with self.assertRaises(RecipeConflictError):
            self.service.update_data_version_parameter_values_hash(
                recipe.recipe_id,
                data_version.data_version_id,
                expected_hash=first_hash,
                parameter_values_hash="sha256:" + "3" * 64,
                actor=LOCAL_ACTOR,
            )

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
            "data_version_id": data_version.data_version_id,
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
            "workspace_project_id": project.project_id,
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

        next_envelope = json.loads(self._envelope(recipe.recipe_id, 2).decode("utf-8"))
        next_envelope["recipe"]["parameter_definitions"]["parameters"][0]["label"] = (
            "Rollout batch reference"
        )
        next_envelope["semantic_hash"] = content_hash(next_envelope["recipe"])
        next_envelope["payload_hash"] = content_hash(
            {
                key: value
                for key, value in next_envelope.items()
                if key != "payload_hash"
            }
        )
        selected = self.recipe_repository.get(recipe.recipe_id)
        self.service.publish_revision(
            recipe.recipe_id,
            expected_recipe_revision=selected.optimistic_revision,
            envelope_bytes=json.dumps(next_envelope).encode("utf-8"),
            actor=LOCAL_ACTOR,
        )
        summary = self.recipe_repository.list()[0]
        self.assertIsNone(summary.qualification_status)
        self.assertIsNone(
            self.service.current_qualification(recipe.recipe_id, actor=LOCAL_ACTOR)
        )
        self.assertEqual(
            len(self.service.qualifications(recipe.recipe_id, actor=LOCAL_ACTOR)), 1
        )
        self.assertEqual(summary.cutover_recipe_revision, 1)

        recipe = self.recipe_repository.get(recipe.recipe_id)
        second_application_id = str(uuid4())
        second_application_hash = "sha256:" + "f" * 64
        self.service.record_application_projection(
            actor=LOCAL_ACTOR,
            application_id=second_application_id,
            recipe_id=recipe.recipe_id,
            recipe_revision=2,
            data_version_id=data_version.data_version_id,
            workspace_project_id=project.project_id,
            source_selection_hash="sha256:" + "3" * 64,
            parameter_values_hash="sha256:" + "4" * 64,
            target_binding_hash=target_binding_hash,
            credential_generation="test-read-generation-4",
            binding_hash="sha256:" + "5" * 64,
            issue_hash="sha256:" + "6" * 64,
            mapping_id=str(uuid4()),
            mapping_content_hash="sha256:" + "7" * 64,
            status="APPLIED",
            evidence_storage_key="protected/application-evidence-v2",
            evidence_hash=second_application_hash,
            created_at=datetime.now(timezone.utc),
        )
        second_evidence = {
            **evidence,
            "application_evidence_hash": second_application_hash,
            "application_id": second_application_id,
            "recipe_revision": 2,
        }
        second_qualification = self.service.publish_qualification(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            evidence=second_evidence,
            actor=LOCAL_ACTOR,
        )
        recipe = self.recipe_repository.get(recipe.recipe_id)
        self.service.select_cutover_candidate(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            recipe_revision=2,
            qualification_id=str(second_qualification.detail["qualification_id"]),
            qualification_evidence_hash=str(
                second_qualification.detail["evidence_hash"]
            ),
            actor=LOCAL_ACTOR,
        )
        current = self.service.cutover_candidate(recipe.recipe_id, actor=LOCAL_ACTOR)
        self.assertIsNotNone(current)
        self.assertEqual(current.recipe_revision, 2)
        with self.recipe_repository._connect(
            self.recipe_repository.registry_path
        ) as connection:
            history_count = connection.execute(
                "SELECT count(*) FROM cutover_candidate WHERE recipe_id = ?",
                [recipe.recipe_id],
            ).fetchone()
        self.assertEqual(history_count, (2,))

    def test_draft_deletion_is_recipe_owned_and_published_recipe_is_protected(self) -> None:
        project, recipe = self._project_and_recipe()
        deleted_project_id = self.service.delete_draft(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            expected_workspace_revision=project.revision,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(deleted_project_id, project.project_id)
        with self.assertRaises(ProjectNotFoundError):
            self.projects.get(project.project_id)

        _published_project, published = self._project_and_recipe("Published")
        self._publish_first_revision(published.recipe_id)
        published = self.recipe_repository.get(published.recipe_id)
        with self.assertRaisesRegex(RecipeConflictError, "unpublished"):
            self.service.delete_draft(
                published.recipe_id,
                expected_recipe_revision=published.optimistic_revision,
                expected_workspace_revision=1,
                actor=LOCAL_ACTOR,
            )


if __name__ == "__main__":
    unittest.main()
