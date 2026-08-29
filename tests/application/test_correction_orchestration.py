"""Verify Phase 2 review orchestration, current pointers, and restart replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from impodo.application.correction_orchestration import (
    CorrectionBinding,
    CorrectionAuthoringStageCoordinator,
    CorrectionNoChangedIntent,
    CorrectionReviewEvidence,
    CorrectionReviewOrchestrator,
    CorrectionSuccessorService,
    CorrectionTargetReviewEvidence,
)
from impodo.domain.correction import (
    CorrectionCandidate,
    CorrectionValueKind,
)
from impodo.domain.correction_origin import ProtectedCorrectionArtifactReference
from impodo.domain.execution.odoo_readback import ReadbackRecord
from impodo.domain.shared.access import Actor, ActorIdentity
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError
from tests.domain.test_correction_origin import HASHES, IDS, _index, _manifest


NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
ACTOR = Actor(
    ActorIdentity("test", "data-manager", "Data manager"),
    frozenset(),
)
SUCCESSOR_RUN_ID = "10000000-0000-4000-8000-000000000000"
SUCCESSOR_WORKSPACE_ID = "10000001-0000-4000-8000-000000000000"
REVIEW_REQUEST_ID = "10000002-0000-4000-8000-000000000000"


class _Bindings:
    def __init__(self, binding: CorrectionBinding) -> None:
        self.binding = binding
        self.invalidations = 0

    def get_for_completed_workspace(self, completed_workspace_id):
        return (
            self.binding
            if self.binding.completed_workspace_id == completed_workspace_id
            else None
        )

    def publish_plan(self, completed_workspace_id, **values):
        if (
            self.binding.current_plan == values["plan"]
            and self.binding.current_mapping_hash == values["mapping_hash"]
            and self.binding.current_prepared_hash == values["prepared_hash"]
        ):
            return self.binding
        self.binding = replace(
            self.binding,
            current_mapping_hash=values["mapping_hash"],
            current_prepared_hash=values["prepared_hash"],
            current_plan=values["plan"],
            current_confirmation=None,
            optimistic_revision=self.binding.optimistic_revision + 1,
        )
        return self.binding

    def attach_successor(self, completed_workspace_id, **values):
        self.binding = replace(
            self.binding,
            successor_migration_run_id=values["successor_migration_run_id"],
            successor_workspace_id=values["successor_workspace_id"],
            optimistic_revision=self.binding.optimistic_revision + 1,
        )
        return self.binding

    def invalidate_plan(self, completed_workspace_id, **values):
        self.invalidations += 1
        self.binding = replace(
            self.binding,
            current_mapping_hash=values["current_mapping_hash"],
            current_prepared_hash=values["current_prepared_hash"],
            current_plan=None,
            current_confirmation=None,
            optimistic_revision=self.binding.optimistic_revision + 1,
        )
        return self.binding


class _Protected:
    def put_plan(self, plan):
        return SimpleNamespace(
            project_id=plan.project_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            storage_key=f"project/correction-plans/{plan.plan_id}.ipe",
            artifact_hash=HASHES[13],
        )


class _Reader:
    scope_hash = HASHES[12]
    imports_external_ids = False

    def __init__(self, target_hash: str, current) -> None:
        self.target_hash = target_hash
        self.current = current
        self.calls = []

    def read_ids(self, model, identifiers, fields):
        self.calls.append((model, tuple(identifiers), tuple(fields)))
        return (
            ReadbackRecord(
                odoo_id=identifiers[0],
                values={fields[0]: self.current},
            ),
        )


class _Pipeline:
    def __init__(self, manifest, *, current=False) -> None:
        self.manifest = manifest
        self.current = current

    def run(self, manifest, successor_workspace_id, *, actor):
        reader = _Reader(manifest.target_hash, self.current)
        return CorrectionReviewEvidence(
            mapping=SimpleNamespace(
                definition=SimpleNamespace(content_hash=HASHES[0])
            ),
            previous_prepared_hash=manifest.prepared_set_hash,
            corrected_prepared_hash=HASHES[1],
            candidate_batches=(
                (
                    CorrectionCandidate(
                        dataset="Products",
                        source_row=1,
                        target_model="product.template",
                        target_field="active",
                        value_kind=CorrectionValueKind.SCALAR,
                        previous=False,
                        corrected=True,
                    ),
                ),
            ),
            reader=reader,
            reader_scope_hash=reader.scope_hash,
            read_credential_binding_hash=HASHES[2],
            read_identity=OdooReadIdentity(
                target_hash=manifest.target_hash,
                principal_hash=HASHES[3],
                permission_hash=HASHES[4],
                context_hash=HASHES[5],
                readable_models=("product.template",),
                observed_at="2026-08-28T04:00:00Z",
            ),
            reviewed_at=NOW,
        )


def _binding(manifest, index):
    return CorrectionBinding(
        correction_binding_id=IDS[10],
        project_id=manifest.project_id,
        data_version_id=manifest.data_version_id,
        completed_migration_run_id=manifest.completed_migration_run_id,
        completed_workspace_id=manifest.completed_workspace_id,
        origin=ProtectedCorrectionArtifactReference(
            artifact_id=manifest.manifest_id,
            logical_hash=manifest.manifest_hash,
            storage_key="project/correction-origins/origin.ipe",
            artifact_hash=HASHES[6],
        ),
        target_index=ProtectedCorrectionArtifactReference(
            artifact_id=index.index_id,
            logical_hash=index.index_hash,
            storage_key="project/correction-target-indexes/index.ipe",
            artifact_hash=HASHES[7],
        ),
        successor_migration_run_id=SUCCESSOR_RUN_ID,
        successor_workspace_id=SUCCESSOR_WORKSPACE_ID,
        current_mapping_hash=None,
        current_prepared_hash=None,
        current_plan=None,
        current_confirmation=None,
        optimistic_revision=2,
        created_at=NOW,
        updated_at=NOW,
    )


class CorrectionReviewOrchestratorTests(unittest.TestCase):
    def test_blocker_free_review_publishes_one_replay_stable_current_plan(self) -> None:
        index = _index()
        manifest = _manifest(index)
        bindings = _Bindings(_binding(manifest, index))
        service = CorrectionReviewOrchestrator(
            bindings=bindings,
            protected=_Protected(),
            pipeline=_Pipeline(manifest),
        )

        review, plan, current = service.review(
            manifest,
            index,
            actor=ACTOR,
            review_request_id=REVIEW_REQUEST_ID,
        )
        replay_review, replay_plan, replay = service.review(
            manifest,
            index,
            actor=ACTOR,
            review_request_id=REVIEW_REQUEST_ID,
        )

        self.assertTrue(review.can_apply)
        self.assertIsNotNone(plan)
        self.assertEqual(replay_review, review)
        self.assertEqual(replay_plan, plan)
        self.assertEqual(replay.optimistic_revision, current.optimistic_revision)
        self.assertEqual(replay.current_plan.logical_hash, plan.plan_hash)

    def test_concurrent_target_change_clears_current_plan_and_blocks_publication(self) -> None:
        index = _index()
        manifest = _manifest(index)
        initial = replace(
            _binding(manifest, index),
            current_mapping_hash=HASHES[8],
            current_prepared_hash=HASHES[9],
            current_plan=ProtectedCorrectionArtifactReference(
                artifact_id="10000003-0000-4000-8000-000000000000",
                logical_hash=HASHES[10],
                storage_key="project/correction-plans/old.ipe",
                artifact_hash=HASHES[11],
            ),
        )
        bindings = _Bindings(initial)
        service = CorrectionReviewOrchestrator(
            bindings=bindings,
            protected=_Protected(),
            pipeline=_Pipeline(manifest, current="manual change"),
        )

        review, plan, current = service.review(
            manifest,
            index,
            actor=ACTOR,
            review_request_id=REVIEW_REQUEST_ID,
        )

        self.assertIsNone(plan)
        self.assertFalse(review.can_apply)
        self.assertEqual(
            tuple(item.code for item in review.blockers),
            ("CONCURRENT_FIELD_CHANGE",),
        )
        self.assertIsNone(current.current_plan)
        self.assertEqual(bindings.invalidations, 1)

    def test_unchanged_rules_return_zero_change_review_without_a_plan(self) -> None:
        index = _index()
        manifest = _manifest(index)
        bindings = _Bindings(_binding(manifest, index))
        pipeline = _Pipeline(manifest)
        original_run = pipeline.run

        def unchanged_run(*args, **kwargs):
            evidence = original_run(*args, **kwargs)
            return replace(
                evidence,
                mapping=SimpleNamespace(
                    definition=SimpleNamespace(
                        content_hash=manifest.mapping_content_hash
                    )
                ),
            )

        pipeline.run = unchanged_run
        service = CorrectionReviewOrchestrator(
            bindings=bindings,
            protected=_Protected(),
            pipeline=pipeline,
        )

        review, plan, current = service.review(
            manifest,
            index,
            actor=ACTOR,
            review_request_id=REVIEW_REQUEST_ID,
        )

        self.assertIsNone(plan)
        self.assertFalse(review.can_apply)
        self.assertEqual(review.fields, ())
        self.assertEqual(review.blockers, ())
        self.assertIsNone(current.current_plan)


class CorrectionSuccessorServiceTests(unittest.TestCase):
    def test_successor_reuses_data_version_and_replays_without_duplicate_roots(self) -> None:
        index = _index()
        manifest = _manifest(index)
        bindings = _Bindings(
            replace(
                _binding(manifest, index),
                successor_migration_run_id=None,
                successor_workspace_id=None,
                optimistic_revision=1,
            )
        )
        project = SimpleNamespace(
            project_id=manifest.project_id,
            optimistic_revision=4,
            source_system_identity="Fixture ERP",
            data_classification=SimpleNamespace(value="INTERNAL"),
            retention_days=365,
        )
        package = SimpleNamespace(
            origin=SimpleNamespace(value="FILE"),
            datasets=(SimpleNamespace(dataset_id="dataset:" + "a" * 24),),
        )

        class Repository:
            def get_project(self, project_id):
                return project

            def get_source_package(self, data_version_id):
                return package

        class Runs:
            repository = Repository()

            def __init__(self):
                self.created = 0
                self.run = None

            def create(self, project_id, **values):
                self.created += 1
                self.asserted_data_version_id = values["data_version_id"]
                self.run = SimpleNamespace(
                    migration_run_id=SUCCESSOR_RUN_ID,
                    data_version_id=values["data_version_id"],
                )
                project.optimistic_revision += 1
                return self.run

            def get(self, run_id, *, actor):
                return self.run

        runs = Runs()

        class Workspaces:
            def __init__(self):
                self.created = 0
                self.workspace = None

            def create(self, project_id, **values):
                self.created += 1
                self.workspace = SimpleNamespace(
                    workspace_id=SUCCESSOR_WORKSPACE_ID,
                    data_version_id=values["data_version_id"],
                    migration_run_id=values["migration_run_id"],
                    display_name=values["display_name"],
                    optimistic_revision=1,
                    setup_state=SimpleNamespace(value="DRAFT"),
                )
                project.optimistic_revision += 1
                return self.workspace

            def get(self, workspace_id, *, actor):
                return self.workspace

            def complete_setup(self, workspace_id, **values):
                self.workspace.setup_state = SimpleNamespace(value="READY")
                self.workspace.optimistic_revision += 1
                return self.workspace

        workspaces = Workspaces()

        class WorkspaceStateRepository:
            def get(self, workspace_id):
                raise WorkspaceStateNotFoundError("missing")

        class WorkspaceStates:
            repository = WorkspaceStateRepository()

            def __init__(self):
                self.provisioned = 0

            def provision_migration_workspace(self, workspace_id, **values):
                self.provisioned += 1

        workspace_states = WorkspaceStates()

        class Projections:
            def __init__(self):
                self.calls = []

            def materialize(self, workspace_id, **values):
                self.calls.append((workspace_id, values["dataset_ids"]))
                workspaces.workspace.optimistic_revision += 1

        projections = Projections()
        prior_target = SimpleNamespace(
            connection_mode="REMOTE",
            base_url="https://odoo.example.test",
            database="fixture",
            intended_applications=("Sales",),
        )

        class Targets:
            def __init__(self):
                self.replaced = 0

            def get(self, run_id, *, actor):
                return prior_target

            def replace(self, run_id, **values):
                self.replaced += 1

        targets = Targets()

        class Seeder:
            def __init__(self):
                self.calls = []

            def seed(self, completed_workspace_id, successor_workspace_id, **values):
                self.calls.append((completed_workspace_id, successor_workspace_id))

        seeder = Seeder()
        service = CorrectionSuccessorService(
            bindings=bindings,
            runs=runs,
            workspaces=workspaces,
            workspace_states=workspace_states,
            source_projections=projections,
            target_setups=targets,
            mapping_seeder=seeder,
        )

        successor = service.start(
            manifest.completed_workspace_id,
            actor=ACTOR,
            request_id=REVIEW_REQUEST_ID,
        )
        replay = service.start(
            manifest.completed_workspace_id,
            actor=ACTOR,
            request_id=REVIEW_REQUEST_ID,
        )

        self.assertEqual(runs.asserted_data_version_id, manifest.data_version_id)
        self.assertEqual(successor.workspace.data_version_id, manifest.data_version_id)
        self.assertEqual(successor.binding.successor_workspace_id, SUCCESSOR_WORKSPACE_ID)
        self.assertEqual(replay.binding, successor.binding)
        self.assertEqual(runs.created, 1)
        self.assertEqual(workspaces.created, 1)
        self.assertEqual(workspace_states.provisioned, 1)
        self.assertEqual(len(projections.calls), 1)
        self.assertEqual(targets.replaced, 1)
        self.assertEqual(
            seeder.calls,
            [(manifest.completed_workspace_id, SUCCESSOR_WORKSPACE_ID)],
        )

    def test_authoring_review_stages_run_in_safe_owner_order(self) -> None:
        manifest = _manifest(_index())
        calls = []
        mapping = SimpleNamespace(
            mapping_id="mapping",
            definition=SimpleNamespace(content_hash=HASHES[0]),
        )

        class Mapping:
            def validate_and_submit(self, *args, **kwargs):
                calls.append("mapping")
                return mapping

        class Preparation:
            def prepare_native(self, *args, **kwargs):
                calls.append("preparation")
                return ()

        class Quality:
            def require_current_quality(self, *args, **kwargs):
                calls.append("quality")

        reader = _Reader(manifest.target_hash, False)

        class Target:
            def refresh_read_capability(self, *args, **kwargs):
                calls.append("target")
                return CorrectionTargetReviewEvidence(
                    reader=reader,
                    reader_scope_hash=reader.scope_hash,
                    read_credential_binding_hash=HASHES[0],
                    read_identity=OdooReadIdentity(
                        target_hash=manifest.target_hash,
                        principal_hash=HASHES[1],
                        permission_hash=HASHES[2],
                        context_hash=HASHES[3],
                        readable_models=("product.template",),
                        observed_at="2026-08-28T04:00:00Z",
                    ),
                    reviewed_at=NOW,
                )

        result = CorrectionAuthoringStageCoordinator(
            mapping=Mapping(),
            preparation=Preparation(),
            quality=Quality(),
            target=Target(),
        ).prepare_review(
            manifest,
            SUCCESSOR_WORKSPACE_ID,
            actor=ACTOR,
        )

        self.assertEqual(calls, ["mapping", "preparation", "quality", "target"])
        self.assertIs(result.mapping, mapping)

    def test_unchanged_rules_stop_before_preparation_and_odoo_read(self) -> None:
        manifest = _manifest(_index())
        calls = []
        mapping = SimpleNamespace(
            definition=SimpleNamespace(
                content_hash=manifest.mapping_content_hash
            )
        )

        class Mapping:
            def validate_and_submit(self, *args, **kwargs):
                calls.append("mapping")
                return mapping

        class UnexpectedStage:
            def __getattr__(self, name):
                def unexpected(*args, **kwargs):
                    calls.append(name)
                    self.fail("No later review stage should run")

                return unexpected

        coordinator = CorrectionAuthoringStageCoordinator(
            mapping=Mapping(),
            preparation=UnexpectedStage(),
            quality=UnexpectedStage(),
            target=UnexpectedStage(),
        )

        with self.assertRaises(CorrectionNoChangedIntent):
            coordinator.prepare_review(
                manifest,
                SUCCESSOR_WORKSPACE_ID,
                actor=ACTOR,
            )

        self.assertEqual(calls, ["mapping"])


if __name__ == "__main__":
    unittest.main()
