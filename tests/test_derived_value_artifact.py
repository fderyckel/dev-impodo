from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import PurePosixPath, PureWindowsPath
import unittest

from impodo.domain.derived_value_artifact import (
    DerivedValueArtifact,
    DerivedValueArtifactContractError,
    DerivedValueInput,
    DerivedValueKind,
    derived_value_artifact_logical_hash,
    derived_value_artifact_storage_key,
)


HASHES = tuple(
    f"sha256:{sha256(str(index).encode('ascii')).hexdigest()}"
    for index in range(12)
)
DATASET_ID = "structural:01234567-89ab-cdef-0123-456789abcdef"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class DerivedValueArtifactContractTests(unittest.TestCase):
    def test_logical_identity_ignores_write_time_and_parquet_encoding(self) -> None:
        first = _artifact(HASHES[10], created_at=NOW)
        second = _artifact(
            HASHES[11],
            created_at=NOW + timedelta(seconds=1),
        )

        self.assertEqual(first.logical_hash, second.logical_hash)
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.parquet_storage_key, second.parquet_storage_key)

    def test_round_trip_and_tampering_are_strict(self) -> None:
        artifact = _artifact(HASHES[10], created_at=NOW)

        self.assertEqual(
            DerivedValueArtifact.from_json(artifact.to_json()),
            artifact,
        )
        payload = json.loads(artifact.to_json())
        payload["row_count"] += 1
        with self.assertRaises(DerivedValueArtifactContractError):
            DerivedValueArtifact.from_json(json.dumps(payload))
        with self.assertRaises(DerivedValueArtifactContractError):
            replace(artifact, created_at=NOW.replace(tzinfo=None))
        with self.assertRaises(DerivedValueArtifactContractError):
            replace(artifact, derivation_kind="invalid")

    def test_inputs_are_unique_canonical_and_hash_bound(self) -> None:
        artifact = _artifact(HASHES[10], created_at=NOW)
        self.assertEqual(
            tuple(item.dataset_id for item in artifact.input_evidence),
            ("dataset:a", "dataset:b"),
        )

        with self.assertRaises(DerivedValueArtifactContractError):
            replace(artifact, input_evidence=tuple(reversed(artifact.input_evidence)))
        with self.assertRaises(DerivedValueArtifactContractError):
            replace(
                artifact,
                input_evidence=(
                    artifact.input_evidence[0],
                    artifact.input_evidence[0],
                ),
            )

    def test_logical_hash_changes_with_every_semantic_binding(self) -> None:
        base = _logical_arguments()
        original = derived_value_artifact_logical_hash(**base)
        changes = (
            ("project_id", "another-project"),
            ("dataset_id", "derived:01234567-89ab-cdef-0123-456789abcdef"),
            ("dataset_name", "another_output"),
            ("derivation_kind", DerivedValueKind.GROUP),
            (
                "input_evidence",
                (DerivedValueInput("dataset:a", HASHES[11]),),
            ),
            ("physical_selection_hash", HASHES[11]),
            ("source_selection_hash", HASHES[11]),
            ("derived_plan_hash", HASHES[11]),
            ("derivation_rule_hash", HASHES[11]),
            ("mapping_hash", HASHES[11]),
            ("schema_hash", HASHES[11]),
            ("transformation_program_hash", HASHES[11]),
            ("lineage_hash", HASHES[11]),
            ("writer_contract_version", 2),
            ("row_count", 11),
        )
        for key, value in changes:
            changed = dict(base)
            changed[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(
                    derived_value_artifact_logical_hash(**changed),
                    original,
                )

    def test_storage_key_is_opaque_and_windows_portable(self) -> None:
        key = derived_value_artifact_storage_key(
            DATASET_ID,
            HASHES[0],
            HASHES[10],
        )
        parts = PurePosixPath(key).parts

        self.assertEqual(parts[:3], ("snapshots", "derived", "v1"))
        self.assertEqual(len(parts[3]), 24)
        self.assertNotIn(DATASET_ID, key)
        path = PureWindowsPath(
            r"C:\Users\12345678901234567890\AppData\Local\Impodo\projects",
            "00000000-0000-0000-0000-000000000000",
            *parts,
        )
        self.assertLessEqual(len(str(path)), 259)
        for invalid in ("", " ../escape", "line\nbreak"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(DerivedValueArtifactContractError):
                    derived_value_artifact_storage_key(
                        invalid,
                        HASHES[0],
                        HASHES[10],
                    )

    def test_dataset_name_is_a_business_label_not_a_database_identifier(self) -> None:
        artifact = DerivedValueArtifact.create(
            **{
                **_logical_arguments(),
                "dataset_name": "Products & BOM Lines",
            },
            physical_schema_hash=HASHES[10],
            parquet_sha256=HASHES[11],
            created_at=NOW,
        )

        self.assertEqual(artifact.dataset_name, "Products & BOM Lines")
        with self.assertRaises(DerivedValueArtifactContractError):
            replace(artifact, dataset_name=" Products")


def _logical_arguments() -> dict[str, object]:
    return {
        "project_id": "project",
        "dataset_id": DATASET_ID,
        "dataset_name": "grouped_output",
        "derivation_kind": DerivedValueKind.JOIN,
        "input_evidence": (
            DerivedValueInput("dataset:a", HASHES[0]),
            DerivedValueInput("dataset:b", HASHES[1]),
        ),
        "physical_selection_hash": HASHES[2],
        "source_selection_hash": HASHES[3],
        "derived_plan_hash": HASHES[4],
        "derivation_rule_hash": HASHES[5],
        "mapping_hash": HASHES[6],
        "schema_hash": HASHES[7],
        "transformation_program_hash": HASHES[8],
        "lineage_hash": HASHES[9],
        "writer_contract_version": 1,
        "row_count": 10,
    }


def _artifact(
    parquet_hash: str,
    *,
    created_at: datetime,
) -> DerivedValueArtifact:
    arguments = _logical_arguments()
    return DerivedValueArtifact.create(
        **arguments,
        physical_schema_hash=HASHES[10],
        parquet_sha256=parquet_hash,
        created_at=created_at,
    )
