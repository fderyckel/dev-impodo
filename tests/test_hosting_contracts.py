"""Contract tests for the local-to-hosted architecture boundaries."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from uuid import uuid4

from impodo.access import (
    Actor,
    ActorIdentity,
    Capability,
)
from impodo.approvals import ExportPlanApproval, FrozenExportPlan
from impodo.artifacts import (
    ArtifactStoreError,
    LocalArtifactStore,
)
from impodo.jobs import (
    InlineJobDispatcher,
    JobKind,
    JobRequest,
    JobStatus,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64
HASH_F = "sha256:" + "f" * 64


def actor(name: str, *capabilities: Capability) -> Actor:
    return Actor(
        identity=ActorIdentity(
            issuer="https://identity.example.test",
            subject_id=name.casefold().replace(" ", "-"),
            display_name=name,
        ),
        capabilities=frozenset(capabilities),
    )


class ArtifactStoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.store = LocalArtifactStore(self.temporary.name)
        self.project_id = str(uuid4())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_round_trip_uses_storage_key_not_repository_path(self) -> None:
        validated: list[bytes] = []
        stored = self.store.store_source(
            self.project_id,
            artifact_id=str(uuid4()),
            suffix=".csv",
            stream=BytesIO(b"code,name\nC1,Example\n"),
            maximum_bytes=1_024,
            chunk_bytes=8,
            validator=lambda path: validated.append(path.read_bytes()),
        )

        self.assertEqual(validated, [b"code,name\nC1,Example\n"])
        self.assertFalse(Path(stored.storage_key).is_absolute())
        with self.store.materialize_source(
            self.project_id,
            stored.storage_key,
        ) as materialized:
            self.assertEqual(materialized.read_bytes(), validated[0])

        self.store.delete_source(self.project_id, stored.storage_key)
        with self.assertRaises(ArtifactStoreError):
            with self.store.materialize_source(
                self.project_id,
                stored.storage_key,
            ):
                pass

    def test_source_storage_rejects_traversal_keys(self) -> None:
        with self.assertRaises(ArtifactStoreError):
            with self.store.materialize_source(
                self.project_id,
                "../source.csv",
            ):
                pass


class JobContractTests(unittest.TestCase):
    def test_concurrent_duplicate_dispatch_executes_work_once(self) -> None:
        dispatcher = InlineJobDispatcher()
        requester = actor("Job requester", Capability.SOURCE_INSPECT)
        request = JobRequest(
            job_id=str(uuid4()),
            project_id=str(uuid4()),
            kind=JobKind.SOURCE_INSPECTION,
            idempotency_key="inspect:project-revision-4",
            input_hash=HASH_A,
            requested_by=requester.identity,
            requested_at=NOW,
        )
        started = Event()
        release = Event()
        executions: list[str] = []
        completed: list[object] = []

        def work() -> tuple[str, ...]:
            executions.append("run")
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return ("catalog-artifact",)

        thread = Thread(
            target=lambda: completed.append(dispatcher.dispatch(request, work)),
            daemon=True,
        )
        thread.start()
        self.assertTrue(started.wait(timeout=5))

        duplicate = dispatcher.dispatch(
            request,
            lambda: ("must-not-run",),
        )
        self.assertEqual(duplicate.status, JobStatus.RUNNING)
        release.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(executions, ["run"])
        final = dispatcher.get(request.job_id)
        self.assertEqual(final.status, JobStatus.SUCCEEDED)
        self.assertEqual(final.result_artifact_ids, ("catalog-artifact",))

        conflicting = replace(request, job_id=str(uuid4()), input_hash=HASH_B)
        with self.assertRaisesRegex(ValueError, "different job request"):
            dispatcher.dispatch(conflicting, lambda: ())


class ExportApprovalContractTests(unittest.TestCase):
    def make_plan(self) -> FrozenExportPlan:
        return FrozenExportPlan(
            plan_id="plan-001",
            project_id=str(uuid4()),
            run_id="run-001",
            source_hashes={"products.xlsx": HASH_A},
            mapping_hash=HASH_B,
            ruleset_hash=HASH_C,
            canonical_dataset_hash=HASH_D,
            target_snapshot_hash=HASH_E,
            actions_hash=HASH_F,
            frozen_at=NOW,
        )

    def test_only_key_user_can_approve_exact_unexpired_plan(self) -> None:
        plan = self.make_plan()
        normalizer = actor("Normalizer", Capability.NORMALIZATION_DECIDE)
        approver = actor("Key approver", Capability.EXPORT_PLAN_APPROVE)

        with self.assertRaisesRegex(PermissionError, "export_plan.approve"):
            ExportPlanApproval.approve(
                plan,
                approval_id="approval-001",
                actor=normalizer,
                approved_at=NOW,
                policy_version="policy-1",
            )

        approval = ExportPlanApproval.approve(
            plan,
            approval_id="approval-001",
            actor=approver,
            approved_at=NOW,
            policy_version="policy-1",
            expires_at=NOW + timedelta(hours=4),
            reason="Approved for the governed target rehearsal",
        )

        self.assertTrue(approval.authorizes(plan, at=NOW + timedelta(hours=1)))
        self.assertFalse(approval.authorizes(plan, at=NOW + timedelta(hours=5)))
        changed_plan = replace(plan, actions_hash=HASH_A)
        self.assertFalse(approval.authorizes(changed_plan, at=NOW))
        with self.assertRaises(FrozenInstanceError):
            approval.plan_hash = changed_plan.semantic_hash  # type: ignore[misc]
        with self.assertRaises(TypeError):
            plan.source_hashes["other.csv"] = HASH_A  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()

