"""Verify bounded, value-safe JSON transport into hardened DuckDB."""

from __future__ import annotations

import json
import math
import unittest

from impodo.adapters.duckdb.serialization import iter_encoded_json_batches
from scripts.benchmark_duckdb_batch_transport import (
    run_benchmark,
    run_canonical_row_benchmark,
    run_preparation_impact_benchmark,
    run_quality_row_benchmark,
    run_source_accounting_benchmark,
)


class EncodedJsonBatchTests(unittest.TestCase):
    def test_batches_preserve_types_unicode_and_literal_null_text(self) -> None:
        rows = (
            {
                "name": "café",
                "count": 1,
                "enabled": True,
                "optional": None,
                "literal": "null",
            },
            {
                "name": "商品",
                "count": 2,
                "enabled": False,
                "optional": "true",
                "literal": "false",
            },
            {
                "name": "third",
                "count": 3,
                "enabled": True,
                "optional": "",
                "literal": "null",
            },
        )

        batches = tuple(
            iter_encoded_json_batches(
                rows,
                max_rows=2,
                max_bytes=1_024,
            )
        )

        self.assertEqual(tuple(item.row_count for item in batches), (2, 1))
        self.assertEqual(
            [row for item in batches for row in json.loads(item.payload)],
            list(rows),
        )
        for item in batches:
            self.assertEqual(item.byte_count, len(item.payload.encode("utf-8")))
            self.assertLessEqual(item.byte_count, 1_024)

    def test_byte_limit_splits_before_row_limit(self) -> None:
        rows = ({"value": "x" * 20} for _index in range(3))

        batches = tuple(
            iter_encoded_json_batches(rows, max_rows=10, max_bytes=65)
        )

        self.assertEqual(tuple(item.row_count for item in batches), (1, 1, 1))

    def test_single_oversized_row_and_non_json_number_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds the byte limit"):
            tuple(
                iter_encoded_json_batches(
                    ({"value": "x" * 100},),
                    max_rows=10,
                    max_bytes=50,
                )
            )
        with self.assertRaisesRegex(ValueError, "JSON"):
            tuple(
                iter_encoded_json_batches(
                    ({"value": math.nan},),
                    max_rows=10,
                    max_bytes=1_024,
                )
            )


class DuckDbTransportBenchmarkTests(unittest.TestCase):
    def test_benchmark_transports_insert_the_complete_typed_shape(self) -> None:
        observations = run_benchmark(
            row_count=25,
            batch_size=7,
            rounds=1,
        )

        self.assertEqual(
            {item.transport for item in observations},
            {"column_arrays", "typed_json"},
        )
        self.assertTrue(all(item.row_count == 25 for item in observations))
        self.assertTrue(all(item.batch_count == 4 for item in observations))
        self.assertTrue(all(item.transport_bytes > 0 for item in observations))

    def test_quality_benchmark_preserves_count_batches_and_types(self) -> None:
        observations = run_quality_row_benchmark(
            row_count=25,
            batch_size=7,
            rounds=1,
        )

        self.assertEqual(
            {item.transport for item in observations},
            {"column_arrays", "typed_json"},
        )
        self.assertTrue(all(item.row_count == 25 for item in observations))
        self.assertTrue(all(item.batch_count == 4 for item in observations))
        self.assertTrue(all(item.transport_bytes > 0 for item in observations))

    def test_source_benchmark_preserves_entries_links_and_types(self) -> None:
        observations = run_source_accounting_benchmark(
            row_count=31,
            batch_size=7,
            rounds=1,
        )

        self.assertEqual(
            {item.transport for item in observations},
            {"column_arrays", "typed_json"},
        )
        self.assertTrue(all(item.row_count == 31 for item in observations))
        self.assertTrue(
            all(item.related_row_count == 29 for item in observations)
        )
        self.assertTrue(all(item.batch_count == 10 for item in observations))
        self.assertTrue(all(item.transport_bytes > 0 for item in observations))

    def test_impact_benchmark_preserves_count_batches_and_types(self) -> None:
        observations = run_preparation_impact_benchmark(
            row_count=25,
            batch_size=7,
            rounds=1,
        )

        self.assertEqual(
            {item.transport for item in observations},
            {"column_arrays", "typed_json"},
        )
        self.assertTrue(all(item.row_count == 25 for item in observations))
        self.assertTrue(all(item.batch_count == 4 for item in observations))
        self.assertTrue(all(item.transport_bytes > 0 for item in observations))

    def test_canonical_benchmark_preserves_count_batches_and_types(self) -> None:
        observations = run_canonical_row_benchmark(
            row_count=25,
            batch_size=7,
            rounds=1,
        )

        self.assertEqual(
            {item.transport for item in observations},
            {"column_arrays", "typed_json"},
        )
        self.assertTrue(all(item.row_count == 25 for item in observations))
        self.assertTrue(all(item.batch_count == 4 for item in observations))
        self.assertTrue(all(item.transport_bytes > 0 for item in observations))


if __name__ == "__main__":
    unittest.main()
