"""Require complete immutable input cohorts before original-cell authorization."""

import unittest
from datetime import UTC, datetime

from impodo.domain.preflight.fields_set_aside import FieldSetAside, FieldSetAsideReason
from impodo.domain.preflight.original_values import (
    OriginalSourceInput,
    OriginalSourceRequest,
    OriginalSourceTarget,
    OriginalSourceValue,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)

_HASH = "sha256:" + "a" * 64
_KEYS = ("column:1:000000000001", "column:2:000000000002")


def _snapshot():
    schema = SourceSnapshotSchema.create(
        SourceSnapshotColumn.create(
            ordinal=index,
            stable_key=key,
            source_name=f"Input {index}",
            candidate_type="string",
        )
        for index, key in enumerate(_KEYS, 1)
    )
    return SourceSnapshot.create(
        data_version_id="data-version-1",
        dataset_id="dataset:" + "1" * 24,
        dataset_name="Rows",
        source=FileSourceBinding("file", "csv", _HASH, _HASH, "utf-8", ",", 1),
        physical_selection_hash=_HASH,
        schema=schema,
        row_count=2,
        data_logical_hash=_HASH,
        parquet_sha256=_HASH,
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
    )


class ScopedOriginalContractTests(unittest.TestCase):
    def test_one_logical_finding_contains_the_complete_ordered_input_group(self):
        inputs = tuple(
            OriginalSourceInput(key, OriginalSourceValue.unavailable()) for key in _KEYS
        )
        finding = FieldSetAside(
            FieldSetAsideReason.UNRECOGNIZED,
            "Rows",
            2,
            _KEYS,
            "parent_id",
            "No related match.",
            inputs,
        )
        self.assertEqual(finding.source_inputs, inputs)
        self.assertEqual(len(finding.source_inputs), 2)
        for wrong in (inputs[:1], tuple(reversed(inputs))):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                FieldSetAside(
                    FieldSetAsideReason.UNRECOGNIZED,
                    "Rows",
                    2,
                    _KEYS,
                    "parent_id",
                    "No match.",
                    wrong,
                )
        with self.assertRaises(ValueError):
            FieldSetAside(
                FieldSetAsideReason.EXCLUDED,
                "Rows",
                None,
                _KEYS,
                "parent_id",
                "Whole columns.",
                inputs,
            )

    def test_target_authorization_requires_every_ordered_component(self):
        snapshot = _snapshot()
        targets = tuple(
            OriginalSourceTarget(2, key, "parent_id", _KEYS) for key in _KEYS
        )
        request = OriginalSourceRequest(
            snapshot, "Rows", tuple((2, key) for key in _KEYS), targets
        )
        self.assertEqual(len(request.targets), 2)
        with self.assertRaisesRegex(ValueError, "every input"):
            OriginalSourceRequest(snapshot, "Rows", ((2, _KEYS[0]),), targets[:1])
        wrong = (
            targets[0],
            OriginalSourceTarget(2, _KEYS[1], "parent_id", tuple(reversed(_KEYS))),
        )
        with self.assertRaisesRegex(ValueError, "every input"):
            OriginalSourceRequest(
                snapshot, "Rows", tuple((2, key) for key in _KEYS), wrong
            )

    def test_synthetic_or_unknown_inputs_cannot_enter_a_physical_request(self):
        target = OriginalSourceTarget(2, _KEYS[0], "parent_id", (*_KEYS, "generated"))
        with self.assertRaisesRegex(ValueError, "physical target inputs"):
            OriginalSourceRequest(_snapshot(), "Rows", ((2, _KEYS[0]),), (target,))

    def test_authorization_metadata_cannot_carry_mutable_sequences(self):
        with self.assertRaises(TypeError):
            OriginalSourceTarget(2, _KEYS[0], "parent_id", list(_KEYS))
        with self.assertRaises(TypeError):
            FieldSetAside(
                FieldSetAsideReason.INVALID, "Rows", 2, _KEYS, "parent_id", "Bad.", []
            )
        with self.assertRaises(TypeError):
            OriginalSourceRequest(_snapshot(), "Rows", [], [])

    def test_unkeyed_original_values_cannot_replace_input_evidence(self):
        with self.assertRaisesRegex(TypeError, "typed input evidence"):
            FieldSetAside(
                FieldSetAsideReason.INVALID, "Rows", 2, (_KEYS[0],), "parent_id", "Bad.",
                (OriginalSourceValue.unavailable(),),
            )

    def test_retired_untyped_target_tuples_are_rejected(self):
        with self.assertRaisesRegex(TypeError, "typed target authorization"):
            OriginalSourceRequest(
                _snapshot(), "Rows", ((2, _KEYS[0]),), ((2, _KEYS[0], "parent_id"),)
            )
