from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import unittest
from unittest.mock import patch

from impodo.domain.source_snapshot import (
    EncodedSourceCell,
    SOURCE_ROW_COLUMN,
    SourceCellKind,
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotContractError,
    SourceSnapshotSchema,
    source_kind_column,
    source_snapshot_storage_key,
    source_value_column,
)
from impodo.domain.source_binding import FileSourceBinding


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64
DATASET_ID = "dataset:" + "1" * 24


class EncodedSourceCellTests(unittest.TestCase):
    def test_adversarial_source_scalars_round_trip_without_text_change(self) -> None:
        values = (
            None,
            "",
            "  Café\u00a0東京 😀  ",
            True,
            False,
            0,
            -(2**63),
            2**80,
            -0.0,
            1.2345678901234567,
            Decimal("12345678901234567890.0012300"),
            date(2024, 2, 29),
            datetime(2024, 2, 29, 23, 59, 58, 123456),
            datetime(
                2024,
                2,
                29,
                23,
                59,
                58,
                123456,
                tzinfo=timezone(timedelta(hours=5, minutes=30)),
            ),
            time(23, 59, 58, 123456),
            time(
                23,
                59,
                58,
                123456,
                tzinfo=timezone(timedelta(hours=-4)),
            ),
            timedelta(microseconds=-1),
            timedelta(days=4, seconds=5, microseconds=6),
        )

        for value in values:
            with self.subTest(value=value):
                encoded = EncodedSourceCell.from_python(value)
                portable = encoded.to_portable_dict()
                restored_cell = EncodedSourceCell.from_portable_dict(portable)
                restored = restored_cell.to_python()
                self.assertEqual(type(restored), type(value))
                self.assertEqual(restored, value)
                self.assertEqual(
                    restored_cell.text,
                    None if value is None else str(value),
                )
                if isinstance(value, float) and value == 0:
                    self.assertEqual(
                        math.copysign(1, restored),
                        math.copysign(1, value),
                    )

    def test_null_and_empty_string_have_distinct_physical_evidence(self) -> None:
        null = EncodedSourceCell.from_python(None)
        empty = EncodedSourceCell.from_python("")

        self.assertEqual(null.kind, SourceCellKind.NULL)
        self.assertIsNone(null.text)
        self.assertEqual(empty.kind, SourceCellKind.STRING)
        self.assertEqual(empty.text, "")
        self.assertNotEqual(null.to_portable_dict(), empty.to_portable_dict())

    def test_non_finite_and_unsupported_values_fail_closed(self) -> None:
        for value in (float("nan"), float("inf"), Decimal("NaN"), b"bytes"):
            with self.subTest(value=value):
                with self.assertRaises(SourceSnapshotContractError):
                    EncodedSourceCell.from_python(value)

    def test_invalid_physical_cell_payloads_fail_closed(self) -> None:
        invalid = (
            {"kind": 0, "text": "not-null"},
            {"kind": 1, "text": None},
            {"kind": 2, "text": "yes"},
            {"kind": 3, "text": "1.2"},
            {"kind": 3, "text": "01"},
            {"kind": 4, "text": "nan"},
            {"kind": 6, "text": "31/12/2024"},
            {"kind": 7, "text": "2024-01-01"},
            {"kind": 9, "text": "00:99:00"},
            {"kind": 255, "text": "x"},
        )

        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(SourceSnapshotContractError):
                    EncodedSourceCell.from_portable_dict(payload)


class SourceSnapshotSchemaTests(unittest.TestCase):
    def test_schema_uses_ordinals_not_user_headers_for_physical_names(self) -> None:
        schema = _schema(
            names=(
                "../customer/name",
                "__impodo_source_row",
                "Prix € / 日本語",
            )
        )

        self.assertEqual(schema.source_row_column, SOURCE_ROW_COLUMN)
        self.assertEqual(
            tuple(item.value_column for item in schema.columns),
            ("v_000001", "v_000002", "v_000003"),
        )
        self.assertEqual(
            tuple(item.kind_column for item in schema.columns),
            ("k_000001", "k_000002", "k_000003"),
        )
        self.assertNotIn("customer", schema.columns[0].value_column)

    def test_identical_schema_is_deterministic_and_order_is_significant(self) -> None:
        first = _schema(names=("Code", "Name"))
        second = _schema(names=("Code", "Name"))

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.to_portable_dict(), second.to_portable_dict())
        with self.assertRaises(SourceSnapshotContractError):
            SourceSnapshotSchema.create(reversed(first.columns))

    def test_schema_physical_type_tampering_is_rejected(self) -> None:
        schema = _schema()
        payload = schema.to_portable_dict()
        columns = payload["columns"]
        assert isinstance(columns, list)
        columns[0]["value_physical_type"] = "binary"

        with self.assertRaises(SourceSnapshotContractError):
            SourceSnapshotSchema.from_portable_dict(payload)

    def test_duplicate_stable_keys_and_forged_physical_names_are_rejected(self) -> None:
        first = SourceSnapshotColumn.create(
            ordinal=1,
            stable_key="column:1:a",
            source_name="Code",
            candidate_type="string",
        )
        duplicate = SourceSnapshotColumn.create(
            ordinal=2,
            stable_key="column:1:a",
            source_name="Name",
            candidate_type="string",
        )
        with self.assertRaises(SourceSnapshotContractError):
            SourceSnapshotSchema.create((first, duplicate))
        with self.assertRaises(SourceSnapshotContractError):
            replace(first, value_column="../../source")

    def test_physical_names_reject_non_positive_ordinals(self) -> None:
        for function in (source_value_column, source_kind_column):
            with self.assertRaises(SourceSnapshotContractError):
                function(0)


class SourceSnapshotManifestTests(unittest.TestCase):
    def test_manifest_round_trip_binds_logical_and_physical_content(self) -> None:
        snapshot = _snapshot()

        restored = SourceSnapshot.from_json(snapshot.to_json())

        self.assertEqual(restored, snapshot)
        self.assertEqual(restored.content_hash, snapshot.content_hash)
        parts = PurePosixPath(snapshot.parquet_storage_key).parts
        self.assertEqual(parts[:4], ("snapshots", "source", "v4", "1" * 24))
        self.assertEqual(len(parts), 5)
        self.assertRegex(parts[4], r"^[0-9a-f]{64}\.parquet$")

    def test_logical_hash_ignores_write_time_and_parquet_encoding(self) -> None:
        first = _snapshot(created_at=datetime(2026, 8, 9, tzinfo=timezone.utc))
        second = _snapshot(
            parquet_sha256=HASH_D,
            created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(first.logical_hash, second.logical_hash)
        self.assertNotEqual(first.parquet_storage_key, second.parquet_storage_key)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_creation_calculates_the_snapshot_logical_root_once(self) -> None:
        from impodo.domain import source_snapshot as contract

        with patch.object(
            contract,
            "source_snapshot_logical_hash",
            wraps=contract.source_snapshot_logical_hash,
        ) as calculate:
            _snapshot()

        self.assertEqual(calculate.call_count, 1)

    def test_changed_governed_input_changes_logical_hash_and_path(self) -> None:
        first = _snapshot()
        changed = _snapshot(row_count=first.row_count + 1)

        self.assertNotEqual(first.logical_hash, changed.logical_hash)
        self.assertNotEqual(first.parquet_storage_key, changed.parquet_storage_key)

    def test_manifest_tampering_is_rejected(self) -> None:
        snapshot = _snapshot()
        for field, value in (
            ("row_count", snapshot.row_count + 1),
            ("parquet_sha256", HASH_D),
            ("parquet_storage_key", "../../escape.parquet"),
            ("content_hash", HASH_D),
        ):
            with self.subTest(field=field):
                payload = json.loads(snapshot.to_json())
                payload[field] = value
                with self.assertRaises(SourceSnapshotContractError):
                    SourceSnapshot.from_json(json.dumps(payload))

    def test_storage_key_rejects_caller_controlled_path_segments(self) -> None:
        self.assertEqual(
            source_snapshot_storage_key(DATASET_ID, HASH_A, HASH_B),
            "snapshots/source/v4/"
            + "1" * 24
            + "/e9c0bd0498a3b16db3ea90d302340b4d"
            + "f50a5583de5270971a2b207d101ffd8a.parquet",
        )
        for dataset_id in (
            "dataset:../../escape",
            "dataset:" + "A" * 24,
            "other:" + "1" * 24,
        ):
            with self.subTest(dataset_id=dataset_id):
                with self.assertRaises(SourceSnapshotContractError):
                    source_snapshot_storage_key(dataset_id, HASH_A, HASH_B)
        with self.assertRaises(SourceSnapshotContractError):
            source_snapshot_storage_key(DATASET_ID, "../../escape", HASH_B)
        with self.assertRaises(SourceSnapshotContractError):
            source_snapshot_storage_key(DATASET_ID, HASH_A, "../../escape")

    def test_storage_key_fits_the_portable_windows_path_budget(self) -> None:
        key = source_snapshot_storage_key(DATASET_ID, HASH_A, HASH_B)
        path = PureWindowsPath(
            r"C:\Users\12345678901234567890\AppData\Local\Impodo\projects",
            "00000000-0000-0000-0000-000000000000",
            *PurePosixPath(key).parts,
        )

        self.assertLessEqual(len(str(path)), 259)

    def test_non_current_storage_key_is_rejected(self) -> None:
        snapshot = _snapshot()
        payload = json.loads(snapshot.to_json())
        payload["parquet_storage_key"] = (
            "snapshots/source/v1/"
            + "1" * 24
            + "/"
            + snapshot.logical_hash.removeprefix("sha256:")
            + "/"
            + snapshot.parquet_sha256.removeprefix("sha256:")
            + ".parquet"
        )

        with self.assertRaises(SourceSnapshotContractError):
            SourceSnapshot.from_json(json.dumps(payload))

    def test_naive_creation_timestamp_is_rejected(self) -> None:
        with self.assertRaises(SourceSnapshotContractError):
            _snapshot(created_at=datetime(2026, 8, 9))


def _schema(*, names: tuple[str, ...] = ("Code", "Name")) -> SourceSnapshotSchema:
    return SourceSnapshotSchema.create(
        SourceSnapshotColumn.create(
            ordinal=index,
            stable_key=f"column:{index}:{index:012x}",
            source_name=name,
            candidate_type="string",
        )
        for index, name in enumerate(names, start=1)
    )


def _snapshot(
    *,
    row_count: int = 3,
    parquet_sha256: str = HASH_C,
    created_at: datetime = datetime(2026, 8, 9, tzinfo=timezone.utc),
) -> SourceSnapshot:
    return SourceSnapshot.create(
        data_version_id="project-1",
        dataset_id=DATASET_ID,
        dataset_name="products",
        source=FileSourceBinding(
            file_id="file-1",
            table_key="csv",
            source_sha256=HASH_A,
            catalog_hash=HASH_B,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        physical_selection_hash=HASH_C,
        schema=_schema(),
        row_count=row_count,
        data_logical_hash=HASH_B,
        parquet_sha256=parquet_sha256,
        created_at=created_at,
    )


if __name__ == "__main__":
    unittest.main()
