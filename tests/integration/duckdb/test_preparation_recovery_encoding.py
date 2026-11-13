"""Recover current prepared rows without reviving unsupported encodings."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.domain.errors import ReadinessError
from impodo.domain.staging.preparation_session import (
    PREPARED_CANONICAL_ENCODING_VERSION,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from tests.performance import test_preparation_scale as fixtures
from tests.support.identity import resolve_local_actor


class PreparationRecoveryEncodingTests(unittest.TestCase):
    def _prepared_workspace(
        self, major: int = 19, *, dirty: bool = False, resolution: bool = False
    ):
        fixture = fixtures.BoundedPreparationParityTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        def schema(**values):
            return OdooSchemaCatalog(**{**values, "odoo_version": f"{major}.0"})

        original_values = fixtures._source_values

        def source_values(index, column_count, workload, *, dirty=False):
            values = original_values(
                index, column_count, workload, dirty=False if resolution else dirty
            )
            if resolution and dirty:
                # Possible duplicates retain distinct record identifiers.
                return (*values[:3], "value-03-00", *values[4:])
            return values

        with (
            patch.object(fixtures, "OdooSchemaCatalog", side_effect=schema),
            patch.object(fixtures, "_source_values", side_effect=source_values),
        ):
            workspace_id, _, _ = (
                fixtures.PreparationWorkflowScaleTests._prepare_project_and_evidence(
                    fixture,
                    row_count=5,
                    column_count=5,
                    mapped_field_count=5,
                    dirty=dirty,
                )
            )
        context = fixture.context
        actor = resolve_local_actor(context)
        if resolution:
            rule_type = fixtures.ResolutionRule

            def resolution_rule(**values):
                if dirty:
                    values = {
                        **values,
                        "blocking_fields": ("x_scale_03",),
                        "candidate_threshold": "0.9",
                    }
                return rule_type(**values)

            with (
                patch.object(fixtures, "PREPARATION_SCALE_ROWS", 5),
                patch.object(fixtures, "ResolutionRule", side_effect=resolution_rule),
            ):
                fixtures.PreparationWorkflowScaleTests._enable_advanced_coverage(
                    fixture, workspace_id
                )
            self._prepare_with_resolution(context, workspace_id, actor, dirty)
        else:
            context.preparation.prepare(workspace_id, actor=actor)
        revision = context.preparation.mappings.get_mapping_revision(workspace_id)
        assert revision is not None
        recovery = context.preparation_recovery
        mapping_hash = revision.definition.content_hash
        current = recovery.current(workspace_id, mapping_hash, actor=actor)
        self.assertIsNotNone(current)
        assert current is not None
        path = (
            recovery.repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with recovery.repository._connect(path) as connection:
            projections = connection.execute(
                "SELECT dataset_id, projection_json FROM canonical_prepared_projection "
                "WHERE run_id = ? ORDER BY ordinal_start",
                [current.staging_run_id],
            ).fetchall()
        self.assertTrue(projections)
        return fixture, workspace_id, mapping_hash, current, projections

    def _replace_projection_text(self, fixture, workspace_id, run_id, text):
        repository = fixture.context.preparation_recovery.repository
        path = repository.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with repository._connect(path) as connection:
            connection.execute(
                "UPDATE canonical_prepared_projection SET projection_json = ? WHERE run_id = ?",
                [text, run_id],
            )

    def test_recovery_rejects_noncurrent_or_malformed_projection_encoding(self):
        fixture, workspace_id, mapping_hash, current, projections = (
            self._prepared_workspace()
        )
        original = json.loads(projections[0][1])
        texts = [
            json.dumps({**original, "contract_version": version})
            for version in (3, 0, 5, False, None, "4", 4.0, 4.5)
        ]
        texts.extend(("malformed", "{}", "null", "[]"))
        recovery = fixture.context.preparation_recovery
        actor = resolve_local_actor(fixture.context)
        inputs = self._recovery_inputs(fixture, workspace_id, mapping_hash)
        with collect_duckdb_request_timings() as current_timings:
            self.assertIsNotNone(recovery.repository.read_current(workspace_id, inputs))
        with patch.object(
            fixture.context.preparation.sessions,
            "_iter_canonical_rows",
            side_effect=AssertionError("Recovery must not restore canonical rows"),
        ):
            for index, text in enumerate(texts):
                with self.subTest(projection_case=index):
                    self._replace_projection_text(
                        fixture, workspace_id, current.staging_run_id, text
                    )
                    with collect_duckdb_request_timings() as timings:
                        self.assertIsNone(
                            recovery.repository.read_current(
                                workspace_id,
                                inputs,
                            )
                        )
                    self.assertEqual(
                        timings.connection_count, current_timings.connection_count
                    )
        self._replace_projection_text(
            fixture, workspace_id, current.staging_run_id, projections[0][1]
        )
        self.assertEqual(
            recovery.current(workspace_id, mapping_hash, actor=actor), current
        )

    def _recovery_inputs(self, fixture, workspace_id, mapping_hash):
        recovery = fixture.context.preparation_recovery
        with patch.object(
            recovery.repository, "read_current", return_value=None
        ) as read:
            recovery.current(
                workspace_id,
                mapping_hash,
                actor=resolve_local_actor(fixture.context),
            )
        return read.call_args.args[1]

    def test_current_json_rows_recover_without_a_projection_or_artifact_read(self):
        fixture, workspace_id, mapping_hash, current, _ = self._prepared_workspace()
        context = fixture.context
        repository = context.preparation_recovery.repository
        path = repository.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with repository._connect(path) as connection:
            rows = tuple(
                row
                for batch in context.preparation.sessions.iter_direct_encoded_batches(
                    workspace_id,
                    current.staging_run_id,
                    batch_size=2,
                    connection=connection,
                )
                for row in batch
            )
            connection.executemany(
                "UPDATE canonical_staging_row SET row_json = ? WHERE run_id = ? AND ordinal = ?",
                [(row[6], current.staging_run_id, row[0]) for row in rows],
            )
            connection.execute(
                "DELETE FROM canonical_prepared_projection WHERE run_id = ?",
                [current.staging_run_id],
            )
        restored = tuple(
            context.preparation.sessions._iter_canonical_rows(
                workspace_id, current.staging_run_id
            )
        )
        self.assertEqual([row.row_id for row in restored], [row[1] for row in rows])
        inputs = self._recovery_inputs(fixture, workspace_id, mapping_hash)
        with (
            patch.object(
                context.artifacts,
                "materialize_prepared_snapshot",
                side_effect=AssertionError("Recovery must not reopen prepared values"),
            ),
            patch.object(
                context.preparation.sessions,
                "_iter_canonical_rows",
                side_effect=AssertionError("Recovery must not restore canonical rows"),
            ),
        ):
            self.assertEqual(repository.read_current(workspace_id, inputs), current)

    def test_fresh_preparation_replaces_retired_result_for_both_odoo_majors(self):
        for major in (19, 20):
            with self.subTest(major=major):
                fixture, workspace_id, mapping_hash, current, projections = (
                    self._prepared_workspace(major)
                )
                retired_text = json.dumps(
                    {**json.loads(projections[0][1]), "contract_version": 3}
                )
                self._replace_projection_text(
                    fixture, workspace_id, current.staging_run_id, retired_text
                )
                context = fixture.context
                actor = resolve_local_actor(context)
                self.assertIsNone(
                    context.preparation_recovery.current(
                        workspace_id, mapping_hash, actor=actor
                    )
                )
                context.preparation.prepare(workspace_id, actor=actor)
                recovered = context.preparation_recovery.current(
                    workspace_id, mapping_hash, actor=actor
                )
                self.assertIsNotNone(recovered)
                assert recovered is not None
                self.assertNotEqual(recovered.staging_run_id, current.staging_run_id)
                path = (
                    context.preparation_recovery.repository.workspace_directory(
                        workspace_id
                    )
                    / "workspace-engine.duckdb"
                )
                with context.preparation_recovery.repository._connect(
                    path
                ) as connection:
                    old_text = connection.execute(
                        "SELECT projection_json FROM canonical_prepared_projection WHERE run_id = ?",
                        [current.staging_run_id],
                    ).fetchone()[0]
                    versions = connection.execute(
                        "SELECT json_extract_string(projection_json, '$.contract_version') "
                        "FROM canonical_prepared_projection WHERE run_id = ?",
                        [recovered.staging_run_id],
                    ).fetchall()
                self.assertEqual(old_text, retired_text)
                self.assertTrue(versions)
                self.assertEqual(
                    set(versions), {(str(PREPARED_CANONICAL_ENCODING_VERSION),)}
                )

    def _prepare_with_resolution(self, context, workspace_id, actor, duplicates):
        if duplicates:
            with self.assertRaisesRegex(
                ReadinessError, "Review the possible duplicate records"
            ):
                context.preparation.prepare(workspace_id, actor=actor)
        else:
            context.preparation.prepare(workspace_id, actor=actor)

    def test_resolution_recovery_rebinds_the_current_staging_parent(self):
        for major in (19, 20):
            for duplicates in (False, True):
                with self.subTest(major=major, duplicates=duplicates):
                    fixture, workspace_id, mapping_hash, _, _ = (
                        self._prepared_workspace(
                            major, dirty=duplicates, resolution=True
                        )
                    )
                    context = fixture.context
                    actor = resolve_local_actor(context)
                    old_resolution = context.resolution.current_summary(workspace_id)
                    self.assertIsNotNone(old_resolution)
                    assert old_resolution is not None
                    current = context.preparation_recovery.current(
                        workspace_id, mapping_hash, actor=actor
                    )
                    self.assertIsNotNone(current)
                    assert current is not None
                    self.assertEqual(
                        current.status,
                        PreparationJobStatus.REVIEW_REQUIRED
                        if duplicates
                        else PreparationJobStatus.SUCCEEDED,
                    )
                    repository = context.preparation_recovery.repository
                    path = (
                        repository.workspace_directory(workspace_id)
                        / "workspace-engine.duckdb"
                    )
                    with repository._connect(path) as connection:
                        projection_text = connection.execute(
                            "SELECT projection_json FROM canonical_prepared_projection WHERE run_id = ?",
                            [current.staging_run_id],
                        ).fetchone()[0]
                        candidate_count = connection.execute(
                            "SELECT COUNT(*) FROM resolution_candidate WHERE run_id = ?",
                            [old_resolution.run_id],
                        ).fetchone()[0]
                    retired_text = json.dumps(
                        {**json.loads(projection_text), "contract_version": 3}
                    )
                    self._replace_projection_text(
                        fixture, workspace_id, current.staging_run_id, retired_text
                    )
                    self.assertIsNone(
                        context.preparation_recovery.current(
                            workspace_id, mapping_hash, actor=actor
                        )
                    )
                    self._prepare_with_resolution(
                        context, workspace_id, actor, duplicates
                    )
                    recovered = context.preparation_recovery.current(
                        workspace_id, mapping_hash, actor=actor
                    )
                    new_resolution = context.resolution.current_summary(workspace_id)
                    self.assertIsNotNone(recovered)
                    self.assertIsNotNone(new_resolution)
                    assert recovered is not None and new_resolution is not None
                    self.assertNotEqual(
                        recovered.staging_run_id, current.staging_run_id
                    )
                    self.assertEqual(
                        new_resolution.staging_run_id, recovered.staging_run_id
                    )
                    self.assertNotEqual(new_resolution.run_id, old_resolution.run_id)
                    self.assertEqual(
                        new_resolution.evaluation_hash, old_resolution.evaluation_hash
                    )
                    with repository._connect(path) as connection:
                        retained = connection.execute(
                            "SELECT COUNT(*) FROM resolution_candidate WHERE run_id = ?",
                            [old_resolution.run_id],
                        ).fetchone()[0]
                        old_text = connection.execute(
                            "SELECT projection_json FROM canonical_prepared_projection WHERE run_id = ?",
                            [current.staging_run_id],
                        ).fetchone()[0]
                    self.assertEqual(retained, candidate_count)
                    self.assertEqual(old_text, retired_text)


if __name__ == "__main__":
    unittest.main()
