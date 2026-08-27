from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.shared.models import BusinessReference, LogicalReference
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode, WorkspaceStatus
from impodo.domain.preparation.staging import StagingRunStatus
from impodo.domain.preparation.staging_contracts import (
    CanonicalControlTotal,
    CanonicalLineage,
    CanonicalRow,
    CanonicalStagingRun,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingDisposition,
    StagingReconciliation,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.workspace_access import data_version_id


ROOT = REPOSITORY_ROOT
PHYSICAL_HASH = "sha256:" + "1" * 64
MAPPING_HASH = "sha256:" + "2" * 64
SCHEMA_HASH = "sha256:" + "3" * 64
SOURCE_HASH = "sha256:" + "4" * 64


class CanonicalStagingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(database)
        self.repository = StagingRepository(database)
        now = datetime.now(timezone.utc)
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Prepared contacts",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspace_states.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=data_version_id(self.workspace_state.workspace_id),
            created_at=now,
            created_by=LOCAL_ACTOR.identity.display_name,
            datasets=(
                SourceDataset(
                    dataset_id="dataset:contacts",
                    name="contacts",
                    source=FileSourceBinding(
                        file_id=str(uuid4()),
                        table_key="csv",
                        source_sha256=SOURCE_HASH,
                        catalog_hash="sha256:" + "a" * 64,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=1,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "Reference",
                            "column:reference",
                            "string",
                        ),
                    ),
                ),
            ),
            content_hash=PHYSICAL_HASH,
        )
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute(
                "INSERT INTO source_selection VALUES (1, ?)",
                [selection.to_json()],
            )
            connection.execute(
                """
                INSERT INTO mapping_revision
                VALUES ('mapping:contacts', 1, NULL, ?, ?, ?, ?, '{}')
                """,
                [MAPPING_HASH, PHYSICAL_HASH, SCHEMA_HASH, now.isoformat()],
            )
            connection.execute(
                "INSERT INTO mapping_current VALUES (1, 'mapping:contacts', 1)"
            )
            connection.execute(
                """
                INSERT INTO mapping_submission
                VALUES (?, 'mapping:contacts', 1, ?, ?, ?, '{}')
                """,
                [
                    str(uuid4()),
                    MAPPING_HASH,
                    "sha256:" + "b" * 64,
                    now.isoformat(),
                ],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_round_trips_and_same_current_evidence_is_idempotent(
        self,
    ) -> None:
        run = _run(self.workspace_state.workspace_id, value="Alice", row_token="5")

        first = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        repeated = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(repeated.run_id, first.run_id)
        self.assertEqual(repeated.content_hash, run.content_hash)
        restored = self.repository.get_canonical_staging_run(
            self.workspace_state.workspace_id,
            first.run_id,
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.to_json(), run.to_json())
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM canonical_staging_run"
            ).fetchone()
            row_count = connection.execute(
                "SELECT COUNT(*) FROM canonical_staging_row"
            ).fetchone()
            audit_count = connection.execute(
                """
                SELECT COUNT(*) FROM audit_event
                 WHERE event_type = 'CANONICAL_STAGING_PUBLISHED'
                """
            ).fetchone()
        self.assertEqual(run_count, (1,))
        self.assertEqual(row_count, (1,))
        self.assertEqual(audit_count, (1,))

    def test_durable_rows_restore_typed_values_for_downstream_evaluation(
        self,
    ) -> None:
        template = _run(self.workspace_state.workspace_id, value="Alice", row_token="5")
        incoming = LogicalReference(
            origin="incoming",
            key=("CAT-1",),
            dataset="categories",
            target_fields=("name",),
        )
        existing = BusinessReference(
            model="res.country",
            key=("BE",),
        )
        row = replace(
            template.rows[0],
            source_identity=("C001", Decimal("10.2500")),
            target_identity=("C001", incoming),
            target_scope=(existing,),
            proposed_values={
                "amount": Decimal("10.2500"),
                "start_date": date(2026, 8, 7),
                "captured_at": datetime(
                    2026,
                    8,
                    7,
                    12,
                    30,
                    tzinfo=timezone.utc,
                ),
            },
            references={
                "category_id": incoming,
                "country_id": existing,
                "related_ids": (incoming, existing),
            },
        )
        run = replace(
            template,
            rows=(row,),
            datasets=(
                StagingDatasetReconciliation.from_rows(
                    dataset="contacts",
                    target_model="res.partner",
                    physical_dataset_id="dataset:contacts",
                    role=StagingDatasetRole.DIRECT,
                    input_rows=1,
                    source_rows=(2,),
                    lineage_links=1,
                    rows=(row,),
                ),
            ),
            reconciliation=StagingReconciliation.from_rows((row,)),
        )

        summary = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        restored = self.repository.get_canonical_staging_run(
            self.workspace_state.workspace_id,
            summary.run_id,
            expected_content_hash=summary.content_hash,
        )

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored, run)
        self.assertEqual(restored.to_json(), run.to_json())
        self.assertIsInstance(restored.rows[0].references["category_id"], LogicalReference)
        self.assertIsInstance(restored.rows[0].references["country_id"], BusinessReference)

        with self.assertRaisesRegex(WorkspaceError, "changed unexpectedly"):
            self.repository.get_canonical_staging_run(
                self.workspace_state.workspace_id,
                summary.run_id,
                expected_content_hash="sha256:" + "0" * 64,
            )

    def test_new_evidence_supersedes_current_but_preserves_history(self) -> None:
        first = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            _run(self.workspace_state.workspace_id, value="Alice", row_token="5"),
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        second_run = _run(
            self.workspace_state.workspace_id,
            value="Alice Smith",
            row_token="6",
        )

        second = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            second_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )

        self.assertNotEqual(second.run_id, first.run_id)
        self.assertEqual(
            self.repository.get_current_staging_summary(
                self.workspace_state.workspace_id
            ).run_id,
            second.run_id,
        )
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            retired = connection.execute(
                """
                SELECT status, successor_run_id
                  FROM canonical_staging_run
                 WHERE run_id = ?
                """,
                [first.run_id],
            ).fetchone()
        self.assertEqual(
            retired,
            (StagingRunStatus.SUPERSEDED.value, second.run_id),
        )
        self.assertIsNotNone(
            self.repository.get_canonical_staging_run(
                self.workspace_state.workspace_id,
                first.run_id,
            )
        )

    def test_declared_control_totals_round_trip_with_summary(self) -> None:
        control = CanonicalControlTotal(
            control_id="sha256:" + "c" * 64,
            name="Opening balance",
            dataset="contacts",
            target_field="credit_limit",
            expected_total="1000.00",
            actual_total="1000.00",
            tolerance="0.01",
            unit="EUR",
            included_rows=1,
            empty_rows=0,
        )
        run = replace(
            _run(self.workspace_state.workspace_id, value="Alice", row_token="5"),
            control_totals=(control,),
        )

        published = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        restored = self.repository.get_canonical_staging_run(
            self.workspace_state.workspace_id,
            published.run_id,
        )

        self.assertEqual(published.control_totals, (control,))
        self.assertTrue(published.control_totals_passed)
        self.assertEqual(restored.control_totals, (control,))


    def test_failed_batch_publication_rolls_back_and_keeps_current(self) -> None:
        first = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            _run(self.workspace_state.workspace_id, value="Alice", row_token="5"),
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )

        with patch.object(
            self.repository,
            "_insert_canonical_rows",
            side_effect=RuntimeError("injected batch failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected batch failure"):
                self.repository.publish_canonical_staging(
                    self.workspace_state.workspace_id,
                    _run(
                        self.workspace_state.workspace_id,
                        value="Alice Smith",
                        row_token="6",
                    ),
                    mapping_version=1,
                    actor=LOCAL_ACTOR,
                )

        current = self.repository.get_current_staging_summary(
            self.workspace_state.workspace_id
        )
        self.assertEqual(current.run_id, first.run_id)
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM canonical_staging_run"
            ).fetchone()
        self.assertEqual(run_count, (1,))

    def test_target_change_invalidates_current_without_deleting_rows(self) -> None:
        published = self.repository.publish_canonical_staging(
            self.workspace_state.workspace_id,
            _run(self.workspace_state.workspace_id, value="Alice", row_token="5"),
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        current_workspace_state = self.workspace_states.get(
            self.workspace_state.workspace_id
        )
        changed = replace(
            current_workspace_state,
            odoo_database="odoo19_replacement",
            revision=current_workspace_state.revision + 1,
            updated_at=datetime.now(timezone.utc),
        )

        self.workspace_states.save(
            changed,
            expected_revision=current_workspace_state.revision,
            event_type="WORKSPACE_TARGET_UPDATED",
            event_detail="",
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(
            self.repository.get_current_staging_summary(self.workspace_state.workspace_id)
        )
        self.assertIsNotNone(
            self.repository.get_canonical_staging_run(
                self.workspace_state.workspace_id,
                published.run_id,
            )
        )
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            status = connection.execute(
                """
                SELECT status, retired_reason
                  FROM canonical_staging_run
                 WHERE run_id = ?
                """,
                [published.run_id],
            ).fetchone()
        self.assertEqual(
            status,
            (StagingRunStatus.INVALIDATED.value, "WORKSPACE_TARGET_CHANGED"),
        )

    def test_row_writer_uses_bounded_bulk_batches(self) -> None:
        template_run = _run(
            self.workspace_state.workspace_id,
            value="Alice",
            row_token="5",
        )
        template = template_run.rows[0]
        rows = tuple(
            replace(
                template,
                row_id=f"sha256:{index:064x}",
                source_row=index + 2,
                lineage=replace(
                    template.lineage,
                    source_row=index + 2,
                    physical_source_rows=(index + 2,),
                ),
            )
            for index in range(2_001)
        )
        connection = MagicMock()

        self.repository._insert_canonical_rows(
            connection,
            str(uuid4()),
            replace(
                template_run,
                rows=rows,
                reconciliation=StagingReconciliation.from_rows(rows),
                datasets=(
                    replace(
                        template_run.datasets[0],
                        input_rows=len(rows),
                        input_rows_used=len(rows),
                        output_rows=len(rows),
                        lineage_links=len(rows),
                        candidate_rows=len(rows),
                    ),
                ),
            ),
        )

        self.assertEqual(connection.execute.call_count, 3)
        self.assertEqual(
            [
                len(item.args[1][0])
                for item in connection.execute.call_args_list
            ],
            [1_000, 1_000, 1],
        )
        connection.executemany.assert_not_called()


def _run(project_id: str, *, value: str, row_token: str) -> CanonicalStagingRun:
    lineage = CanonicalLineage(
        source_selection_hash=PHYSICAL_HASH,
        source_hash=SOURCE_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        derived_plan_hash=None,
        dataset="contacts",
        source_row=2,
        physical_dataset_id="dataset:contacts",
        physical_source_rows=(2,),
        field_sources={"name": ("column:reference",)},
    )
    row = CanonicalRow(
        row_id="sha256:" + row_token * 64,
        dataset="contacts",
        source_row=2,
        target_model="res.partner",
        disposition=StagingDisposition.CANDIDATE,
        source_identity=("C001",),
        target_identity=("C001",),
        target_scope=(),
        proposed_values={"name": value},
        references={},
        issues=(),
        lineage=lineage,
    )
    dataset = StagingDatasetReconciliation.from_rows(
        dataset="contacts",
        target_model="res.partner",
        physical_dataset_id="dataset:contacts",
        role=StagingDatasetRole.DIRECT,
        input_rows=1,
        source_rows=(2,),
        lineage_links=1,
        rows=(row,),
    )
    return CanonicalStagingRun(
        workspace_id=project_id,
        mapping_id="mapping:contacts",
        physical_selection_hash=PHYSICAL_HASH,
        source_selection_hash=PHYSICAL_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        derived_plan_hash=None,
        datasets=(dataset,),
        rows=(row,),
        issues=(),
        reconciliation=StagingReconciliation.from_rows((row,)),
        compiled_plan_hash=MAPPING_HASH,
    )

