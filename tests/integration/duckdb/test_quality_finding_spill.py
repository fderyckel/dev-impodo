"""Keep numeric findings complete and wide publication pages bounded."""

import json
import unittest
from unittest.mock import patch

from impodo.adapters.duckdb.preparation_quality_index import PreparationQualityIndex
from impodo.adapters.duckdb.preparation_quality_spill import quality_finding_store
from impodo.adapters.duckdb.quality_repository import _quality_evidence_batches
from impodo.domain.target_numeric_precision import PreparedNumericField
from tests.integration.duckdb import test_numeric_precision_streaming as numeric


class QualityFindingSpillTests(unittest.TestCase):
    def test_numeric_failures_exceed_total_allowance_without_losing_lineage(self):
        case = numeric.NumericPrecisionStreamingTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case._seed([{"amount": "0.003"} for _ in range(513)], physical_copies=2)
        specs = (PreparedNumericField("rows", numeric.MODEL, "amount", (16, 2)),)
        expected = case._findings(specs)
        with (
            quality_finding_store() as store,
            patch(
                "impodo.adapters.duckdb.preparation_quality_index.QUALITY_FINDING_INDEX_MAX_BYTES",
                1024,
            ),
        ):
            actual = PreparationQualityIndex._bounded_numeric_precision_rows(
                case.host,
                case.recording,
                numeric.WORKSPACE_ID,
                numeric.SESSION_ID,
                specs,
                finding_store=store,
            )
            self.assertEqual(len(actual), 1026)
            self.assertEqual(tuple(actual), expected)
            self.assertNotIsInstance(actual, (list, tuple))

    def test_wide_findings_replay_in_canonical_byte_bounded_publication_pages(self):
        encode = lambda value: json.dumps(value, ensure_ascii=False)
        with quality_finding_store() as store:
            values = store.mapping(
                "wide",
                encode_key=str,
                decode_key=str,
                encode_value=encode,
                decode_value=json.loads,
            )
            for index in reversed(range(257)):
                values[f"{index:04d}"] = f"{index:04d}:" + "é" * 16_384
            ordered = store.values("wide", decode_value=json.loads)
            observed = []
            pages = 0
            for start, batch in _quality_evidence_batches(ordered):
                self.assertEqual(start, len(observed))
                self.assertLessEqual(
                    sum(len(encode(value).encode("utf-8")) for value in batch),
                    2 * 1024 * 1024,
                )
                observed.extend(value[:4] for value in batch)
                pages += 1
            self.assertGreater(pages, 1)
            self.assertEqual(observed, [f"{index:04d}" for index in range(257)])
