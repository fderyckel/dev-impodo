from __future__ import annotations

from dataclasses import replace
import unittest

from impodo.domain.odoo.contracts import (
    ConnectorIncompleteResultError,
    MetadataSnapshot,
    merge_disjoint_metadata_snapshots,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
)


class MetadataSnapshotMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fingerprint = TargetFingerprint(
            target_hash="sha256:" + "1" * 64,
            connection_mode="REMOTE",
            database="migration",
            odoo_version="20.0",
            snapshot_timestamp="2026-10-12T08:00:00Z",
        )

    @staticmethod
    def _model(name: str) -> ModelMetadata:
        return ModelMetadata(
            model=name,
            description=name,
            fields={"name": FieldMetadata("name", "char")},
        )

    def test_merges_complete_disjoint_deltas_and_discards_bound_hashes(self) -> None:
        current = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.parent": self._model("x.parent")},
            create_defaults={"x.parent": {"name": "Parent"}},
            limitations=("shared limitation", "first limitation"),
            content_hash="sha256:" + "2" * 64,
        )
        addition = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.child": self._model("x.child")},
            create_defaults={"x.child": {"name": "Child"}},
            limitations=("second limitation", "shared limitation"),
            content_hash="sha256:" + "3" * 64,
        )

        merged = merge_disjoint_metadata_snapshots(current, addition)

        self.assertEqual(set(merged.models), {"x.parent", "x.child"})
        self.assertEqual(
            merged.create_defaults,
            {
                "x.parent": {"name": "Parent"},
                "x.child": {"name": "Child"},
            },
        )
        self.assertEqual(
            merged.limitations,
            ("first limitation", "second limitation", "shared limitation"),
        )
        self.assertIs(merged.fingerprint, self.fingerprint)
        self.assertTrue(merged.complete)
        self.assertIsNone(merged.content_hash)

    def test_rejects_deltas_from_a_different_fingerprint(self) -> None:
        current = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.parent": self._model("x.parent")},
        )
        addition = MetadataSnapshot(
            fingerprint=replace(
                self.fingerprint,
                snapshot_timestamp="2026-10-12T08:00:01Z",
            ),
            models={"x.child": self._model("x.child")},
        )

        with self.assertRaisesRegex(
            ConnectorIncompleteResultError,
            "Odoo changed while Impodo was checking linked data",
        ):
            merge_disjoint_metadata_snapshots(current, addition)

    def test_rejects_overlapping_model_deltas(self) -> None:
        current = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.parent": self._model("x.parent")},
        )
        addition = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.parent": self._model("x.parent")},
        )

        with self.assertRaisesRegex(
            ConnectorIncompleteResultError,
            "record type more than once",
        ):
            merge_disjoint_metadata_snapshots(current, addition)

    def test_rejects_incomplete_or_out_of_scope_default_deltas(self) -> None:
        current = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.parent": self._model("x.parent")},
        )
        addition = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={"x.child": self._model("x.child")},
        )
        cases = (
            (
                replace(current, complete=False),
                addition,
                "field details were incomplete",
            ),
            (
                current,
                replace(addition, complete=False),
                "field details were incomplete",
            ),
            (
                replace(
                    current,
                    create_defaults={"x.unknown": {"name": "Unknown"}},
                ),
                addition,
                "field defaults for an unexpected record type",
            ),
            (
                current,
                replace(
                    addition,
                    create_defaults={"x.unknown": {"name": "Unknown"}},
                ),
                "field defaults for an unexpected record type",
            ),
        )

        for first, second, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ConnectorIncompleteResultError,
                message,
            ):
                merge_disjoint_metadata_snapshots(first, second)


if __name__ == "__main__":
    unittest.main()
