"""Recover mixed prepared data without retaining temporary finding storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from impodo.adapters.duckdb import preparation_quality_index, preparation_quality_spill
from impodo.application.workspace.preparation import preparation_capability
from impodo.application.workspace.preparation.job_models import PreparationPhase
from impodo.domain.compiler.columnar_transformation import (
    ColumnarFallbackReason,
    ColumnarOperationKind,
    ColumnarSupport,
)
from impodo.domain.preparation.quality import evaluate_quality
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.app import create_local_app
from tests.performance import test_preparation_scale as fixtures
from tests.support.identity import resolve_local_actor


class MixedFindingRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "sources"
        self.root.mkdir()
        self.store_root = Path(temporary.name) / "store"
        self.app = create_local_app(self.store_root)
        self.context = self.app.state.context
        self.artifacts = self.context.artifacts

    def test_changed_native_metadata_rejects_normalization_then_retry_reopens_review(
        self,
    ):
        original_values = fixtures._related_product_values

        def values(index, column_count):
            source = original_values(index, column_count)
            return (source[0], "", *source[2:]) if index < 97 else source

        with patch.object(fixtures, "_related_product_values", values):
            workspace_id, _, _ = (
                fixtures.PreparationWorkflowScaleTests._prepare_related_product_bom_project_and_evidence(
                    self,
                    product_count=129,
                    bom_line_count=257,
                    column_count=5,
                    mapped_field_count=5,
                    generic_identity=True,
                )
            )
        service = self.context.preparation
        actor = resolve_local_actor(self.context)
        workspace = service.workspaces.get(workspace_id)
        revision = service.mappings.get_mapping_revision(workspace_id)
        physical = service.sources.get_source_selection(workspace_id)
        effective = service.sources.get_mapping_source_selection(workspace_id)
        expected = fixtures.preparation_module.stage_browser_mapping(
            workspace,
            revision.definition,
            physical,
            effective,
            None,
            service.sources.get_source_catalogs(workspace_id),
            self.artifacts,
        )
        compile_programs = (
            fixtures.bounded_preparation_module.compile_columnar_transformation_programs
        )

        def mixed_storage(*args, **kwargs):
            return tuple(
                replace(
                    decision,
                    support=ColumnarSupport.PYTHON_FALLBACK,
                    program=None,
                    fallback_reasons=(
                        ColumnarFallbackReason(
                            code="TEST_ROW_COMPATIBILITY",
                            message="Exercise mixed finding recovery",
                            path="datasets.bom_lines",
                            operation=ColumnarOperationKind.READ_SOURCE,
                        ),
                    ),
                )
                if decision.dataset_name == "bom_lines"
                else decision
                for decision in compile_programs(*args, **kwargs)
            )

        directories = []
        temporary = tempfile.TemporaryDirectory

        def scratch(*args, **kwargs):
            directory = temporary(*args, **kwargs)
            directories.append(Path(directory.name))
            return directory

        database_path = (
            service.staging.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        tampered = []

        def corrupt_after_quality(phase, *_args):
            if phase is not PreparationPhase.NORMALIZING:
                return
            # Quality has closed its local owner. A new canonical read must
            # validate current metadata while scratch findings are still alive.
            with service.staging._connect(database_path) as connection:
                run_id, dataset_id, count, projection = connection.execute(
                    "SELECT run_id, dataset_id, row_count, projection_json"
                    " FROM canonical_prepared_projection"
                    " ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                payload = json.loads(projection)
                payload["row_count"] = count + 1
                connection.execute(
                    "UPDATE canonical_prepared_projection"
                    " SET row_count = ?, projection_json = ?"
                    " WHERE run_id = ? AND dataset_id = ?",
                    [count + 1, json.dumps(payload), run_id, dataset_id],
                )
                tampered.append((run_id, dataset_id, count, projection))

        with (
            patch.object(
                fixtures.bounded_preparation_module,
                "compile_columnar_transformation_programs",
                mixed_storage,
            ),
            patch.object(preparation_quality_spill, "TemporaryDirectory", scratch),
            patch.object(
                preparation_quality_index, "QUALITY_FINDING_INDEX_MAX_BYTES", 16 * 1024
            ),
            patch.object(
                preparation_capability, "MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT", 10
            ),
            patch.object(
                fixtures.quality_module,
                "evaluate_quality",
                side_effect=AssertionError("whole-run quality fallback"),
            ),
            patch.object(
                fixtures.normalization_module,
                "evaluate_normalization",
                side_effect=AssertionError("whole-run normalization fallback"),
            ),
        ):
            first = service.prepare(workspace_id, actor=actor)
            first_quality = service.quality.current_summary(workspace_id)
            with self.assertRaisesRegex(WorkspaceError, "metadata changed"):
                service.prepare(
                    workspace_id, actor=actor, progress=corrupt_after_quality
                )
            self.assertEqual(len(tampered), 1)
            self.assertTrue(all(not directory.exists() for directory in directories))
            self.assertEqual(
                service.normalization.current_summary(workspace_id).content_hash,
                first.content_hash,
            )
            with service.staging._connect(database_path) as connection:
                run_id, dataset_id, count, projection = tampered[0]
                connection.execute(
                    "UPDATE canonical_prepared_projection"
                    " SET row_count = ?, projection_json = ?"
                    " WHERE run_id = ? AND dataset_id = ?",
                    [count, projection, run_id, dataset_id],
                )
            retry = service.prepare(workspace_id, actor=actor)

        self.assertEqual(len(directories), 3)
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assertEqual(first.content_hash, retry.content_hash)
        quality = service.quality.current_summary(workspace_id)
        self.assertEqual(first_quality.content_hash, quality.content_hash)
        self.assertGreater(quality.quarantined_count, 0)
        oracle = evaluate_quality(
            workspace_state=workspace,
            staging=expected.canonical_run,
            physical_rows=dict(expected.physical_rows),
            ruleset=service.quality.current_ruleset(workspace_id),
            published_staging_content_hash=expected.canonical_run.content_hash,
        )
        actual = service.quality.quality.get_quality_run(workspace_id, quality.run_id)
        self.assertEqual(actual.to_json(), oracle.to_json())
        self.assertGreater(len(actual.issues), 128)
        self.assertTrue(
            any(
                issue.dataset == "bom_lines"
                and issue.reason_code == "INCOMING_RELATIONSHIP_PARENT_SET_ASIDE"
                for issue in actual.issues
            )
        )
        self.assertEqual(service.mappings.get_mapping_revision(workspace_id), revision)
        self.assertEqual(service.sources.get_source_selection(workspace_id), physical)
        with service.staging._connect(database_path) as connection:
            shapes = connection.execute(
                "SELECT dataset, COUNT(*), COUNT(*) FILTER (WHERE row_json = '')"
                " FROM canonical_staging_row WHERE run_id = ?"
                " GROUP BY dataset ORDER BY dataset",
                [quality.staging_run_id],
            ).fetchall()
            statuses = connection.execute(
                "SELECT status, COUNT(*) FROM preparation_session"
                " GROUP BY status ORDER BY status"
            ).fetchall()
        self.assertEqual(shapes, [("bom_lines", 257, 0), ("products", 129, 129)])
        self.assertEqual(statuses, [("FAILED", 1), ("PUBLISHED", 2)])

        # A fresh application must use durable review evidence after all three
        # evaluation stores and their SQLite connections have been removed.
        reopened = create_local_app(self.store_root).state.context.preparation
        saved = reopened.quality.current_summary(workspace_id)
        self.assertEqual(saved.content_hash, oracle.content_hash)
        self.assertEqual(
            reopened.quality.quality.get_quality_run(
                workspace_id, saved.run_id
            ).to_json(),
            oracle.to_json(),
        )
        for status, expected_count in (
            ("quarantined", quality.quarantined_count),
            ("ready", quality.ready_count),
        ):
            page = reopened.quality.quality.get_quality_review_page(
                workspace_id, saved.run_id, status=status, page_size=17
            )
            self.assertEqual(page.matching_count, expected_count)
            self.assertEqual(len(page.items), min(17, expected_count))
