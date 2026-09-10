from __future__ import annotations

from datetime import UTC, datetime
import tempfile
import unittest
from uuid import uuid4

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.matching_order_repository import (
    MatchingOrderRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import (
    WorkspaceStateRepository,
)
from impodo.domain.matching_order import (
    MatchingOrderCheck,
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
    MatchingOrderPreference,
    MatchingOrderVersionConflict,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
)
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus
from tests.support.paths import REPOSITORY_ROOT


class MatchingOrderRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp")
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_id = str(uuid4())
        WorkspaceStateRepository(self.database).initialize_workbench(
            WorkspaceState(
                workspace_id=self.workspace_id,
                name="Matching order",
                source_system="CSV",
                status=WorkspaceStatus.REGISTERED,
            ),
            actor=LOCAL_ACTOR,
        )
        self.selection = _selection()
        self.schema = _schema(self.workspace_id)
        self.governance = _governance(self.workspace_id, self.schema)
        self.draft = _draft(
            self.workspace_id,
            self.selection,
            self.governance,
        )
        self.database_path = (
            self.database.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.database._connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO source_selection VALUES (1, ?)",
                [self.selection.to_json()],
            )
            connection.execute(
                """
                INSERT INTO mapping_working_draft VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self.draft.mapping_id,
                    self.draft.version,
                    self.selection.content_hash,
                    self.governance.content_hash,
                    self.draft.content_hash,
                    self.draft.updated_at.isoformat(),
                    self.draft.to_json(),
                ],
            )
            connection.execute(
                "INSERT INTO odoo_schema_catalog VALUES (1, ?)",
                [self.schema.to_json()],
            )
            connection.execute(
                "INSERT INTO schema_governance_revision VALUES (?, ?, ?, ?, ?)",
                [
                    self.governance.governance_id,
                    self.governance.version,
                    self.governance.catalog_hash,
                    self.governance.content_hash,
                    self.governance.to_json(),
                ],
            )
            connection.execute(
                "INSERT INTO schema_governance_current VALUES (1, ?, ?)",
                [self.governance.governance_id, self.governance.version],
            )
        projection = type(
            "Projection",
            (),
            {"get_mapping_source_selection": lambda _self, _workspace_id: self.selection},
        )()
        self.repository = MatchingOrderRepository(self.database, projection)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_conflict_reset_and_semantic_isolation(self) -> None:
        semantic_before = self._semantic_rows()
        incomplete = _preference(
            self.workspace_id,
            self.selection,
            version=1,
            order=("dataset:a",),
        )
        with self.assertRaisesRegex(WorkspaceError, "every current"):
            self.repository.save_preference(
                self.workspace_id,
                incomplete,
                expected_version=None,
                actor=LOCAL_ACTOR,
            )

        self.assertIsNone(self.repository.get_preference(self.workspace_id))
        self.assertEqual(self._semantic_rows(), semantic_before)

        first = _preference(
            self.workspace_id,
            self.selection,
            version=1,
            order=("dataset:a", "dataset:b"),
        )

        self.repository.save_preference(
            self.workspace_id,
            first,
            expected_version=None,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(self.repository.get_preference(self.workspace_id), first)
        self.assertEqual(self._semantic_rows(), semantic_before)

        second = _preference(
            self.workspace_id,
            self.selection,
            version=2,
            order=("dataset:b", "dataset:a"),
        )
        self.repository.save_preference(
            self.workspace_id,
            second,
            expected_version=1,
            actor=LOCAL_ACTOR,
        )
        with self.assertRaises(MatchingOrderVersionConflict):
            self.repository.save_preference(
                self.workspace_id,
                second,
                expected_version=1,
                actor=LOCAL_ACTOR,
            )

        self.repository.reset_preference(
            self.workspace_id,
            expected_version=2,
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(self.repository.get_preference(self.workspace_id))
        self.assertEqual(self._semantic_rows(), semantic_before)
        with self.assertRaises(MatchingOrderVersionConflict):
            self.repository.reset_preference(
                self.workspace_id,
                expected_version=2,
                actor=LOCAL_ACTOR,
            )
        with self.database._connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT event_type FROM audit_event "
                    "WHERE event_type LIKE 'MATCHING_ORDER_%' ORDER BY event_id"
                ).fetchall(),
                [
                    ("MATCHING_ORDER_PREFERENCE_SAVED",),
                    ("MATCHING_ORDER_PREFERENCE_SAVED",),
                    ("MATCHING_ORDER_PREFERENCE_RESET",),
                ],
            )

    def test_one_active_check_and_safe_failure_preserve_semantic_evidence(self) -> None:
        semantic_before = self._semantic_rows()
        now = datetime.now(UTC)
        first = MatchingOrderCheckAttempt(
            check_id=str(uuid4()),
            workspace_id=self.workspace_id,
            status=MatchingOrderCheckStatus.QUEUED,
            phase=MatchingOrderCheckPhase.QUEUED,
            message="Waiting to check Odoo",
            progress_percent=0,
            created_at=now,
            updated_at=now,
        )
        stored, created = self.repository.begin_check(
            self.workspace_id,
            first,
            actor=LOCAL_ACTOR,
        )
        self.assertTrue(created)
        self.assertEqual(stored, first)

        second = MatchingOrderCheckAttempt(
            check_id=str(uuid4()),
            workspace_id=self.workspace_id,
            status=MatchingOrderCheckStatus.QUEUED,
            phase=MatchingOrderCheckPhase.QUEUED,
            message="Waiting to check Odoo",
            progress_percent=0,
            created_at=now,
            updated_at=now,
        )
        existing, created = self.repository.begin_check(
            self.workspace_id,
            second,
            actor=LOCAL_ACTOR,
        )
        self.assertFalse(created)
        self.assertEqual(existing.check_id, first.check_id)

        failed = MatchingOrderCheckAttempt(
            check_id=first.check_id,
            workspace_id=self.workspace_id,
            status=MatchingOrderCheckStatus.FAILED,
            phase=MatchingOrderCheckPhase.COMPLETE,
            message="Odoo check could not be completed",
            progress_percent=100,
            created_at=now,
            updated_at=now,
            finished_at=now,
            failure_message="Safe failure",
        )
        self.repository.fail_check(
            self.workspace_id,
            failed,
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(self.repository.get_active_check_attempt(self.workspace_id))
        self.assertEqual(
            self.repository.get_check_attempt(
                self.workspace_id,
                first.check_id,
            ).failure_message,
            "Safe failure",
        )
        self.assertIsNone(self.repository.get_current_check(self.workspace_id))
        self.assertEqual(self._semantic_rows(), semantic_before)

    def test_compatible_check_publishes_aggregate_and_protected_evidence(self) -> None:
        now = datetime.now(UTC)
        attempt = MatchingOrderCheckAttempt(
            check_id=str(uuid4()),
            workspace_id=self.workspace_id,
            status=MatchingOrderCheckStatus.QUEUED,
            phase=MatchingOrderCheckPhase.QUEUED,
            message="Waiting to check Odoo",
            progress_percent=0,
            created_at=now,
            updated_at=now,
        )
        self.repository.begin_check(
            self.workspace_id,
            attempt,
            actor=LOCAL_ACTOR,
        )
        check = MatchingOrderCheck(
            check_id=attempt.check_id,
            workspace_id=self.workspace_id,
            source_selection_hash=self.selection.content_hash,
            schema_hash=self.schema.content_hash,
            governance_hash=self.governance.content_hash,
            working_draft_version=self.draft.version,
            working_draft_hash=self.draft.content_hash,
            target_hash=self.schema.connection_target_hash,
            read_credential_binding_hash=self.schema.read_credential_binding_hash,
            read_principal_hash=self.schema.read_principal_hash,
            read_permission_hash=self.schema.read_permission_hash,
            read_context_hash=self.schema.read_context_hash,
            relationship_results=(),
            unchecked_relationship_count=1,
            ordered_dataset_ids=("dataset:a", "dataset:b"),
            recommendation_hash="sha256:" + "9" * 64,
            schema_changed=False,
            captured_at=now,
            actor_issuer=LOCAL_ACTOR.identity.issuer,
            actor_subject=LOCAL_ACTOR.identity.subject_id,
            actor_display_name=LOCAL_ACTOR.identity.display_name,
        )

        status = self.repository.publish_check(
            self.workspace_id,
            check,
            protected_snapshot_json='{"source_keys":[["SECRET-KEY"]]}',
            actor=LOCAL_ACTOR,
        )

        self.assertIs(status, MatchingOrderCheckStatus.SUCCEEDED)
        self.assertEqual(self.repository.get_current_check(self.workspace_id), check)
        finished = self.repository.get_check_attempt(
            self.workspace_id,
            check.check_id,
        )
        self.assertIs(finished.status, MatchingOrderCheckStatus.SUCCEEDED)
        with self.database._connect(self.database_path) as connection:
            public_json = connection.execute(
                "SELECT check_json FROM matching_order_check WHERE check_id = ?",
                [check.check_id],
            ).fetchone()[0]
            protected_json = connection.execute(
                "SELECT snapshot_json FROM matching_order_protected_snapshot WHERE check_id = ?",
                [check.check_id],
            ).fetchone()[0]
        self.assertNotIn("SECRET-KEY", public_json)
        self.assertIn("SECRET-KEY", protected_json)

    def _semantic_rows(self):
        tables = (
            "mapping_working_draft",
            "mapping_current",
            "canonical_staging_current",
            "quality_current",
            "normalization_current",
            "preflight_current",
            "execution_current",
            "recipe_quality_seed",
        )
        with self.database._connect(self.database_path) as connection:
            return {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY ALL"
                ).fetchall()
                for table in tables
            }


def _selection() -> SourceSelection:
    datasets = tuple(_dataset(suffix) for suffix in ("a", "b"))
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=datetime.now(UTC),
        created_by="Test operator",
        datasets=datasets,
        content_hash="sha256:" + "a" * 64,
    )


def _dataset(suffix: str) -> SourceDataset:
    return SourceDataset(
        dataset_id=f"dataset:{suffix}",
        name=f"table_{suffix}",
        source=FileSourceBinding(
            file_id=f"file:{suffix}",
            table_key=f"table_{suffix}",
            source_sha256="sha256:" + suffix * 64,
            catalog_hash="sha256:" + suffix * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=1,
        columns=(SourceDatasetColumn(1, "Code", f"column:{suffix}", "string"),),
    )


def _preference(
    workspace_id: str,
    selection: SourceSelection,
    *,
    version: int,
    order: tuple[str, ...],
) -> MatchingOrderPreference:
    return MatchingOrderPreference(
        workspace_id=workspace_id,
        version=version,
        source_selection_hash=selection.content_hash,
        ordered_dataset_ids=order,
        updated_at=datetime.now(UTC),
        actor_issuer=LOCAL_ACTOR.identity.issuer,
        actor_subject=LOCAL_ACTOR.identity.subject_id,
        actor_display_name=LOCAL_ACTOR.identity.display_name,
    )


def _schema(workspace_id: str) -> OdooSchemaCatalog:
    field = SchemaField(
        name="x_code",
        label="Code",
        type="char",
        required=False,
        readonly=False,
        relation=None,
        relation_field=None,
        selection=(),
    )
    return OdooSchemaCatalog(
        workspace_id=workspace_id,
        policy_hash="sha256:" + "1" * 64,
        captured_at=datetime.now(UTC),
        captured_by="Test operator",
        connection_mode="remote",
        database="test",
        odoo_version="18.0",
        models=(
            SchemaModel("x.a", "A", (field,)),
            SchemaModel("x.b", "B", (field,)),
        ),
        content_hash="sha256:" + "2" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "3" * 64,
        read_principal_hash="sha256:" + "4" * 64,
        read_permission_hash="sha256:" + "5" * 64,
        read_context_hash="sha256:" + "6" * 64,
        connection_target_hash="sha256:" + "7" * 64,
    )


def _governance(
    workspace_id: str,
    schema: OdooSchemaCatalog,
) -> SchemaGovernance:
    return SchemaGovernance(
        governance_id=str(uuid4()),
        version=1,
        workspace_id=workspace_id,
        catalog_hash=schema.content_hash,
        permitted_models=("x.a", "x.b"),
        business_keys=tuple(
            BusinessKeyDefinition(
                key_id=str(uuid4()),
                model=model,
                key_fields=("x_code",),
                status=BusinessKeyStatus.CONFIRMED,
            )
            for model in ("x.a", "x.b")
        ),
        recorded_at=datetime.now(UTC),
        recorded_by="Test operator",
    )


def _draft(
    workspace_id: str,
    selection: SourceSelection,
    governance: SchemaGovernance,
) -> MappingWorkingDraft:
    definition = MappingDefinition(
        mapping_id=str(uuid4()),
        source_selection_hash=selection.content_hash,
        schema_hash=governance.content_hash,
        datasets=tuple(
            DatasetMapping(
                dataset_id=dataset.dataset_id,
                target_model=f"x.{dataset.dataset_id[-1]}",
                source_identity_column_keys=(dataset.columns[0].stable_key,),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=(dataset.columns[0].stable_key,),
                        target_fields=("x_code",),
                    ),
                ),
            )
            for dataset in selection.datasets
        ),
    )
    return MappingWorkingDraft(
        mapping_id=definition.mapping_id,
        version=4,
        workspace_id=workspace_id,
        base_mapping_version=None,
        definition=definition,
        updated_at=datetime.now(UTC),
        updated_by="Test operator",
    )


if __name__ == "__main__":
    unittest.main()
