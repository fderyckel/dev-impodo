"""Verify read throttling, shared cooldowns and complete metadata recovery."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from io import BytesIO
from itertools import product
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    observe_json2_read_failures,
)
from impodo.adapters.odoo.request_pacing import Json2RequestPacer, retry_after_seconds
from impodo.application.shared.performance import OperationPerformance, bind_performance
from impodo.domain.odoo.contracts import ConnectorTransportError, MetadataRequest
from tests.support.odoo_read_clock import ReadClock as Clock

NOW = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)


class Response(BytesIO):
    """Supply an ordinary bounded JSON HTTP response to the real transport."""

    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


class Json2ReadPacingTests(unittest.TestCase):
    def connector(
        self, pacer, *, retries=2, database="private-db", transport=None, mode="REMOTE"
    ):
        return Json2ReadConnector(
            Json2Config(
                base_url=(
                    "https://private-host.example.test"
                    if mode == "REMOTE"
                    else "http://127.0.0.1:8069"
                ),
                database=database,
                api_key="private-key",
                retries=retries,
                connection_mode=mode,
            ),
            request_pacer=pacer,
            now=lambda: NOW,
            transport=transport,
        )

    def test_retry_after_accepts_seconds_and_dates_and_rejects_arbitrary_text(self):
        for value, expected in (
            ("10", 10.0),
            (format_datetime(NOW + timedelta(seconds=15), usegmt=True), 15.0),
            (format_datetime(NOW - timedelta(seconds=15), usegmt=True), 0.0),
            (None, None),
            ("-10", None),
            ("NaN", None),
            ("1.5", None),
            ("private-server-message", None),
            ("9" * 129, None),
        ):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value, now=NOW), expected)

    def test_metadata_retry_keeps_prior_models_for_both_odoo_majors(self):
        for major, mode in product((19, 20), ("LOCAL", "REMOTE")):
            with self.subTest(major=major, mode=mode):
                clock = Clock()
                connector = self.connector(clock.pacer(), mode=mode)
                calls = []

                def open_request(request, *, timeout):
                    path = request.full_url.rsplit("/json/2/", 1)[-1]
                    calls.append((path, clock.elapsed))
                    if request.full_url.endswith("/web/version"):
                        return Response({"version": f"{major}.0"})
                    if (
                        path == "x.second/fields_get"
                        and sum(name == path for name, _when in calls) == 1
                    ):
                        raise HTTPError(
                            request.full_url,
                            429,
                            "private-body",
                            {"Retry-After": "10"},
                            BytesIO(b"private response"),
                        )
                    return Response({"name": {"type": "char", "string": "Name"}})

                with patch("impodo.adapters.odoo.connectors.build_opener") as opener:
                    opener.return_value.open.side_effect = open_request
                    snapshot = connector.get_model_metadata(
                        [
                            MetadataRequest("x.first", ("name",)),
                            MetadataRequest("x.second", ("name",)),
                        ],
                        include_create_defaults=False,
                    )
                self.assertEqual(set(snapshot.models), {"x.first", "x.second"})
                self.assertTrue(snapshot.complete)
                self.assertEqual(snapshot.fingerprint.odoo_version, f"{major}.0")
                self.assertEqual(
                    [name for name, _when in calls if "/fields_get" in name],
                    [
                        "x.first/fields_get",
                        "x.second/fields_get",
                        "x.second/fields_get",
                    ],
                )
                self.assertEqual(clock.waits, [10.0])

    def test_repeated_429_uses_seconds_backoff_and_an_actionable_safe_error(self):
        for major in (19, 20):
            with self.subTest(major=major):
                clock = Clock()
                calls = []

                def transport(*_args):
                    calls.append(clock.elapsed)
                    return 429, {"private": "private-body-and-token"}

                connector = self.connector(clock.pacer(), transport=transport)
                failures = []
                with (
                    observe_json2_read_failures(failures.append),
                    self.assertRaises(ConnectorTransportError) as caught,
                ):
                    connector._post_read_method("account.tax", "has_access", {})
                self.assertEqual(calls, [0.0, 2.0, 6.0])
                self.assertEqual(clock.waits, [2.0, 4.0])
                self.assertEqual(failures[0].attempt_count, 3)
                self.assertIn("HTTP 429", str(caught.exception))
                self.assertIn("Wait before repeating", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))

    def test_shared_cooldown_survives_new_connector_and_keeps_other_targets_free(self):
        clock = Clock()
        pacer = clock.pacer()
        first = self.connector(pacer, retries=0, transport=lambda *_: (429, None))
        with self.assertRaises(ConnectorTransportError):
            first._post_read_method("x.first", "fields_get", {})
        other = self.connector(
            pacer, database="another-db", transport=lambda *_: (200, {})
        )
        other._post_read_method("x.first", "fields_get", {})
        self.assertEqual(clock.elapsed, 0.0)
        following = self.connector(pacer, transport=lambda *_: (200, {}))
        following._post_read_method("x.first", "fields_get", {})
        following._post_read_method("x.second", "fields_get", {})
        self.assertEqual(clock.waits, [2.0, 1.0])
        clock.elapsed = 100.0
        following._post_read_method("x.third", "fields_get", {})
        self.assertEqual(clock.elapsed, 100.0)

    def test_long_retry_after_never_sends_an_early_retry_and_counts_zero_new_calls(
        self,
    ):
        clock = Clock()
        pacer = clock.pacer()
        failures = []
        first = self.connector(pacer)
        performance = OperationPerformance(
            operation_id="a0000000-0000-4000-8000-000000000001",
            operation="preflight",
        )

        def unavailable(request, **_kwargs):
            raise HTTPError(
                request.full_url, 429, "private", {"Retry-After": "300"}, BytesIO()
            )

        with (
            patch("impodo.adapters.odoo.connectors.build_opener") as opener,
            observe_json2_read_failures(failures.append),
            bind_performance(performance),
        ):
            opener.return_value.open.side_effect = unavailable
            with self.assertRaises(ConnectorTransportError):
                first._post_read_method("x.first", "fields_get", {})
            self.assertEqual(opener.return_value.open.call_count, 1)
            following = self.connector(pacer)
            with self.assertRaisesRegex(ConnectorTransportError, "without sending"):
                following._post_read_method("x.first", "fields_get", {})
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual([item.attempt_count for item in failures], [1, 0])
        self.assertEqual(clock.waits, [])
        counters = dict(performance.snapshot().counters)
        self.assertEqual(counters["rpc_calls"], 1)
        self.assertEqual(counters["rpc_failures"], 1)

    def test_new_throttle_while_waiting_extends_the_shared_cooldown(self):
        clock = Clock()
        interrupted = False

        def sleep(seconds):
            nonlocal interrupted
            clock.sleep(seconds)
            if not interrupted:
                interrupted = True
                pacer.defer("target", attempt=1, retry_after=None)

        pacer = Json2RequestPacer(clock=clock.time, sleep=sleep, jitter=lambda: 0.0)
        pacer.defer("target", attempt=0, retry_after=None)
        self.assertTrue(pacer.wait("target", deadline=120.0))
        self.assertEqual(clock.waits, [2.0, 4.0])

    def test_active_cooldowns_are_not_evicted_when_target_capacity_is_reached(self):
        clock = Clock()
        pacer = clock.pacer()
        for index in range(300):
            pacer.defer(str(index), attempt=0, retry_after=300.0)
        for target in ("0", "299"):
            self.assertFalse(pacer.wait(target, deadline=120.0))
        self.assertEqual(clock.waits, [])

    def test_network_failure_keeps_its_cause_when_the_wait_budget_expires(self):
        clock = Clock()

        def transport(*_args):
            clock.elapsed += 120.0
            raise URLError("private-host")

        connector = self.connector(clock.pacer(), transport=transport)
        with (
            patch("impodo.adapters.odoo.connectors.time.sleep"),
            self.assertRaisesRegex(
                ConnectorTransportError, "unreachable after 1 attempt"
            ),
        ):
            connector._post_read_method("x.first", "fields_get", {})

    def test_expiry_before_any_send_does_not_invent_an_http_429(self):
        pacer = Json2RequestPacer(clock=Mock(side_effect=[0.0, 121.0, 121.0]))
        transport = Mock(return_value=(200, {}))
        connector = self.connector(pacer, transport=transport)
        failures = []
        with (
            observe_json2_read_failures(failures.append),
            self.assertRaisesRegex(ConnectorTransportError, "retry window ended"),
        ):
            connector._post_read_method("x.first", "fields_get", {})
        transport.assert_not_called()
        self.assertEqual(failures, [])
