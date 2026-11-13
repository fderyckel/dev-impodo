"""Prove bounded remote overlap without changing Odoo snapshot semantics."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from threading import Barrier, Event, Lock, get_ident
from unittest.mock import patch
from urllib.error import URLError

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    observe_json2_read_failures,
)
from impodo.application.shared.performance import OperationPerformance, bind_performance
from impodo.domain.mapping.source_record_identity import SOURCE_EXTERNAL_IDS_FIELD
from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorConfigurationError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
    MetadataRequest,
    RecordRequest,
    bind_snapshot_hashes,
)

NOW = datetime(2026, 11, 13, tzinfo=UTC)


class RecordReadConcurrencyTests(unittest.TestCase):
    def connector(self, transport, **overrides):
        options = {
            "base_url": "https://odoo.example.test", "database": "fictional",
            "api_key": "private-test-token", "page_size": 2, "retries": 0,
            "context": {"allowed_company_ids": [7], "lang": "en_US"},
        }
        options.update(overrides)
        return Json2ReadConnector(Json2Config(**options), transport=transport, now=lambda: NOW)

    def test_two_requests_overlap_keep_pages_sequential_and_pin_one_fingerprint(self):
        for major in (19, 20):
            with self.subTest(major=major):
                barrier, second_done, lock = Barrier(2), Event(), Lock()
                calls, versions, active, peak = [], [], 0, 0

                def transport(url, headers, body, _timeout, _method):
                    nonlocal active, peak
                    if url.endswith("/web/version"):
                        versions.append(url)
                        return 200, {"version": f"{major}.0"}
                    if url.endswith("/fields_get"):
                        return 200, {"name": {"type": "char"}}
                    payload = json.loads(body)
                    key, offset = payload["domain"][0][2], payload["offset"]
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        calls.append((key, offset, get_ident(), dict(headers), payload))
                    try:
                        if offset == 0:
                            barrier.wait(timeout=5)
                            if key == 1:
                                self.assertTrue(second_done.wait(timeout=5))
                        rows = [{"id": key * 10 + offset + 1, "name": "Example"}]
                        if offset == 0:
                            rows.append({"id": key * 10 + 2, "name": "Example"})
                        elif key == 2:
                            second_done.set()
                        return 200, rows
                    finally:
                        with lock:
                            active -= 1

                connector = self.connector(transport)
                metadata = connector.get_model_metadata((MetadataRequest("x.model", ("name",)),))
                collector = OperationPerformance(operation_id="a0000000-0000-4000-8000-000000000001", operation="preflight", configured_workers=2)
                requests = tuple(RecordRequest("x.model", ("name",), (("key", "=", key),)) for key in (2, 1))
                with bind_performance(collector):
                    records = connector.get_records(requests, max_workers=2)
                metadata, records = bind_snapshot_hashes(metadata, records)
                self.assertEqual(metadata.fingerprint, records.fingerprint)
                self.assertEqual(len(versions), 1)
                self.assertEqual(peak, 2)
                self.assertEqual([row.odoo_id for row in records.records["x.model"]], [11, 12, 13, 21, 22, 23])
                for key in (1, 2):
                    pages = [call for call in calls if call[0] == key]
                    self.assertEqual([call[1] for call in pages], [0, 2])
                    self.assertEqual(len({call[2] for call in pages}), 1)
                    self.assertTrue(all(call[4]["context"]["allowed_company_ids"] == [7] for call in pages))
                    self.assertTrue(all(call[4]["fields"] == ["id", "name"] for call in pages))
                counters = dict(collector.snapshot().counters)
                self.assertEqual((counters["rpc_batches"], counters["rpc_pages"], counters["rpc_rows"]), (2, 4, 6))
                self.assertEqual(collector.snapshot().observed_workers, 2)

    def test_serial_and_parallel_overlap_merges_have_identical_hashes(self):
        for major in (19, 20):
            with self.subTest(major=major):
                def transport(url, _headers, body, _timeout, _method):
                    if url.endswith("/web/version"):
                        return 200, {"version": f"{major}.0"}
                    if url.endswith("/fields_get"):
                        return 200, {"name": {"type": "char"}}
                    payload = json.loads(body)
                    return 200, ([{"id": 9, "name": "Shared"}] if payload["offset"] == 0 else [])

                requests = tuple(RecordRequest(model, ("name",), (("key", "=", key),)) for model, key in (("x.other", 1), ("x.model", 2), ("x.model", 1)))
                snapshots = []
                for workers in (1, 2):
                    connector = self.connector(transport)
                    metadata = connector.get_model_metadata((MetadataRequest("x.model", ("name",)),))
                    snapshots.append(bind_snapshot_hashes(metadata, connector.get_records(requests, max_workers=workers)))
                self.assertEqual(snapshots[0], snapshots[1])

    def test_external_id_aliases_merge_and_missing_targets_still_fail(self):
        for major in (19, 20):
            for workers in (1, 2):
                with self.subTest(major=major, workers=workers):
                    missing = False

                    def transport(url, _headers, body, _timeout, _method):
                        if url.endswith("/web/version"):
                            return 200, {"version": f"{major}.0"}
                        payload = json.loads(body)
                        if "/ir.model.data/" in url:
                            name = payload["domain"][-1][2][0]
                            return 200, [{"module": "example", "name": name, "model": "x.model", "res_id": 9}]
                        return 200, [] if missing else [{"id": 9, "name": "Shared"}]

                    requests = tuple(RecordRequest("x.model", ("name",), external_ids=(f"example.{name}",)) for name in ("b", "a"))
                    connector = self.connector(transport)
                    snapshot = connector.get_records(requests, max_workers=workers)
                    self.assertEqual(snapshot.records["x.model"][0].values[SOURCE_EXTERNAL_IDS_FIELD], ("example.a", "example.b"))
                    missing = True
                    with self.assertRaisesRegex(ConnectorIncompleteResultError, "missing or inaccessible"):
                        connector.get_records(requests, max_workers=workers)

    def test_duplicate_pages_and_conflicting_chunks_fail_in_both_modes(self):
        for workers in (1, 2):
            for failure in ("pages", "chunks"):
                with self.subTest(workers=workers, failure=failure):
                    def transport(url, _headers, body, _timeout, _method):
                        if url.endswith("/web/version"):
                            return 200, {"version": "20.0"}
                        payload = json.loads(body)
                        key = payload["domain"][0][2]
                        if failure == "pages":
                            return 200, ([{"id": 1, "name": "Same"}] if payload["offset"] == 0 else [])
                        return 200, [{"id": 1, "name": str(key)}]

                    connector = self.connector(transport, page_size=1 if failure == "pages" else 2)
                    if failure == "pages":
                        def repeated(url, *args):
                            if url.endswith("/web/version"):
                                return 200, {"version": "20.0"}
                            payload = json.loads(args[1])
                            return 200, ([{"id": 1, "name": "Same"}] if payload["offset"] < 2 else [])
                        connector = self.connector(repeated, page_size=1)
                    requests = tuple(RecordRequest("x.model", ("name",), (("key", "=", key),)) for key in (1, 2))
                    with self.assertRaisesRegex(ConnectorIncompleteResultError, "pagination repeated" if failure == "pages" else "chunks conflict"):
                        connector.get_records(requests, max_workers=workers)

    def test_window_never_submits_an_unbounded_queue(self):
        barrier, second_done = Barrier(2), Event()
        entered = []

        def transport(url, _headers, body, _timeout, _method):
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            key = json.loads(body)["domain"][0][2]
            entered.append(key)
            barrier.wait(timeout=5)
            if key == 2:
                second_done.set()
            return 200, []

        connector = self.connector(transport)
        requests = tuple(RecordRequest("x.model", ("name",), (("key", "=", key),)) for key in range(1, 101))
        outcomes = connector._record_read_outcomes(requests, frozenset(), 2)
        try:
            next(outcomes)
            self.assertTrue(second_done.wait(timeout=5))
            self.assertCountEqual(entered, [1, 2])
        finally:
            outcomes.close()

    def test_ordered_errors_stop_submissions_and_replay_worker_diagnostics(self):
        for major in (19, 20):
            for first_status in (200, 503):
                with self.subTest(major=major, first_status=first_status):
                    second_finished, calls, observed = Event(), [], []
                    original = Json2ReadConnector._run_record_request

                    def run(connector, request, *args):
                        outcome = original(connector, request, *args)
                        if request.model == "x.b":
                            second_finished.set()
                        return outcome

                    def transport(url, _headers, _body, _timeout, _method):
                        if url.endswith("/web/version"):
                            return 200, {"version": f"{major}.0"}
                        calls.append(url)
                        if "/x.a/" in url:
                            self.assertTrue(second_finished.wait(timeout=5))
                            return first_status, []
                        return 401, {}

                    requests = tuple(RecordRequest(f"x.{letter}", ("name",)) for letter in "abcdef")
                    expected = ConnectorAuthenticationError if first_status == 200 else ConnectorTransportError
                    with patch.object(Json2ReadConnector, "_run_record_request", run), observe_json2_read_failures(observed.append):
                        with self.assertRaisesRegex(expected, "401" if first_status == 200 else "503"):
                            self.connector(transport).get_records(requests, max_workers=2)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual([failure.model for failure in observed], ["x.b"] if first_status == 200 else ["x.a", "x.b"])

    def test_each_worker_retains_transient_retries_and_aggregate_counts(self):
        for major in (19, 20):
            with self.subTest(major=major):
                lock, attempts = Lock(), {}

                def transport(url, _headers, _body, _timeout, _method):
                    if url.endswith("/web/version"):
                        return 200, {"version": f"{major}.0"}
                    with lock:
                        attempt = attempts[url] = attempts.get(url, 0) + 1
                    if attempt == 1:
                        if "/x.a/" in url:
                            return 429, {}
                        raise URLError("fictional network interruption")
                    return 200, []

                collector = OperationPerformance(operation_id="a0000000-0000-4000-8000-000000000002", operation="preflight", configured_workers=2)
                requests = tuple(RecordRequest(model, ("name",)) for model in ("x.a", "x.b"))
                with bind_performance(collector), patch("impodo.adapters.odoo.connectors.time.sleep"):
                    snapshot = self.connector(transport, retries=1).get_records(requests, max_workers=2)
                counts = dict(collector.snapshot().counters)
                self.assertEqual(snapshot.records, {"x.a": (), "x.b": ()})
                self.assertEqual((counts["rpc_calls"], counts["rpc_retries"], counts["rpc_batches"]), (5, 2, 2))
                self.assertEqual(counts.get("rpc_failures", 0), 0)

    def test_invalid_worker_count_never_contacts_odoo(self):
        for workers in (0, 5, True, 1.5):
            with self.subTest(workers=workers):
                def transport(*_args):
                    self.fail("Invalid concurrency contacted Odoo")
                with self.assertRaises(ConnectorConfigurationError):
                    self.connector(transport).get_records((), max_workers=workers)
