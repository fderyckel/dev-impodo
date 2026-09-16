from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import (
    WorkspaceStateRepository,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    TransformationImpactReport,
    TransformationImpactRow,
)
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus
from tests.support.paths import REPOSITORY_ROOT


class TransformationImpactRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / ".tmp")
        self.database = DuckDbWorkspaceDatabase(Path(self.temporary.name))
        self.workspace_id = str(uuid4())
        WorkspaceStateRepository(self.database).initialize_workbench(
            WorkspaceState(
                workspace_id=self.workspace_id,
                name="Impact review",
                source_system="CSV",
                status=WorkspaceStatus.REGISTERED,
            ),
            actor=LOCAL_ACTOR,
        )
        self.repository = TransformationImpactRepository(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_batched_json_preserves_values_and_failed_replace_is_atomic(self) -> None:
        identity = TransformationImpactIdentity(
            physical_selection_hash="sha256:" + "1" * 64,
            source_selection_hash="sha256:" + "2" * 64,
            mapping_content_hash="sha256:" + "3" * 64,
            schema_hash="sha256:" + "4" * 64,
            derived_plan_hash=None,
        )
        special = TransformationImpactRow(
            dataset="Products",
            source_row=2,
            source_column="Name",
            target_field="name",
            raw_value='raw,"\nΩ',
            proposed_value="",
            rules="Trim",
            outcome="changed",
            message='Needs "review"\nnow',
        )
        expected = (
            special,
            *(
                TransformationImpactRow(
                    dataset="Products",
                    source_row=index + 2,
                    source_column="Name",
                    target_field="name",
                    raw_value=str(index),
                    proposed_value=f"value {index}",
                    rules="Formula",
                    outcome="changed",
                )
                for index in range(1, 1_002)
            ),
        )

        def build(write_impact):
            for row in expected:
                write_impact(row)
            return TransformationImpactReport(
                mapping_content_hash=identity.mapping_content_hash,
                evaluated_count=len(expected),
                changed_count=len(expected),
                fallback_count=0,
                null_count=0,
                invalid_count=0,
                provided_count=0,
                unchanged_count=0,
                rows=(),
                detail_limit=0,
            )

        snapshot = self.repository.replace_transformation_impact_snapshot(
            self.workspace_id, identity, build, actor=LOCAL_ACTOR
        )
        filters = TransformationImpactFilter()
        first_page = self.repository.get_transformation_impact_page(
            self.workspace_id, identity, filters, page_size=2
        )
        stored = tuple(self.repository.iter_transformation_impact_rows(
            self.workspace_id, identity, filters
        ))

        self.assertEqual(snapshot.report.changed_count, len(expected))
        self.assertEqual(snapshot.affected_row_count, len(expected))
        self.assertEqual(first_page.rows, expected[:2])
        self.assertEqual(stored, expected)

        newer_identity = TransformationImpactIdentity(
            physical_selection_hash=identity.physical_selection_hash,
            source_selection_hash=identity.source_selection_hash,
            mapping_content_hash="sha256:" + "5" * 64,
            schema_hash=identity.schema_hash,
            derived_plan_hash=None,
        )

        def interrupted(write_impact):
            write_impact(special)
            raise RuntimeError("injected evaluation failure")

        with self.assertRaisesRegex(RuntimeError, "injected evaluation failure"):
            self.repository.replace_transformation_impact_snapshot(
                self.workspace_id,
                newer_identity,
                interrupted,
                actor=LOCAL_ACTOR,
            )
        self.assertIsNotNone(
            self.repository.get_transformation_impact_snapshot(
                self.workspace_id, identity
            )
        )
        self.assertEqual(
            tuple(self.repository.iter_transformation_impact_rows(
                self.workspace_id, identity, filters
            )),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
