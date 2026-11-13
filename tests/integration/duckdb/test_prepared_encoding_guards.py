"""Reject unsupported prepared encodings at each persisted read boundary."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from unittest.mock import Mock

import duckdb

from impodo.adapters.duckdb.preparation_numeric_precision import (
    bounded_numeric_precision_rows,
)
from impodo.adapters.duckdb.preparation_quality_index import PreparationQualityIndex
from impodo.adapters.duckdb.quality_review_projection import (
    read_category_parent_paths,
    read_projected_review_rows,
)
from impodo.domain.staging.preparation_session import (
    PreparedCanonicalEncodingUnsupported,
)
from impodo.domain.target_numeric_precision import PreparedNumericField
from impodo.domain.workspace.errors import WorkspaceError

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"


class PreparedEncodingGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:", config={"threads": "1"})
        self.addCleanup(self.connection.close)
        self.connection.execute(
            """
            CREATE TABLE canonical_staging_row (
                run_id VARCHAR, ordinal BIGINT, row_id VARCHAR, dataset VARCHAR,
                source_row BIGINT, target_model VARCHAR, disposition VARCHAR,
                row_json VARCHAR
            );
            CREATE TABLE canonical_prepared_projection (
                run_id VARCHAR, ordinal_start BIGINT, row_count BIGINT,
                projection_json VARCHAR, prepared_snapshot_hash VARCHAR,
                dataset_id VARCHAR, dataset VARCHAR
            );
            CREATE TABLE prepared_snapshot_manifest (
                content_hash VARCHAR, dataset_id VARCHAR, manifest_json VARCHAR
            );
            CREATE TABLE canonical_staging_row_issue (
                run_id VARCHAR, ordinal BIGINT, issue_ordinal BIGINT, issue_json VARCHAR
            );
            """
        )
        self.connection.execute(
            "INSERT INTO canonical_staging_row VALUES (?, 0, 'row-1', 'rows', 2, "
            "'x_document', 'CANDIDATE', '')",
            [SESSION_ID],
        )
        self.connection.execute(
            "INSERT INTO canonical_prepared_projection VALUES "
            "(?, 0, 1, ?, 'prepared-hash', 'dataset:rows', 'rows')",
            [SESSION_ID, json.dumps({"contract_version": 3})],
        )
        self.connection.execute(
            "INSERT INTO prepared_snapshot_manifest VALUES "
            "('prepared-hash', 'dataset:rows', '{}')"
        )
        self.artifacts = Mock()
        self.host = Mock()
        self.host._artifacts = self.artifacts

    def _assert_rejected(self, operation: Callable[[], object]) -> None:
        for version in (3, 5):
            with self.subTest(encoding_version=version):
                saved = json.dumps({"contract_version": version})
                self.connection.execute(
                    "UPDATE canonical_prepared_projection SET projection_json = ?",
                    [saved],
                )
                with self.assertRaisesRegex(
                    WorkspaceError, "unsupported format.*Prepare data again"
                ) as raised:
                    operation()
                self.assertIsInstance(
                    raised.exception.__cause__, PreparedCanonicalEncodingUnsupported
                )
                self.artifacts.materialize_prepared_snapshot.assert_not_called()
                self.assertEqual(
                    self.connection.execute(
                        "SELECT projection_json FROM canonical_prepared_projection"
                    ).fetchall(),
                    [(saved,)],
                )
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM canonical_staging_row"
                    ).fetchone(),
                    (1,),
                )

    def test_selected_review_records_require_the_current_encoding(self) -> None:
        self._assert_rejected(
            lambda: read_projected_review_rows(
                self.connection,
                SESSION_ID,
                ("row-1",),
                workspace_id=WORKSPACE_ID,
                artifacts=self.artifacts,
            )
        )

    def test_parent_path_review_requires_the_current_encoding(self) -> None:
        self._assert_rejected(
            lambda: read_category_parent_paths(
                self.connection,
                SESSION_ID,
                "rows",
                ("P001",),
                workspace_id=WORKSPACE_ID,
                artifacts=self.artifacts,
            )
        )

    def test_native_dependencies_reject_before_filtering_or_opening_artifacts(
        self,
    ) -> None:
        self._assert_rejected(
            lambda: tuple(
                PreparationQualityIndex._native_hybrid_dependency_projections(
                    self.host,
                    self.connection,
                    WORKSPACE_ID,
                    SESSION_ID,
                )
            )
        )

    def test_numeric_scan_rejects_without_publishing_findings(self) -> None:
        read_findings = Mock()
        self._assert_rejected(
            lambda: bounded_numeric_precision_rows(
                self.host,
                self.connection,
                WORKSPACE_ID,
                SESSION_ID,
                (PreparedNumericField("rows", "x_document", "amount", (10, 2)),),
                max_bytes=1024 * 1024,
                read_findings=read_findings,
            )
        )
        read_findings.assert_not_called()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM duckdb_tables() "
                "WHERE table_name = 'quality_numeric_failure'"
            ).fetchone(),
            (0,),
        )


if __name__ == "__main__":
    unittest.main()
