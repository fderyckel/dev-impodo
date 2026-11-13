"""Prove artifact observations count existing work without weakening evidence."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore, _file_sha256
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.application.shared.io_performance import (
    FileIoPerformance,
    collect_file_io,
    measure_file_io,
)

MODULE = "impodo.application.shared.io_performance"


class ArtifactIoPerformanceTests(TestCase):
    def test_hash_counts_the_existing_single_read_without_ordinary_read_overlap(self):
        content = b"column_a,column_b\nprivate-value,42\n"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "private-customer-name.csv"
            path.write_bytes(content)
            with (
                collect_file_io() as observation,
                patch(f"{MODULE}.perf_counter", side_effect=(10.0, 10.025)),
                patch.object(Path, "open", autospec=True, wraps=Path.open) as opened,
            ):
                # A real file verifies the digest and one underlying open.
                opened.side_effect = lambda current, *args, **kwargs: original_open(
                    current, *args, **kwargs
                )
                result = _file_sha256(path)
            self.assertEqual(result, "sha256:" + sha256(content).hexdigest())
            self.assertEqual(opened.call_count, 1)
            snapshot = observation.snapshot()
            self.assertEqual(snapshot["hash_operations"], 1)
            self.assertEqual(snapshot["hash_bytes"], len(content))
            self.assertEqual(snapshot["hash_ms"], 25.0)
            self.assertNotIn("read_bytes", snapshot)
            self.assertEqual(snapshot["hash_scope"], "read_and_hash_inclusive")
            self.assertNotIn("private", str(snapshot))

    def test_tampering_still_fails_each_materialization_and_is_rehashed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "evidence.parquet"
            path.write_bytes(b"accepted")
            expected = "sha256:" + sha256(b"accepted").hexdigest()
            store = LocalArtifactStore(root / "artifacts")
            with (
                collect_file_io() as observation,
                patch.object(store, "_source_snapshot_path", return_value=path),
            ):
                with store.materialize_source_snapshot(
                    str(uuid4()), "unused", expected_sha256=expected
                ) as materialized:
                    self.assertEqual(materialized, path)
                path.write_bytes(b"tampered")
                with (
                    self.assertRaisesRegex(ArtifactStoreError, "hash verification"),
                    store.materialize_source_snapshot(
                        str(uuid4()), "unused", expected_sha256=expected
                    ),
                ):
                    self.fail("Changed evidence must never be yielded")
            self.assertEqual(observation.snapshot()["hash_operations"], 2)
            self.assertEqual(observation.snapshot()["hash_bytes"], 16)

    def test_rejected_upload_keeps_write_counts_and_removes_partial_file(self):
        with TemporaryDirectory() as directory:
            store = LocalArtifactStore(directory)
            owner_id, artifact_id = str(uuid4()), str(uuid4())
            error = ValueError("Rejected file")
            with collect_file_io() as observation:
                with self.assertRaises(ValueError) as raised:
                    store.store_source(
                        owner_id,
                        artifact_id=artifact_id,
                        suffix=".csv",
                        stream=BytesIO(b"code\nA\n"),
                        maximum_bytes=100,
                        chunk_bytes=4,
                        validator=lambda _path: self._raise(error),
                    )
            self.assertIs(raised.exception, error)
            snapshot = observation.snapshot()
            self.assertEqual(snapshot["write_bytes"], 7)
            self.assertEqual(snapshot["write_operations"], 2)
            self.assertNotIn("hash_bytes", snapshot)
            self.assertEqual(tuple(Path(directory).rglob("*.csv")), ())

    def test_report_write_is_measured_but_path_materialization_is_not_a_read(self):
        with TemporaryDirectory() as directory:
            store = LocalArtifactStore(directory)
            owner_id, run_id = str(uuid4()), str(uuid4())
            content = b'{"status":"complete"}'
            with collect_file_io() as observation:
                store.write_report(owner_id, run_id, "result.json", content)
                before_materialization = observation.snapshot()
                with store.materialize_report(owner_id, run_id, "result.json") as path:
                    self.assertEqual(path.read_bytes(), content)
                self.assertEqual(observation.snapshot(), before_materialization)
            self.assertEqual(observation.snapshot()["write_bytes"], len(content))
            self.assertEqual(observation.snapshot()["write_operations"], 1)
            self.assertNotIn("read_operations", observation.snapshot())

    def test_hash_read_failure_preserves_original_exception_and_partial_bytes(self):
        class BrokenReader(BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("Source disappeared")
                return super().read(size)

        with (
            collect_file_io() as observation,
            patch.object(Path, "open", return_value=BrokenReader(b"bytes")),
            self.assertRaisesRegex(OSError, "Source disappeared"),
        ):
            _file_sha256(Path("unused"))
        snapshot = observation.snapshot()
        self.assertEqual(snapshot["hash_bytes"], 5)
        self.assertEqual(snapshot["failed_operations"], 1)

    def test_failed_optional_observer_does_not_replace_file_failure(self):
        expected = OSError("Original file failure")
        with (
            collect_file_io() as observation,
            patch.object(FileIoPerformance, "record", side_effect=RuntimeError),
            self.assertRaises(OSError) as raised,
        ):
            with measure_file_io("read"):
                raise expected
        self.assertIs(raised.exception, expected)
        self.assertEqual(observation.snapshot()["capture_failure_count"], 1)
        self.assertEqual(observation.snapshot()["status"], "no_observations")

    def test_failed_clock_keeps_work_and_bytes_with_duration_unavailable(self):
        with (
            collect_file_io() as observation,
            patch(f"{MODULE}.perf_counter", side_effect=OSError),
        ):
            with measure_file_io("read") as transfer:
                self.assertIsNotNone(transfer)
                transfer.add_bytes(12)
        snapshot = observation.snapshot()
        self.assertEqual(snapshot["read_bytes"], 12)
        self.assertEqual(snapshot["read_operations"], 1)
        self.assertNotIn("read_ms", snapshot)
        self.assertEqual(snapshot["capture_failure_count"], 1)

    def test_nested_collection_reuses_observations_and_outer_scope_is_reset(self):
        with collect_file_io() as outer:
            with collect_file_io() as inner:
                self.assertIs(inner, outer)
                with measure_file_io("write") as transfer:
                    self.assertIsNotNone(transfer)
                    transfer.add_bytes(8)
        with measure_file_io("write") as unobserved:
            self.assertIsNone(unobserved)
        with collect_file_io() as fresh:
            self.assertEqual(fresh.snapshot()["status"], "no_observations")
            self.assertNotIn("write_bytes", fresh.snapshot())
        self.assertEqual(outer.snapshot()["write_bytes"], 8)

    @staticmethod
    def _raise(error):
        raise error


original_open = Path.open
