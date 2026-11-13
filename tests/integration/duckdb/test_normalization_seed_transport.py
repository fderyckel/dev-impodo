"""Preserve normalization evidence while bounding repeated group seeds."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterable
from dataclasses import replace
from hashlib import sha256
from itertools import product
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb import preparation_normalization_records as records
from impodo.adapters.duckdb.preparation_normalization_records import (
    PreparationNormalizationRecords,
)
from impodo.adapters.duckdb.schema.preparation_session import (
    create_preparation_session_schema,
)
from impodo.adapters.duckdb.serialization import (
    EncodedJsonBatch,
    _canonical_json,
    iter_encoded_json_batches,
)
from impodo.domain.preparation.normalization import (
    NormalizationEffect,
    NormalizationGroupKind,
    NormalizationOutcome,
)
from impodo.domain.workspace.errors import WorkspaceError


def _hash(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _metadata() -> dict[str, object]:
    return {
        "dataset": "contacts",
        "target_field": "name",
        "rule_id": _hash("rule"),
        "kind": NormalizationGroupKind.CHANGE,
        "outcome": NormalizationOutcome.AUTOMATIC,
        "name": "Trim café names",
        "explanation": "Keep Unicode: é / e\u0301 / 測定",
        "owner_label": "Data owner",
    }


def _effect(index: int, *, group_id: str | None = None) -> NormalizationEffect:
    return NormalizationEffect(
        effect_id=_hash(f"effect:{index}"),
        group_id=group_id or _hash("group"),
        row_id=_hash(f"row:{index}"),
        dataset="contacts",
        source_row=index + 1,
        target_field="name",
        before=f" café {index} ",
        after=f"café {index}",
        eligible=True,
    )


class NormalizationSeedTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "workspace-engine.duckdb"
        self.session_id = str(uuid4())
        with duckdb.connect(str(self.path)) as connection:
            create_preparation_session_schema(connection)
            connection.execute(
                """
                CREATE TABLE quality_current (singleton_id INTEGER, run_id VARCHAR);
                CREATE TABLE quality_run (
                    run_id VARCHAR, status VARCHAR, staging_content_hash VARCHAR
                );
                CREATE TABLE quality_evidence_projection (run_id VARCHAR);
                CREATE TABLE canonical_staging_run (
                    run_id VARCHAR, content_hash VARCHAR
                );
                CREATE TABLE normalization_run (run_id VARCHAR);
                CREATE TABLE normalization_effect (
                    run_id VARCHAR NOT NULL,
                    ordinal BIGINT NOT NULL,
                    effect_id VARCHAR NOT NULL,
                    group_id VARCHAR NOT NULL,
                    row_id VARCHAR NOT NULL,
                    dataset VARCHAR NOT NULL,
                    source_row BIGINT NOT NULL,
                    target_field VARCHAR NOT NULL,
                    eligible BOOLEAN NOT NULL,
                    effect_json VARCHAR NOT NULL,
                    PRIMARY KEY (run_id, ordinal),
                    UNIQUE (run_id, effect_id)
                );
                INSERT INTO quality_current VALUES (1, 'quality');
                INSERT INTO quality_run VALUES ('quality', 'PUBLISHED', 'content');
                """
            )
            connection.execute(
                "INSERT INTO canonical_staging_run VALUES (?, 'content')",
                [self.session_id],
            )
        # Admission and impact binding have their own integration coverage.
        # This seam keeps the real ledger transaction and transport under test.
        self.host = Mock()
        self.host.workspace_directory.return_value = self.path.parent
        self.host._connect.side_effect = lambda path: duckdb.connect(str(path))

    def _prepare(
        self,
        effects: Iterable[tuple[NormalizationEffect, dict[str, object]]],
    ) -> dict[str, object]:
        with (
            patch.object(
                PreparationNormalizationRecords,
                "_iter_bound_impacts_with_eligibility",
                return_value=iter(()),
            ),
            patch.object(
                PreparationNormalizationRecords,
                "_iter_eligible_warning_issues",
                return_value=iter(()),
            ),
        ):
            return PreparationNormalizationRecords._prepare_normalization_facts(
                self.host,
                "workspace",
                self.session_id,
                effect_builder=lambda rows: iter(effects),
                finding_builder=lambda issues: (),
            )

    def _rows(self, sql: str) -> list[tuple]:
        with duckdb.connect(str(self.path)) as connection:
            return connection.execute(sql).fetchall()

    def _assert_rolled_back(self) -> None:
        for table in (
            "normalization_effect",
            "preparation_normalization_group_seed",
            "preparation_normalization_finding",
        ):
            with self.subTest(table=table):
                self.assertEqual(self._rows(f"SELECT COUNT(*) FROM {table}"), [(0,)])

    def test_repeated_normalized_seeds_hash_once_per_batch_with_exact_evidence(
        self,
    ) -> None:
        metadata = _metadata()
        effects = [
            replace(_effect(0), source_row=9),
            replace(_effect(1), source_row=2),
            replace(_effect(2), source_row=7, eligible=False),
            replace(_effect(3), source_row=1),
            replace(_effect(4), source_row=3, row_id=_effect(0).row_id),
        ]
        normalized = {key: str(value) for key, value in metadata.items()}
        expected_hash = _hash(_canonical_json(normalized))
        metadata_hash_calls: list[object] = []
        seed_batch_sizes: list[int] = []
        original_insert = (
            PreparationNormalizationRecords._insert_prepared_normalization_group_seeds
        )

        def canonical_json(value: object) -> str:
            if isinstance(value, dict) and set(value) == set(metadata):
                metadata_hash_calls.append(value)
            return _canonical_json(value)

        def insert_seeds(connection, rows) -> None:
            batch = tuple(rows)
            seed_batch_sizes.append(len(batch))
            original_insert(connection, batch)

        equivalent = dict(normalized)
        with (
            patch.object(records, "PREPARATION_SESSION_ROW_BATCH_SIZE", 2),
            patch.object(records, "_canonical_json", side_effect=canonical_json),
            patch.object(
                PreparationNormalizationRecords,
                "_insert_prepared_normalization_group_seeds",
                side_effect=insert_seeds,
            ),
        ):
            summary = self._prepare(
                (effect, metadata if index % 2 else equivalent)
                for index, effect in enumerate(effects)
            )

        self.assertEqual(seed_batch_sizes, [1, 1, 1])
        self.assertEqual(len(metadata_hash_calls), 3)
        self.assertEqual(
            self._rows(
                "SELECT metadata_hash FROM preparation_normalization_group_seed"
            ),
            [(expected_hash,)],
        )
        self.assertEqual(
            self._rows(
                "SELECT ordinal, effect_json FROM normalization_effect ORDER BY ordinal"
            ),
            [
                (index, _canonical_json(effect.to_portable_dict()))
                for index, effect in enumerate(effects)
            ],
        )
        self.assertEqual(
            {
                key: summary[key]
                for key in (
                    "effect_count",
                    "changed_record_count",
                    "distinct_rule_count",
                    "distinct_group_count",
                    "distinct_source_count",
                )
            },
            {
                "effect_count": 5,
                "changed_record_count": 3,
                "distinct_rule_count": 1,
                "distinct_group_count": 1,
                "distinct_source_count": 5,
            },
        )
        self.assertEqual(
            summary["effect_groups"],
            (
                (
                    _hash("group"),
                    metadata["rule_id"],
                    "CHANGE",
                    "AUTOMATIC",
                    "contacts",
                    "name",
                    metadata["name"],
                    metadata["explanation"],
                    "Data owner",
                    4,
                    1,
                ),
            ),
        )
        self.assertEqual(summary["finding_groups"], ())
        self.assertEqual(
            summary["examples"],
            {
                _hash("group"): tuple(
                    (effect.source_row, effect.before, effect.after)
                    for effect in sorted(effects, key=lambda item: item.source_row)
                    if effect.eligible
                )
            },
        )

    def test_every_metadata_field_preserves_conflict_rejection_and_rollback(
        self,
    ) -> None:
        for key, batch_size in product(_metadata(), (1, 2)):
            with self.subTest(changed_field=key, batch_size=batch_size):
                metadata = _metadata()
                conflicting = dict(metadata)
                conflicting[key] = str(metadata[key]) + ":changed"
                with (
                    patch.object(
                        records, "PREPARATION_SESSION_ROW_BATCH_SIZE", batch_size
                    ),
                    self.assertRaisesRegex(
                        WorkspaceError, "group metadata is inconsistent"
                    ),
                ):
                    self._prepare([(_effect(0), metadata), (_effect(1), conflicting)])
                self._assert_rolled_back()

    def test_mutating_reused_metadata_between_yields_is_not_hidden(self) -> None:
        metadata = _metadata()

        def effects():
            yield _effect(0), metadata
            metadata["explanation"] = "A changed policy explanation"
            yield _effect(1), metadata

        with self.assertRaisesRegex(WorkspaceError, "group metadata is inconsistent"):
            self._prepare(effects())
        self._assert_rolled_back()

    def test_duplicate_effects_keep_first_payload_and_construction_ordinals(
        self,
    ) -> None:
        first = _effect(0)
        duplicate = replace(first, before="later", after="changed", eligible=False)
        effects = [first, _effect(1), duplicate, _effect(2)]
        with patch.object(records, "PREPARATION_SESSION_ROW_BATCH_SIZE", 2):
            summary = self._prepare((effect, _metadata()) for effect in effects)
        self.assertEqual(summary["effect_count"], 3)
        self.assertEqual(summary["changed_record_count"], 3)
        self.assertEqual(
            self._rows(
                "SELECT ordinal, effect_json FROM normalization_effect ORDER BY ordinal"
            ),
            [
                (index, _canonical_json(effect.to_portable_dict()))
                for index, effect in enumerate(effects)
                if index != 2
            ],
        )

    def test_duplicate_effect_id_does_not_hide_conflicting_group_metadata(self) -> None:
        metadata = _metadata()
        conflicting = dict(metadata, name="A different group name")
        with self.assertRaisesRegex(WorkspaceError, "group metadata is inconsistent"):
            self._prepare([(_effect(0), metadata), (_effect(0), conflicting)])
        self._assert_rolled_back()

    def test_distinct_seeds_keep_utf8_byte_and_row_transport_limits(self) -> None:
        captured: list[EncodedJsonBatch] = []
        seed_batches: list[EncodedJsonBatch] = []
        effects = [
            _effect(index, group_id=_hash(f"group:{index}")) for index in range(9)
        ]
        metadata = dict(_metadata(), name="é" * 200, explanation="測定" * 60)

        def encode(rows, *, max_rows, max_bytes):
            for batch in iter_encoded_json_batches(
                rows, max_rows=max_rows, max_bytes=max_bytes
            ):
                captured.append(batch)
                if "metadata_hash" in json.loads(batch.payload)[0]:
                    seed_batches.append(batch)
                yield batch

        with (
            patch.object(records, "PREPARATION_SESSION_ROW_BATCH_SIZE", 4),
            patch.object(records, "DUCKDB_JSON_BATCH_MAX_BYTES", 1800),
            patch.object(records, "iter_encoded_json_batches", side_effect=encode),
        ):
            summary = self._prepare((effect, metadata) for effect in effects)

        self.assertEqual(summary["effect_count"], 9)
        self.assertEqual(summary["distinct_group_count"], 9)
        self.assertEqual(sum(batch.row_count for batch in seed_batches), 9)
        self.assertGreater(len(seed_batches), 3)
        for batch in captured:
            self.assertLessEqual(batch.row_count, 4)
            self.assertLessEqual(batch.byte_count, 1800)
            self.assertEqual(batch.byte_count, len(batch.payload.encode("utf-8")))

    def test_oversized_seed_still_rejects_and_rolls_back(self) -> None:
        metadata = dict(_metadata(), explanation="測" * 3000)
        with (
            patch.object(records, "DUCKDB_JSON_BATCH_MAX_BYTES", 1800),
            self.assertRaisesRegex(ValueError, "transport row exceeds the byte limit"),
        ):
            self._prepare([(_effect(0), metadata)])
        self._assert_rolled_back()


if __name__ == "__main__":
    unittest.main()
