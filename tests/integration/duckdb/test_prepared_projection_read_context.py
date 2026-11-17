"""Bind each canonical read to current metadata without caching byte validation."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.preparation_stored_run_reader import (
    PreparationStoredRunReader,
)
from impodo.adapters.duckdb.prepared_projection_read_context import (
    PreparedProjectionReadContext,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarTransformationProgram,
    compile_columnar_transformation_program,
)
from impodo.domain.preparation.normalization import canonical_eligible_dataset_hash
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.columnar import test_polars_transformation as fixtures
from tests.integration.duckdb import test_eligible_hash_stream as hash_fixtures

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
OTHER_HASH = "sha256:" + "f" * 64


class PreparedProjectionReadContextTests(unittest.TestCase):
    def setUp(self):
        self.selection = fixtures._selection()
        self.definition = fixtures._definition(self.selection)
        decision = compile_columnar_transformation_program(
            self.definition, self.selection, fixtures.DATASET_ID
        )
        self.assertIsNotNone(decision.program)
        self.program = decision.program

    def _snapshot(self, program=None, **changes):
        program = program or self.program
        return PreparedSnapshot.create(
            **{
                "workspace_id": WORKSPACE_ID,
                "dataset_id": program.dataset_id,
                "dataset_name": program.dataset_name,
                "source_snapshot_hash": fixtures.HASH_A,
                "mapping_hash": program.mapping_content_hash,
                "schema_hash": program.schema_hash,
                "transformation_program_hash": program.content_hash,
                "row_count": 3,
                "physical_schema_hash": fixtures.HASH_B,
                "parquet_sha256": fixtures.HASH_A,
                "created_at": fixtures.NOW,
                **changes,
            }
        )

    def _projection(self, program=None, *, start=0, count=3, source_hash=None):
        program = program or self.program
        return PreparedCanonicalProjection(
            dataset_id=program.dataset_id,
            dataset=program.dataset_name,
            ordinal_start=start,
            row_count=count,
            mode=program.target_mode,
            source_hash=source_hash or fixtures.HASH_A,
            physical_dataset_id=program.dataset_id,
            field_sources={},
            program=program,
            set_based_projection=True,
        )

    def _raw(
        self, projection=None, snapshot=None, *, projection_text=None, **query_changes
    ):
        projection = projection or self._projection()
        snapshot = snapshot or self._snapshot(projection.program)
        values = {
            "start": projection.ordinal_start,
            "count": projection.row_count,
            "projection": projection_text or json.dumps(projection.to_portable_dict()),
            "manifest": snapshot.to_json(),
            **query_changes,
        }
        return (
            values["start"],
            values["count"],
            values["projection"],
            values["manifest"],
        )

    def _read(self, *rows):
        return PreparedProjectionReadContext.create(WORKSPACE_ID, RUN_ID, rows)

    def test_malformed_or_retired_metadata_cannot_create_a_read_context(self):
        projection = self._projection().to_portable_dict()
        for version in (0, 3, False, None, "4", 4.0):
            with self.subTest(version=version), self.assertRaises(WorkspaceError):
                self._read(
                    self._raw(
                        projection_text=json.dumps(
                            {**projection, "contract_version": version}
                        )
                    )
                )
        for text in ("{", "null", "[]", "{}"):
            with self.subTest(projection=text), self.assertRaises(WorkspaceError):
                self._read(self._raw(projection_text=text))
            with self.subTest(manifest=text), self.assertRaises(WorkspaceError):
                self._read(self._raw(manifest=text))
        for row in ((), (0, 3), (0, 3, "{}", "{}", "extra")):
            with self.subTest(row=row), self.assertRaises(WorkspaceError):
                self._read(row)

    def test_internally_valid_but_unbound_manifest_is_rejected(self):
        changes = (
            {"workspace_id": "33333333-3333-4333-8333-333333333333"},
            {"dataset_id": "dataset:" + "b" * 24},
            {"dataset_name": "another-dataset"},
            {"row_count": 4},
            {"mapping_hash": OTHER_HASH},
            {"schema_hash": OTHER_HASH},
            {"transformation_program_hash": OTHER_HASH},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(WorkspaceError):
                self._read(self._raw(snapshot=self._snapshot(**change)))

    def test_query_ranges_and_portable_ranges_must_agree_exactly(self):
        for change in ({"start": 1}, {"count": 4}, {"start": True}, {"count": 3.0}):
            with self.subTest(change=change), self.assertRaises(WorkspaceError):
                self._read(self._raw(**change))
        payload = self._projection().to_portable_dict()
        for change in ({"ordinal_start": -1}, {"row_count": 2}, {"row_count": 3.0}):
            with self.subTest(change=change), self.assertRaises(WorkspaceError):
                self._read(self._raw(projection_text=json.dumps({**payload, **change})))

    def test_a_matching_untrusted_hash_receipt_cannot_replace_program_validation(self):
        payload = self._projection().to_portable_dict()
        payload["program_hash"] = OTHER_HASH
        manifest = self._snapshot(transformation_program_hash=OTHER_HASH)

        with self.assertRaises(WorkspaceError):
            self._read(
                self._raw(projection_text=json.dumps(payload), snapshot=manifest)
            )

    def test_altered_program_and_altered_dataset_binding_are_rejected(self):
        payload = self._projection().to_portable_dict()
        program = dict(payload["program"])
        program["target_model"] = "x_custom.changed"
        altered = {**payload, "program": program}
        for change in (altered, {**payload, "dataset": "another-dataset"}):
            with self.subTest(change=change), self.assertRaises(WorkspaceError):
                self._read(self._raw(projection_text=json.dumps(change)))

    def test_each_dataset_hash_is_validated_once_and_the_next_read_is_fresh(self):
        other_program = replace(
            self.program,
            dataset_id="dataset:" + "c" * 24,
            dataset_name="other-products",
        )
        first = self._raw()
        second = self._raw(self._projection(other_program, start=3))
        original = ColumnarTransformationProgram.content_hash.fget
        calls = []

        def counted(program):
            calls.append(program.dataset_id)
            return original(program)

        with patch.object(
            ColumnarTransformationProgram, "content_hash", property(counted)
        ):
            context = self._read(first, second)
            self.assertEqual(calls, [self.program.dataset_id, other_program.dataset_id])
            for dataset in context.datasets:
                for _ in range(5):
                    self.assertEqual(dataset.row_count, 3)
                    self.assertEqual(
                        dataset.projection.dataset, dataset.snapshot.dataset_name
                    )
            self.assertEqual(len(calls), 2)
            fresh = self._read(first, second)

        self.assertEqual(len(calls), 4)
        self.assertIsNot(fresh, context)
        self.assertIsNot(fresh.datasets[0].projection, context.datasets[0].projection)

    def test_failed_context_does_not_poison_a_corrected_retry(self):
        good = self._raw()
        bad = self._raw(snapshot=self._snapshot(dataset_name="changed"))

        with self.assertRaises(WorkspaceError):
            self._read(bad)
        first = self._read(good)
        with self.assertRaises(WorkspaceError):
            self._read(bad)
        retry = self._read(good)

        self.assertEqual(first, retry)
        self.assertIsNot(first, retry)
        self.assertEqual(retry.workspace_id, WORKSPACE_ID)
        self.assertEqual(retry.run_id, RUN_ID)

    def test_overlapping_ranges_are_rejected_before_the_reader_yields_any_rows(self):
        first_projection = self._projection(count=2)
        first_snapshot = self._snapshot(row_count=2)
        other_program = replace(
            self.program, dataset_id="dataset:" + "d" * 24, dataset_name="details"
        )
        second_projection = self._projection(other_program, start=1, count=1)
        second_snapshot = self._snapshot(other_program, row_count=1)
        rows = (
            self._raw(first_projection, first_snapshot),
            self._raw(second_projection, second_snapshot),
        )
        with self.assertRaisesRegex(WorkspaceError, "overlap"):
            self._read(*rows)

        with duckdb.connect(config={"threads": "1"}) as connection:
            connection.execute(
                "CREATE TABLE canonical_prepared_projection "
                "(ordinal_start BIGINT, row_count BIGINT, projection_json VARCHAR, "
                "prepared_snapshot_hash VARCHAR, dataset_id VARCHAR, run_id VARCHAR)"
            )
            connection.execute(
                "CREATE TABLE prepared_snapshot_manifest "
                "(content_hash VARCHAR, dataset_id VARCHAR, manifest_json VARCHAR)"
            )
            connection.execute(
                "CREATE TABLE canonical_staging_row "
                "(run_id VARCHAR, ordinal BIGINT, row_json VARCHAR)"
            )
            for raw, snapshot in zip(
                rows, (first_snapshot, second_snapshot), strict=True
            ):
                connection.execute(
                    "INSERT INTO canonical_prepared_projection VALUES (?, ?, ?, ?, ?, ?)",
                    [*raw[:3], snapshot.content_hash, snapshot.dataset_id, RUN_ID],
                )
                connection.execute(
                    "INSERT INTO prepared_snapshot_manifest VALUES (?, ?, ?)",
                    [snapshot.content_hash, snapshot.dataset_id, raw[3]],
                )
            connection.executemany(
                "INSERT INTO canonical_staging_row VALUES (?, ?, '')",
                [(RUN_ID, ordinal) for ordinal in range(3)],
            )
            with (
                patch.object(
                    PreparationStoredRunReader,
                    "_iter_stored_direct_encoded_batches",
                    return_value=iter(()),
                ) as stored,
                patch.object(
                    PreparationStoredRunReader,
                    "_iter_projected_dataset_encoded_batches",
                    return_value=iter((("would-yield-a-row",),)),
                ) as projected,
            ):
                reader = PreparationStoredRunReader.iter_direct_encoded_batches(
                    SimpleNamespace(),
                    WORKSPACE_ID,
                    RUN_ID,
                    batch_size=1,
                    connection=connection,
                )
                with self.assertRaisesRegex(WorkspaceError, "overlap"):
                    next(reader)
                stored.assert_not_called()
                projected.assert_not_called()

    def test_real_mixed_reader_preserves_bytes_hashes_and_fresh_contexts(self):
        fixture = hash_fixtures.EligibleHashStreamTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        staging, _ = fixture._native_staging(mixed=True)
        expected_rows = tuple(staging.rows)
        expected_bytes = tuple(
            canonical_json_bytes(row.to_portable_dict()).decode("utf-8")
            for row in expected_rows
        )
        eligible = frozenset(row.row_id for row in expected_rows)
        _, quality, expected_hash = fixture._assert_hash_parity(staging, eligible)
        create = PreparedProjectionReadContext.create
        contexts = []

        def capture(*args, **kwargs):
            context = create(*args, **kwargs)
            contexts.append(context)
            return context

        with patch.object(PreparedProjectionReadContext, "create", side_effect=capture):
            for batch_size in (1, 2, 1):
                with self.subTest(batch_size=batch_size):
                    batches = tuple(
                        fixture.repository.iter_direct_encoded_batches(
                            fixture.workspace_id,
                            staging.rows.canonical_run_id,
                            batch_size=batch_size,
                        )
                    )
                    self.assertTrue(all(len(batch) <= batch_size for batch in batches))
                    self.assertEqual(
                        tuple(row[-1] for batch in batches for row in batch),
                        expected_bytes,
                    )
        self.assertEqual(len(contexts), 3)
        self.assertEqual(len({id(context) for context in contexts}), 3)
        self.assertEqual(
            len({id(context.datasets[0].projection) for context in contexts}), 3
        )
        self.assertEqual(
            canonical_eligible_dataset_hash(staging, quality), expected_hash
        )
