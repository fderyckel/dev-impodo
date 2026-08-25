from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import PurePosixPath, PureWindowsPath
import unittest

from impodo.domain.prepared_snapshot import (
    PreparedSnapshot,
    PreparedSnapshotContractError,
    prepared_snapshot_logical_hash,
    prepared_snapshot_storage_key,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
HASH_E = "sha256:" + "e" * 64
HASH_F = "sha256:" + "f" * 64
DATASET_ID = "dataset:0123456789abcdef01234567"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class PreparedSnapshotContractTests(unittest.TestCase):
    def test_logical_identity_ignores_write_time_and_parquet_encoding(self) -> None:
        first = _snapshot(HASH_E, created_at=NOW)
        second = _snapshot(
            HASH_F,
            created_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual(first.logical_hash, second.logical_hash)
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.parquet_storage_key, second.parquet_storage_key)

    def test_round_trip_and_tampering_are_strict(self) -> None:
        snapshot = _snapshot(HASH_E, created_at=NOW)

        self.assertEqual(PreparedSnapshot.from_json(snapshot.to_json()), snapshot)
        payload = json.loads(snapshot.to_json())
        payload["row_count"] += 1
        with self.assertRaises(PreparedSnapshotContractError):
            PreparedSnapshot.from_json(json.dumps(payload))
        with self.assertRaises(PreparedSnapshotContractError):
            replace(snapshot, created_at=NOW.replace(tzinfo=None))

    def test_storage_paths_accept_only_application_constructed_segments(self) -> None:
        key = prepared_snapshot_storage_key(DATASET_ID, HASH_A, HASH_E)
        self.assertEqual(
            key,
            (
                "snapshots/prepared/v3/0123456789abcdef01234567/"
                "abd7a54048ca55aed975af8e803e2a6e"
                "e68098d851e968bed684075b43e9bc99.parquet"
            ),
        )
        for dataset_id in ("../escape", "dataset:not-hex"):
            with self.subTest(dataset_id=dataset_id):
                with self.assertRaises(PreparedSnapshotContractError):
                    prepared_snapshot_storage_key(dataset_id, HASH_A, HASH_E)

    def test_storage_key_fits_the_portable_windows_path_budget(self) -> None:
        key = prepared_snapshot_storage_key(DATASET_ID, HASH_A, HASH_E)
        path = PureWindowsPath(
            r"C:\Users\12345678901234567890\AppData\Local\Impodo\projects",
            "artifacts",
            "ws",
            "00000000-0000-0000-0000-000000000000",
            *PurePosixPath(key).parts,
        )

        self.assertLessEqual(len(str(path)), 259)

    def test_retired_storage_key_is_rejected(self) -> None:
        snapshot = _snapshot(HASH_E, created_at=NOW)
        payload = json.loads(snapshot.to_json())
        payload["parquet_storage_key"] = (
            "snapshots/prepared/v1/0123456789abcdef01234567/"
            + snapshot.logical_hash.removeprefix("sha256:")
            + "/"
            + snapshot.parquet_sha256.removeprefix("sha256:")
            + ".parquet"
        )

        with self.assertRaises(PreparedSnapshotContractError):
            PreparedSnapshot.from_json(json.dumps(payload))

    def test_logical_hash_changes_with_every_transformation_binding(self) -> None:
        base = dict(
            workspace_id="workspace",
            dataset_id=DATASET_ID,
            dataset_name="products",
            source_snapshot_hash=HASH_A,
            mapping_hash=HASH_B,
            schema_hash=HASH_C,
            transformation_program_hash=HASH_D,
            writer_contract_version=1,
            row_count=10,
        )
        original = prepared_snapshot_logical_hash(**base)
        for key, value in (
            ("source_snapshot_hash", HASH_F),
            ("mapping_hash", HASH_F),
            ("schema_hash", HASH_F),
            ("transformation_program_hash", HASH_F),
            ("writer_contract_version", 2),
            ("row_count", 11),
        ):
            changed = dict(base)
            changed[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(
                    prepared_snapshot_logical_hash(**changed),
                    original,
                )


def _snapshot(parquet_hash: str, *, created_at: datetime) -> PreparedSnapshot:
    return PreparedSnapshot.create(
        workspace_id="workspace",
        dataset_id=DATASET_ID,
        dataset_name="products",
        source_snapshot_hash=HASH_A,
        mapping_hash=HASH_B,
        schema_hash=HASH_C,
        transformation_program_hash=HASH_D,
        row_count=10,
        physical_schema_hash=HASH_A,
        parquet_sha256=parquet_hash,
        created_at=created_at,
    )


if __name__ == "__main__":
    unittest.main()
