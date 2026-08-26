from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import polars as pl

from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.derived_values import (
    DerivedValueArtifactCandidateWriter,
    DerivedValueArtifactPublisher,
    DerivedValueArtifactWriteError,
    DerivedValuePage,
    validate_derived_value_artifact,
)
from impodo.domain.derived_value_artifact import (
    DERIVED_VALUE_ORDINAL_COLUMN,
    DerivedValueInput,
    DerivedValueKind,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
HASHES = tuple(
    f"sha256:{sha256(str(index).encode('ascii')).hexdigest()}"
    for index in range(12)
)


class DerivedValueArtifactIoTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.workspace_id = str(uuid4())
        self.workspace_root = self.root / "ws" / self.workspace_id
        self.artifacts = LocalArtifactStore(self.root)
        self.publisher = DerivedValueArtifactPublisher(
            self.artifacts,
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bounded_pages_are_published_and_verified_in_stable_order(self) -> None:
        publication = self.publisher.publish(
            **_publication_arguments(self.workspace_id),
            value_schema={"Product Code": pl.String, "Quantity": pl.Int64},
            pages=(
                DerivedValuePage(
                    0,
                    {"Product Code": ("A", "B"), "Quantity": (2, 3)},
                ),
                DerivedValuePage(
                    2,
                    {"Product Code": ("C",), "Quantity": (5,)},
                ),
            ),
            batch_rows=2,
        )

        artifact = publication.artifact
        self.assertEqual(artifact.row_count, 3)
        self.assertEqual(publication.fragment_count, 2)
        with self.artifacts.materialize_derived_value_artifact(
            self.workspace_id,
            artifact.parquet_storage_key,
            expected_sha256=artifact.parquet_sha256,
        ) as path:
            validate_derived_value_artifact(path, artifact, batch_rows=2)
            self.assertEqual(
                pl.read_parquet(path).to_dict(as_series=False),
                {
                    DERIVED_VALUE_ORDINAL_COLUMN: [0, 1, 2],
                    "Product Code": ["A", "B", "C"],
                    "Quantity": [2, 3, 5],
                },
            )
        work = self.workspace_root / "snapshots" / "derived" / ".work"
        self.assertEqual(tuple(work.iterdir()), ())

    def test_empty_derived_output_has_a_valid_typed_artifact(self) -> None:
        publication = self.publisher.publish(
            **_publication_arguments(self.workspace_id),
            value_schema={"Product Code": pl.String},
            pages=(),
            batch_rows=2,
        )

        self.assertEqual(publication.artifact.row_count, 0)
        self.assertEqual(publication.fragment_count, 0)
        with self.artifacts.materialize_derived_value_artifact(
            self.workspace_id,
            publication.artifact.parquet_storage_key,
            expected_sha256=publication.artifact.parquet_sha256,
        ) as path:
            validate_derived_value_artifact(path, publication.artifact)

    def test_writer_rejects_unbounded_or_noncontiguous_pages(self) -> None:
        with self.artifacts.prepare_derived_value_artifact(
            self.workspace_id
        ) as workspace:
            writer = DerivedValueArtifactCandidateWriter(
                workspace,
                {"code": pl.String},
                batch_rows=2,
            )
            with self.assertRaisesRegex(
                DerivedValueArtifactWriteError,
                "page size",
            ):
                writer.append_columnar_page(
                    DerivedValuePage(0, {"code": ("A", "B", "C")})
                )
            with self.assertRaisesRegex(
                DerivedValueArtifactWriteError,
                "not contiguous",
            ):
                writer.append_columnar_page(
                    DerivedValuePage(1, {"code": ("A",)})
                )

    def test_writer_rejects_schema_drift_and_reserved_columns(self) -> None:
        with self.artifacts.prepare_derived_value_artifact(
            self.workspace_id
        ) as workspace:
            with self.assertRaisesRegex(
                DerivedValueArtifactWriteError,
                "column name",
            ):
                DerivedValueArtifactCandidateWriter(
                    workspace,
                    {DERIVED_VALUE_ORDINAL_COLUMN: pl.Int64},
                )
            writer = DerivedValueArtifactCandidateWriter(
                workspace,
                {"code": pl.String},
            )
            with self.assertRaisesRegex(
                DerivedValueArtifactWriteError,
                "projection",
            ):
                writer.append_columnar_page(
                    DerivedValuePage(0, {"different": ("A",)})
                )

    def test_publication_is_idempotent_for_identical_bytes(self) -> None:
        arguments = {
            **_publication_arguments(self.workspace_id),
            "value_schema": {"code": pl.String},
            "pages": (DerivedValuePage(0, {"code": ("A",)}),),
            "batch_rows": 1,
        }

        first = self.publisher.publish(**arguments)
        second = self.publisher.publish(**arguments)

        self.assertEqual(second.artifact, first.artifact)
        paths = tuple(
            (self.workspace_root / "snapshots" / "derived").rglob(
                "*.parquet"
            )
        )
        self.assertEqual(len(paths), 1)

    def test_tampered_published_bytes_fail_closed(self) -> None:
        publication = self.publisher.publish(
            **_publication_arguments(self.workspace_id),
            value_schema={"code": pl.String},
            pages=(DerivedValuePage(0, {"code": ("A",)}),),
        )
        artifact = publication.artifact
        path = self.workspace_root / artifact.parquet_storage_key
        path.write_bytes(path.read_bytes()[:16])

        with self.assertRaisesRegex(ArtifactStoreError, "hash verification"):
            with self.artifacts.materialize_derived_value_artifact(
                self.workspace_id,
                artifact.parquet_storage_key,
                expected_sha256=artifact.parquet_sha256,
            ):
                pass

    def test_failed_read_back_removes_only_the_new_artifact(self) -> None:
        with (
            patch(
                "impodo.adapters.artifacts.derived_values.validate_derived_value_artifact",
                side_effect=DerivedValueArtifactWriteError("injected failure"),
            ),
            self.assertRaisesRegex(DerivedValueArtifactWriteError, "injected"),
        ):
            self.publisher.publish(
                **_publication_arguments(self.workspace_id),
                value_schema={"code": pl.String},
                pages=(DerivedValuePage(0, {"code": ("A",)}),),
            )

        root = self.workspace_root / "snapshots" / "derived"
        self.assertEqual(tuple(root.rglob("*.parquet")), ())

    def test_byte_limits_fail_before_immutable_publication(self) -> None:
        arguments = {
            **_publication_arguments(self.workspace_id),
            "value_schema": {"code": pl.String},
            "pages": (DerivedValuePage(0, {"code": ("A",)}),),
        }
        for limit_name in ("maximum_temporary_bytes", "maximum_artifact_bytes"):
            with (
                self.subTest(limit_name=limit_name),
                self.assertRaisesRegex(
                    DerivedValueArtifactWriteError,
                    "byte limit",
                ),
            ):
                self.publisher.publish(**arguments, **{limit_name: 1})

        root = self.workspace_root / "snapshots" / "derived"
        self.assertEqual(tuple(root.rglob("*.parquet")), ())

    def test_cleanup_preserves_only_referenced_immutable_artifacts(self) -> None:
        first = self.publisher.publish(
            **_publication_arguments(self.workspace_id),
            value_schema={"code": pl.String},
            pages=(DerivedValuePage(0, {"code": ("A",)}),),
        ).artifact
        second_arguments = _publication_arguments(self.workspace_id)
        second_arguments["derivation_rule_hash"] = HASHES[10]
        second = self.publisher.publish(
            **second_arguments,
            value_schema={"code": pl.String},
            pages=(DerivedValuePage(0, {"code": ("B",)}),),
        ).artifact

        removed = self.artifacts.cleanup_derived_value_artifacts(
            self.workspace_id,
            frozenset((first.parquet_storage_key,)),
        )

        self.assertEqual(removed, 1)
        with self.artifacts.materialize_derived_value_artifact(
            self.workspace_id,
            first.parquet_storage_key,
            expected_sha256=first.parquet_sha256,
        ):
            pass
        with self.assertRaisesRegex(ArtifactStoreError, "missing"):
            with self.artifacts.materialize_derived_value_artifact(
                self.workspace_id,
                second.parquet_storage_key,
                expected_sha256=second.parquet_sha256,
            ):
                pass

    def test_store_rejects_keys_outside_the_derived_namespace(self) -> None:
        with self.artifacts.prepare_derived_value_artifact(
            self.workspace_id
        ) as workspace:
            candidate = workspace / "candidate.parquet"
            candidate.write_bytes(b"candidate")
            candidate_hash = (
                "sha256:" + sha256(candidate.read_bytes()).hexdigest()
            )
            with self.assertRaisesRegex(
                ArtifactStoreError,
                "Invalid derived-value artifact key",
            ):
                self.artifacts.publish_derived_value_artifact(
                    self.workspace_id,
                    candidate,
                    "snapshots/prepared/v2/aaaaaaaaaaaaaaaaaaaaaaaa/"
                    + "b" * 64
                    + ".parquet",
                    expected_sha256=candidate_hash,
                )


def _publication_arguments(workspace_id: str) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "dataset_id": "structural:01234567-89ab-cdef-0123-456789abcdef",
        "dataset_name": "Products & BOM Lines",
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
    }


if __name__ == "__main__":
    unittest.main()
