from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.invalidation import EvidenceInvalidationMixin
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.derived_value_artifact import (
    DerivedValueArtifact,
    DerivedValueInput,
    DerivedValueKind,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.preparation_session import (
    PreparationSessionBindings,
    PreparationSessionStatus,
)
from impodo.domain.staging.transformation_impact import TransformationImpactReport
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode, WorkspaceStatus
from impodo.domain.preparation.staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    StagingDatasetRole,
)
from impodo.domain.workspace.errors import WorkspaceError


ROOT = REPOSITORY_ROOT
NOW = datetime(2026, 8, 18, 16, 0, tzinfo=timezone.utc)
HASHES = tuple(
    f"sha256:{sha256(str(index).encode('ascii')).hexdigest()}"
    for index in range(16)
)
PHYSICAL_SELECTION_HASH = HASHES[0]
SOURCE_SELECTION_HASH = HASHES[1]
MAPPING_HASH = HASHES[2]
SCHEMA_HASH = HASHES[3]
DERIVED_PLAN_HASH = HASHES[4]
COMPILED_PLAN_HASH = HASHES[5]
DATASET_ID = "structural:01234567-89ab-cdef-0123-456789abcdef"
DOWNSTREAM_DATASET_ID = "structural:fedcba98-7654-3210-fedc-ba9876543210"
INPUT_A_ID = "dataset:" + "a" * 24
INPUT_B_ID = "dataset:" + "b" * 24


class DerivedValueArtifactRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(self.database)
        self.repository = PreparationSessionRepository(self.database)
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Derived artifact repository",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("product.template",),
            status=WorkspaceStatus.REGISTERED,
            registered_at=NOW,
        )
        self.workspace_states.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
        self.bindings = PreparationSessionBindings(
            mapping_id="mapping:products",
            mapping_version=1,
            physical_selection_hash=PHYSICAL_SELECTION_HASH,
            source_selection_hash=SOURCE_SELECTION_HASH,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            derived_plan_hash=DERIVED_PLAN_HASH,
            compiled_plan_hash=COMPILED_PLAN_HASH,
            contract_version=STAGING_CONTRACT_VERSION,
            evaluator_version=BROWSER_EVALUATOR_VERSION,
            source_hashes={"products": HASHES[6], "bom": HASHES[7]},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_and_binding_advance_only_with_published_session(self) -> None:
        artifact = _artifact(self.workspace_state.workspace_id)
        session = self._begin()

        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            session.session_id,
            artifact,
        )

        self.assertEqual(
            self.repository.find_derived_value_artifact(
                self.workspace_state.workspace_id,
                artifact.dataset_id,
                artifact.logical_hash,
            ),
            artifact,
        )
        self.assertEqual(
            self.repository.session_derived_value_artifacts(
                self.workspace_state.workspace_id,
                session.session_id,
            ),
            (artifact,),
        )
        self.assertEqual(
            self.repository.current_derived_value_artifacts(
                self.workspace_state.workspace_id
            ),
            (),
        )
        self.assertEqual(
            self.repository.derived_value_artifact_storage_keys(
                self.workspace_state.workspace_id
            ),
            frozenset((artifact.parquet_storage_key,)),
        )

        self._finalize_and_publish(session.session_id)

        self.assertEqual(
            self.repository.current_derived_value_artifacts(
                self.workspace_state.workspace_id
            ),
            (artifact,),
        )
        self.assertEqual(
            self.repository.session_derived_value_artifacts(
                self.workspace_state.workspace_id,
                session.session_id,
            ),
            (),
        )

    def test_failed_session_keeps_manifest_without_advancing_current(self) -> None:
        first = _artifact(self.workspace_state.workspace_id)
        published = self._begin()
        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            published.session_id,
            first,
        )
        self._finalize_and_publish(published.session_id)

        second = _artifact(
            self.workspace_state.workspace_id,
            derivation_rule_hash=HASHES[14],
            parquet_sha256=HASHES[15],
        )
        failed = self._begin()
        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            failed.session_id,
            second,
        )
        self.repository.fail_session(
            self.workspace_state.workspace_id,
            failed.session_id,
            "DERIVED_PUBLICATION_FAILED",
        )

        self.assertEqual(
            self.repository.current_derived_value_artifacts(
                self.workspace_state.workspace_id
            ),
            (first,),
        )
        self.assertEqual(
            self.repository.session_derived_value_artifacts(
                self.workspace_state.workspace_id,
                failed.session_id,
            ),
            (),
        )
        self.assertEqual(
            self.repository.find_derived_value_artifact(
                self.workspace_state.workspace_id,
                second.dataset_id,
                second.logical_hash,
            ),
            second,
        )
        self.assertEqual(
            self.repository.derived_value_artifact_storage_keys(
                self.workspace_state.workspace_id
            ),
            frozenset(
                (first.parquet_storage_key, second.parquet_storage_key)
            ),
        )

    def test_binding_mismatch_rolls_back_manifest_registration(self) -> None:
        session = self._begin()
        mismatches = (
            {"physical_selection_hash": HASHES[10]},
            {"source_selection_hash": HASHES[10]},
            {"mapping_hash": HASHES[10]},
            {"schema_hash": HASHES[10]},
            {"derived_plan_hash": HASHES[10]},
        )
        for overrides in mismatches:
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(WorkspaceError, "does not match"),
            ):
                self.repository.bind_derived_value_artifact(
                    self.workspace_state.workspace_id,
                    session.session_id,
                    _artifact(self.workspace_state.workspace_id, **overrides),
                )

        with self.assertRaisesRegex(WorkspaceError, "another workspace"):
            self.repository.bind_derived_value_artifact(
                self.workspace_state.workspace_id,
                session.session_id,
                _artifact(str(uuid4())),
            )
        with self.assertRaisesRegex(WorkspaceError, "input evidence"):
            self.repository.bind_derived_value_artifact(
                self.workspace_state.workspace_id,
                session.session_id,
                _artifact(
                    self.workspace_state.workspace_id,
                    input_evidence=(
                        DerivedValueInput(INPUT_A_ID, HASHES[15]),
                    ),
                ),
            )
        self.assertEqual(
            self.repository.derived_value_artifact_storage_keys(
                self.workspace_state.workspace_id
            ),
            frozenset(),
        )
        self.assertEqual(
            self.repository.session_derived_value_artifacts(
                self.workspace_state.workspace_id,
                session.session_id,
            ),
            (),
        )

    def test_missing_bound_manifest_blocks_current_promotion(self) -> None:
        artifact = _artifact(self.workspace_state.workspace_id)
        session = self._begin()
        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            session.session_id,
            artifact,
        )
        self._finalize(session.session_id)
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute(
                "DELETE FROM derived_value_artifact_manifest "
                "WHERE content_hash = ?",
                [artifact.content_hash],
            )

        with self.assertRaisesRegex(WorkspaceError, "manifest is missing"):
            self.repository.mark_published(
                self.workspace_state.workspace_id,
                session.session_id,
            )

        self.assertEqual(
            self.repository.get_session(
                self.workspace_state.workspace_id,
                session.session_id,
            ).status,
            PreparationSessionStatus.READY,
        )
        self.assertEqual(
            self.repository.current_derived_value_artifacts(
                self.workspace_state.workspace_id
            ),
            (),
        )

    def test_non_building_session_rejects_new_artifact_binding(self) -> None:
        session = self._begin()
        self._finalize(session.session_id)

        with self.assertRaisesRegex(WorkspaceError, "wrong state"):
            self.repository.bind_derived_value_artifact(
                self.workspace_state.workspace_id,
                session.session_id,
                _artifact(self.workspace_state.workspace_id),
            )
        self.assertEqual(
            self.repository.derived_value_artifact_storage_keys(
                self.workspace_state.workspace_id
            ),
            frozenset(),
        )

    def test_upstream_derived_input_must_be_bound_before_its_consumer(self) -> None:
        session = self._begin()
        upstream = _artifact(self.workspace_state.workspace_id)
        downstream = _artifact(
            self.workspace_state.workspace_id,
            dataset_id=DOWNSTREAM_DATASET_ID,
            dataset_name="Grouped Products",
            derivation_kind=DerivedValueKind.GROUP,
            input_evidence=(
                DerivedValueInput(upstream.dataset_id, upstream.content_hash),
            ),
            derivation_rule_hash=HASHES[14],
            parquet_sha256=HASHES[15],
        )

        with self.assertRaisesRegex(WorkspaceError, "input evidence"):
            self.repository.bind_derived_value_artifact(
                self.workspace_state.workspace_id,
                session.session_id,
                downstream,
            )
        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            session.session_id,
            upstream,
        )
        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            session.session_id,
            downstream,
        )

        self.assertEqual(
            self.repository.session_derived_value_artifacts(
                self.workspace_state.workspace_id,
                session.session_id,
            ),
            (upstream, downstream),
        )
        self._finalize(session.session_id)
        statements: list[str] = []
        original_connect = self.repository._connect

        class CountingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, *args, **kwargs):
                statements.append(str(statement))
                return self.connection.execute(statement, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def counting_connect(path):
            with original_connect(path) as connection:
                yield CountingConnection(connection)

        with patch.object(self.repository, "_connect", counting_connect):
            self.repository.mark_published(
                self.workspace_state.workspace_id,
                session.session_id,
            )

        self.assertEqual(
            sum(
                "INSERT OR REPLACE INTO derived_value_artifact_current"
                in statement
                for statement in statements
            ),
            1,
        )
        self.assertEqual(
            self.repository.current_derived_value_artifacts(
                self.workspace_state.workspace_id
            ),
            (upstream, downstream),
        )

    def test_staging_invalidation_clears_current_but_retains_history(self) -> None:
        artifact = _artifact(self.workspace_state.workspace_id)
        session = self._begin()
        self.repository.bind_derived_value_artifact(
            self.workspace_state.workspace_id,
            session.session_id,
            artifact,
        )
        self._finalize_and_publish(session.session_id)
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.begin()
            EvidenceInvalidationMixin._invalidate_canonical_staging(
                connection,
                reason="TEST_INVALIDATION",
            )
            connection.commit()

        self.assertEqual(
            self.repository.current_derived_value_artifacts(
                self.workspace_state.workspace_id
            ),
            (),
        )
        self.assertEqual(
            self.repository.find_derived_value_artifact(
                self.workspace_state.workspace_id,
                artifact.dataset_id,
                artifact.logical_hash,
            ),
            artifact,
        )

    def _begin(self):
        session = self.repository.begin_direct_session(
            self.workspace_state.workspace_id,
            self.bindings,
            actor=LOCAL_ACTOR,
        )
        for snapshot in _input_snapshots(self.workspace_state.workspace_id):
            self.repository.bind_prepared_snapshot(
                self.workspace_state.workspace_id,
                session.session_id,
                snapshot,
            )
        return session

    def _finalize(self, session_id: str) -> None:
        self.repository.finalize_direct_session(
            self.workspace_state.workspace_id,
            session_id,
            dataset_evidence={
                "Products & BOM Lines": (
                    DATASET_ID,
                    StagingDatasetRole.JOIN,
                    0,
                    "product.template",
                )
            },
            run_issues=(),
            control_totals=(),
            impact_report=_empty_impact_report(),
        )

    def _finalize_and_publish(self, session_id: str) -> None:
        self._finalize(session_id)
        self.repository.mark_published(self.workspace_state.workspace_id, session_id)


def _artifact(
    workspace_id: str,
    **overrides: object,
) -> DerivedValueArtifact:
    input_snapshots = _input_snapshots(workspace_id)
    arguments: dict[str, object] = {
        "workspace_id": workspace_id,
        "dataset_id": DATASET_ID,
        "dataset_name": "Products & BOM Lines",
        "derivation_kind": DerivedValueKind.JOIN,
        "input_evidence": (
            DerivedValueInput(INPUT_A_ID, input_snapshots[0].content_hash),
            DerivedValueInput(INPUT_B_ID, input_snapshots[1].content_hash),
        ),
        "physical_selection_hash": PHYSICAL_SELECTION_HASH,
        "source_selection_hash": SOURCE_SELECTION_HASH,
        "derived_plan_hash": DERIVED_PLAN_HASH,
        "derivation_rule_hash": HASHES[8],
        "mapping_hash": MAPPING_HASH,
        "schema_hash": SCHEMA_HASH,
        "transformation_program_hash": HASHES[9],
        "lineage_hash": HASHES[10],
        "row_count": 0,
        "physical_schema_hash": HASHES[11],
        "parquet_sha256": HASHES[12],
        "created_at": NOW,
    }
    arguments.update(overrides)
    return DerivedValueArtifact.create(**arguments)


def _input_snapshots(workspace_id: str) -> tuple[PreparedSnapshot, ...]:
    def snapshot(
        dataset_id: str,
        dataset_name: str,
        source_hash: str,
        parquet_hash: str,
    ) -> PreparedSnapshot:
        return PreparedSnapshot.create(
            workspace_id=workspace_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            source_snapshot_hash=source_hash,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            transformation_program_hash=COMPILED_PLAN_HASH,
            row_count=0,
            physical_schema_hash=HASHES[13],
            parquet_sha256=parquet_hash,
            created_at=NOW,
        )

    return (
        snapshot(INPUT_A_ID, "products", HASHES[6], HASHES[11]),
        snapshot(INPUT_B_ID, "bom", HASHES[7], HASHES[12]),
    )


def _empty_impact_report() -> TransformationImpactReport:
    return TransformationImpactReport(
        mapping_content_hash=MAPPING_HASH,
        evaluated_count=0,
        changed_count=0,
        fallback_count=0,
        null_count=0,
        invalid_count=0,
        provided_count=0,
        unchanged_count=0,
        rows=(),
        detail_limit=0,
    )


if __name__ == "__main__":
    unittest.main()

