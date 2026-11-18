"""Bind immutable physical input cohorts to saved logical field occurrences."""

import unittest
from datetime import UTC, datetime

from impodo.domain.preflight.fields_set_aside import FieldSetAside, FieldSetAsideReason
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalSourceInput,
    OriginalSourceOccurrence,
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
_DATASET_ID = "dataset:" + "1" * 24


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
        dataset_id=_DATASET_ID,
        dataset_name="Rows",
        source=FileSourceBinding("file", "csv", _HASH, _HASH, "utf-8", ",", 1),
        physical_selection_hash=_HASH,
        schema=schema,
        row_count=2,
        data_logical_hash=_HASH,
        parquet_sha256=_HASH,
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
    )


def _coordinate(key=_KEYS[0]):
    return OriginalSourceCoordinate(_DATASET_ID, "Rows", 2, key)


def _occurrence(trace="trace-1"):
    return OriginalSourceOccurrence("Prepared rows", 1, "parent_id", ("alias",), trace)


class ScopedOriginalContractTests(unittest.TestCase):
    def test_physical_geometry_is_independent_of_logical_aliases_and_rows(self):
        inputs = tuple(
            OriginalSourceInput(_coordinate(key), OriginalSourceValue.unavailable())
            for key in _KEYS
        )
        finding = FieldSetAside(
            FieldSetAsideReason.UNRECOGNIZED,
            "Prepared rows",
            1,
            ("alias",),
            "parent_id",
            "No related match.",
            inputs,
            source_trace_id="trace-1",
        )
        self.assertEqual(finding.source_inputs, inputs)
        self.assertTrue(finding.source_inputs_known)
        self.assertEqual(finding.source_keys, ("alias",))
        # A computation can use a subset of its saved logical dependency envelope.
        self.assertEqual(
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "Prepared rows",
                1,
                ("alias", "unused"),
                "parent_id",
                "Invalid.",
                inputs[1:],
            ).source_inputs,
            inputs[1:],
        )
        for row, values in ((None, inputs), (1, (inputs[0], inputs[0]))):
            with self.subTest(row=row), self.assertRaises(ValueError):
                FieldSetAside(
                    FieldSetAsideReason.INVALID,
                    "Prepared rows",
                    row,
                    ("alias",),
                    "parent_id",
                    "Invalid.",
                    values,
                )

    def test_known_constant_and_unknown_history_are_distinct(self):
        unknown = FieldSetAside(
            FieldSetAsideReason.INVALID,
            "Rows",
            1,
            (),
            "name",
            "Invalid.",
        )
        constant = FieldSetAside(
            FieldSetAsideReason.INVALID,
            "Rows",
            1,
            (),
            "name",
            "Invalid.",
            source_inputs_known=True,
        )
        self.assertFalse(unknown.source_inputs_known)
        self.assertTrue(constant.source_inputs_known)
        self.assertEqual(unknown.source_inputs, constant.source_inputs)

    def test_request_validates_physical_snapshot_and_target_cells(self):
        cells = tuple(_coordinate(key) for key in _KEYS)
        targets = tuple(
            OriginalSourceTarget(_occurrence(), coordinate, cells)
            for coordinate in cells
        )
        request = OriginalSourceRequest(_snapshot(), cells, targets)
        self.assertEqual(request.targets, targets)
        with self.assertRaisesRegex(ValueError, "exact physical"):
            OriginalSourceRequest(_snapshot(), cells, targets[:1])
        unknown = _coordinate("generated")
        with self.assertRaisesRegex(ValueError, "physical snapshot"):
            OriginalSourceRequest(
                _snapshot(),
                (unknown,),
                (OriginalSourceTarget(_occurrence(), unknown, (unknown,)),),
            )

    def test_cross_snapshot_cohort_is_validated_globally_by_the_reader(self):
        first = _coordinate()
        second = OriginalSourceCoordinate("other", "Other rows", 3, "other:key")
        cohort = (first, second)
        request = OriginalSourceRequest(
            _snapshot(),
            (first,),
            (OriginalSourceTarget(_occurrence(), first, cohort),),
        )
        self.assertEqual(request.targets[0].input_coordinates, cohort)

    def test_trace_identity_disambiguates_same_logical_dataset_row_and_target(self):
        cell = _coordinate()
        first = OriginalSourceTarget(_occurrence("trace-1"), cell, (cell,))
        second = OriginalSourceTarget(_occurrence("trace-2"), cell, (cell,))
        request = OriginalSourceRequest(_snapshot(), (cell,), (first, second))
        self.assertEqual(len(set(request.targets)), 2)
        different = OriginalSourceTarget(
            _occurrence(), cell, (cell, _coordinate(_KEYS[1]))
        )
        self.assertNotEqual(first, different)
        self.assertEqual(hash(first), hash(different))

    def test_shared_cohort_validation_rejects_duplicates_and_untyped_inputs(self):
        cell = _coordinate()
        for cohort, error in (((cell, cell), ValueError), ((cell, "raw"), TypeError)):
            with self.subTest(cohort=cohort), self.assertRaises(error):
                OriginalSourceRequest(
                    _snapshot(),
                    (cell,),
                    (OriginalSourceTarget(_occurrence(), cell, cohort),),
                )
        with self.assertRaisesRegex(ValueError, "belong"):
            OriginalSourceRequest(
                _snapshot(),
                (cell,),
                (OriginalSourceTarget(_occurrence(), cell, (_coordinate(_KEYS[1]),)),),
            )

    def test_authorization_metadata_cannot_carry_mutable_sequences(self):
        cell = _coordinate()
        with self.assertRaises(TypeError):
            OriginalSourceTarget(_occurrence(), cell, [cell])
        with self.assertRaises(TypeError):
            OriginalSourceOccurrence("Rows", 1, "name", [_KEYS[0]])
        with self.assertRaises(TypeError):
            FieldSetAside(
                FieldSetAsideReason.INVALID, "Rows", 2, _KEYS, "parent_id", "Bad.", []
            )
        with self.assertRaises(TypeError):
            OriginalSourceRequest(_snapshot(), [], [])

    def test_untyped_inputs_and_retired_authorizations_are_rejected(self):
        with self.assertRaisesRegex(TypeError, "typed input evidence"):
            FieldSetAside(
                FieldSetAsideReason.INVALID,
                "Rows",
                2,
                _KEYS,
                "parent_id",
                "Bad.",
                (OriginalSourceValue.unavailable(),),
            )
        with self.assertRaisesRegex(TypeError, "typed target authorization"):
            OriginalSourceRequest(
                _snapshot(), (_coordinate(),), ((2, _KEYS[0], "parent_id"),)
            )
