"""Keep encoded eligible-row hashing identical to the materialized contract."""

from __future__ import annotations

import unittest
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from impodo.domain.preparation.normalization import (
    NormalizationError,
    canonical_eligible_dataset_hash,
)
from impodo.domain.preparation.quality import StoredQualityRun
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    canonical_json_bytes,
)
from tests.domain.preparation.test_quality import (
    MAPPING_HASH,
    SCHEMA_HASH,
    _canonical_row,
    _staging,
    _stored_staging,
    _workspace_state,
)


class _EncodedRows(Sequence[CanonicalRow]):
    def __init__(self, entries: tuple[tuple[str, str | CanonicalRow], ...]) -> None:
        self.entries = entries
        self.calls = 0

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, _index):
        raise AssertionError("The encoded capability must not slice canonical rows")

    def __iter__(self):
        raise AssertionError("The encoded capability must not reconstruct every row")

    def iter_canonical_hash_rows(self) -> Iterator[tuple[str, str | CanonicalRow]]:
        self.calls += 1
        yield from self.entries


def _quality(staging, eligible_ids) -> StoredQualityRun:
    return StoredQualityRun(
        workspace_id=staging.workspace_id,
        staging_content_hash=staging.content_hash,
        ruleset_hash=MAPPING_HASH,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        retention_context_hash=MAPPING_HASH,
        row_results=(),
        source_accounting=(),
        issues=(),
        quarantine=(),
        effective_dataset_hash=staging.content_hash,
        eligible_row_ids=eligible_ids,
        summary_counts={
            "ready_count": len(eligible_ids),
            "review_count": 0,
            "quarantined_count": 0,
            "excluded_count": len(staging.rows) - len(eligible_ids),
            "blocked_count": 0,
        },
        published_content_hash=content_hash("quality-publication"),
    )


def _oracle(staging_hash, quality_hash, rows, *, effective_hash=None) -> str:
    return content_hash(
        {
            "effective_dataset_hash": effective_hash or staging_hash,
            "quality_content_hash": quality_hash,
            "rows": [row.to_portable_dict() for row in rows],
            "staging_content_hash": staging_hash,
        }
    )


class EligibleHashStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = (
            replace(
                _canonical_row("a", 2),
                proposed_values={
                    "name": 'Été "夏"\\line\n💡\t\u2028',
                    "amount": Decimal("123.4500"),
                    "absent": None,
                    "active": False,
                    "day": date(2026, 11, 13),
                    "moment": datetime(
                        2026, 11, 13, 14, 7, tzinfo=timezone(timedelta(hours=2))
                    ),
                },
                target_scope=(None,),
                references={
                    "group_id": BusinessReference(
                        model="x_group", key=("Équipe",), scope=(None,)
                    ),
                    "parent_id": LogicalReference(
                        origin="target_then_incoming",
                        model="x_parent",
                        dataset="parents",
                        key=("A", "夏"),
                        scope=(None,),
                        target_fields=("code", "ref"),
                        incoming_key=("local",),
                    ),
                },
            ),
            _canonical_row("b", 3),
            _canonical_row("c", 4),
        )
        materialized = _staging(_workspace_state().workspace_id, self.rows)
        self.staging = replace(
            _stored_staging(materialized),
            validated_content_hash=materialized.content_hash,
        )

    def test_mixed_payloads_keep_exact_framing_values_and_iteration_order(self) -> None:
        ordered = (self.rows[2], self.rows[0], self.rows[1])
        source = _EncodedRows(
            tuple(
                (
                    row.row_id,
                    row
                    if index == 1
                    else canonical_json_bytes(row.to_portable_dict()).decode(),
                )
                for index, row in enumerate(ordered)
            )
        )
        staging = replace(self.staging, rows=source)
        quality = _quality(staging, frozenset(row.row_id for row in self.rows))
        actual = canonical_eligible_dataset_hash(staging, quality)
        self.assertEqual(
            actual, _oracle(staging.content_hash, quality.content_hash, ordered)
        )
        self.assertEqual(source.calls, 1)
        self.assertNotEqual(
            actual,
            _oracle(staging.content_hash, quality.content_hash, reversed(ordered)),
        )

    def test_canonical_membership_precedes_set_membership_and_skips_serialization(
        self,
    ) -> None:
        selected = frozenset((self.rows[0].row_id,))

        class CanonicalIds(frozenset):
            def __contains__(self, _value):
                raise AssertionError("Canonical membership must take precedence")

            def contains_canonical(self, value):
                return value in selected

        source = _EncodedRows(
            (
                (self.rows[0].row_id, self.rows[0]),
                (self.rows[1].row_id, self.rows[1]),
            )
        )
        staging = replace(self.staging, rows=source)
        quality = _quality(staging, CanonicalIds(selected))
        expected = _oracle(staging.content_hash, quality.content_hash, self.rows[:1])
        original = CanonicalRow.to_portable_dict

        def serialize(row):
            if row.row_id != self.rows[0].row_id:
                raise AssertionError("An ineligible fallback row was serialized")
            return original(row)

        with patch.object(CanonicalRow, "to_portable_dict", serialize):
            self.assertEqual(
                canonical_eligible_dataset_hash(staging, quality), expected
            )

    def test_effective_rows_bypass_staging_encoded_capability(self) -> None:
        source = _EncodedRows(())
        staging = replace(self.staging, rows=source)
        quality = _quality(self.staging, frozenset((self.rows[1].row_id,)))
        effective = SimpleNamespace(
            content_hash=content_hash("effective-dataset"),
            rows=tuple(
                SimpleNamespace(canonical_row=row) for row in reversed(self.rows)
            ),
        )
        quality = replace(quality, effective_dataset_hash=effective.content_hash)
        self.assertEqual(
            canonical_eligible_dataset_hash(staging, quality, effective=effective),
            _oracle(
                staging.content_hash,
                quality.content_hash,
                self.rows[1:2],
                effective_hash=effective.content_hash,
            ),
        )
        self.assertEqual(source.calls, 0)

    def test_stale_bindings_fail_before_the_encoded_source_is_opened(self) -> None:
        source = _EncodedRows(())
        staging = replace(self.staging, rows=source)
        quality = _quality(self.staging, frozenset())
        for field, message in (
            ("staging_content_hash", "Prepared data"),
            ("effective_dataset_hash", "Resolved data"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(NormalizationError, message),
            ):
                canonical_eligible_dataset_hash(
                    staging,
                    replace(quality, **{field: content_hash("stale")}),
                )
        self.assertEqual(source.calls, 0)

    def test_empty_eligibility_still_consumes_and_propagates_source_errors(
        self,
    ) -> None:
        source = _EncodedRows(((self.rows[0].row_id, self.rows[0]),))
        staging = replace(self.staging, rows=source)
        quality = _quality(staging, frozenset())
        self.assertEqual(
            canonical_eligible_dataset_hash(staging, quality),
            _oracle(staging.content_hash, quality.content_hash, ()),
        )
        self.assertEqual(source.calls, 1)
        failure = ValueError("Invalid later canonical row")

        def broken_source():
            yield self.rows[0].row_id, self.rows[0]
            raise failure

        with (
            patch.object(source, "iter_canonical_hash_rows", broken_source),
            self.assertRaises(ValueError) as raised,
        ):
            canonical_eligible_dataset_hash(staging, quality)
        self.assertIs(raised.exception, failure)

    def test_ordinary_rows_and_explicit_publication_hashes_keep_the_old_contract(
        self,
    ) -> None:
        staging = self.staging
        quality = _quality(
            staging, frozenset((self.rows[0].row_id, self.rows[2].row_id))
        )
        published_quality = content_hash("explicit-quality-publication")
        self.assertEqual(
            canonical_eligible_dataset_hash(
                staging,
                quality,
                staging_content_hash=staging.content_hash,
                quality_content_hash=published_quality,
            ),
            _oracle(
                staging.content_hash, published_quality, (self.rows[0], self.rows[2])
            ),
        )


if __name__ == "__main__":
    unittest.main()
