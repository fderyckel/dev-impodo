"""Keep load admission, uncertain writes and read-back on one target clock."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from io import BytesIO
from itertools import product
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.odoo_generation_fence import GenerationFencedOdooWriteExecutor
from impodo.application.shared.remote_wait import observe_remote_wait
from impodo.application.workspace.execution.load_transport import (
    create_transport_results,
)
from impodo.domain.execution.odoo_readback import OdooReadbackError
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    OdooRequestDeferred,
    OdooWriteOutcomeUnknown,
)
from impodo.domain.odoo.contracts import ConnectorTransportError
from tests.application.workspace.execution.test_service import _snapshot
from tests.support.odoo_read_clock import ReadClock

SCOPE = OdooApiScope(
    preview_hash="sha256:" + "1" * 64,
    models=(OdooModelScope("res.partner", ("name",), ("name",), ("ref",)),),
)


def config(major=20, mode="REMOTE", **kwargs):
    return Json2Config(
        base_url="https://odoo.example.test"
        if mode == "REMOTE"
        else "http://127.0.0.1:8069",
        database=f"synthetic-{major}",
        api_key="synthetic",
        connection_mode=mode,
        **kwargs,
    )


class Response(BytesIO):
    status = 200

    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class LoadRequestPacingTests(unittest.TestCase):
    def test_checking_cooldown_governs_load_lookup_write_and_readback(self):
        for major, mode in product((19, 20), ("LOCAL", "REMOTE")):
            with self.subTest(major=major, mode=mode):
                clock = ReadClock()
                pacer = clock.pacer()
                cfg = config(major, mode)
                checker = Json2ReadConnector(
                    cfg,
                    request_pacer=pacer,
                    transport=lambda *_: (429, None),
                )
                with self.assertRaises(ConnectorTransportError):
                    checker._post_read_method("res.partner", "fields_get", {})
                calls = []

                def transport(url, *_):
                    calls.append((url.rsplit("/", 1)[-1], clock.elapsed))
                    if url.endswith("/load"):
                        return 200, {"ids": [101], "messages": []}
                    return 200, [{"id": 101, "name": "Synthetic"}]

                writer = Json2WriteExecutor(cfg, SCOPE, transport, request_pacer=pacer)
                self.assertEqual(
                    writer.find_ids("res.partner", (("ref", "=", "A"),)), (101,)
                )
                self.assertEqual(
                    writer.load_create_rows(
                        "res.partner", ({"name": "Synthetic"},), ("synthetic.a",)
                    ),
                    (101,),
                )
                reader = Json2ReadbackReader(cfg, SCOPE, transport, request_pacer=pacer)
                self.assertEqual(
                    reader.read_ids("res.partner", (101,), ("name",))[0].odoo_id, 101
                )
                self.assertEqual(
                    calls,
                    [("search_read", 14.0), ("load", 18.0), ("search_read", 22.0)],
                )

    def test_write_429_publishes_retry_after_without_resending(self):
        for major, mode in product((19, 20), ("LOCAL", "REMOTE")):
            with self.subTest(major=major, mode=mode):
                clock = ReadClock()
                pacer = clock.pacer()
                cfg = config(major, mode)
                calls = []

                def open_request(request, **_kwargs):
                    calls.append((request.full_url.rsplit("/", 1)[-1], clock.elapsed))
                    if request.full_url.endswith("/load"):
                        raise HTTPError(
                            request.full_url,
                            429,
                            "private",
                            {"Retry-After": "10"},
                            BytesIO(b"private"),
                        )
                    return Response([{"id": 101, "name": "Synthetic"}])

                with patch("impodo.adapters.odoo.connectors.build_opener") as opener:
                    opener.return_value.open.side_effect = open_request
                    writer = Json2WriteExecutor(cfg, SCOPE, request_pacer=pacer)
                    with self.assertRaises(OdooWriteOutcomeUnknown) as caught:
                        writer.load_create_rows(
                            "res.partner", ({"name": "Synthetic"},), ("synthetic.a",)
                        )
                    self.assertEqual(caught.exception.diagnostic.http_status, 429)
                    self.assertNotIn("private", str(caught.exception))
                    reader = Json2ReadbackReader(cfg, SCOPE, request_pacer=pacer)
                    reader.read_ids("res.partner", (101,), ("name",))
                self.assertEqual(calls, [("load", 0.0), ("search_read", 10.0)])

    def test_long_verification_cooldown_defers_unsent_write_before_journal(self):
        clock = ReadClock()
        pacer = clock.pacer()
        cfg = config()
        reader = Json2ReadbackReader(cfg, SCOPE, request_pacer=pacer)
        with patch("impodo.adapters.odoo.connectors.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError(
                "https://odoo.example.test",
                429,
                "private",
                {"Retry-After": "300"},
                BytesIO(),
            )
            with self.assertRaisesRegex(OdooReadbackError, "cooldown"):
                reader.read_ids("res.partner", (101,), ("name",))
            self.assertEqual(opener.return_value.open.call_count, 1)
        transport = Mock(return_value=(200, {"ids": [101], "messages": []}))
        writer = Json2WriteExecutor(cfg, SCOPE, transport, request_pacer=pacer)
        fenced = GenerationFencedOdooWriteExecutor(writer, lambda: None)
        begin = Mock()
        with self.assertRaises(OdooRequestDeferred):
            list(
                create_transport_results(
                    ((_snapshot().rows[0], {"name": "Synthetic"}, ()),),
                    model="res.partner",
                    executor=fenced,
                    import_create=True,
                    continuation=True,
                    begin=begin,
                    rolled_back=Mock(),
                )
            )
        begin.assert_not_called()
        transport.assert_not_called()
        other = Json2WriteExecutor(
            replace(cfg, database="other"), SCOPE, transport, request_pacer=pacer
        )
        self.assertEqual(
            other.load_create_rows(
                "res.partner", ({"name": "Synthetic"},), ("synthetic.a",)
            ),
            (101,),
        )
        self.assertEqual(clock.waits, [])

    def test_remote_spacing_starts_before_throttling_and_admission_is_consumed_once(
        self,
    ):
        clock = ReadClock()
        calls = []
        cfg = config(minimum_request_interval_seconds=1.0)

        def transport(*_):
            calls.append(clock.elapsed)
            return 200, {"ids": [101], "messages": []}

        writer = Json2WriteExecutor(cfg, SCOPE, transport, request_pacer=clock.pacer())
        for _ in range(3):
            writer.admit_write()
            writer.load_create_rows(
                "res.partner", ({"name": "Synthetic"},), ("synthetic.a",)
            )
        self.assertEqual(calls, [0.0, 1.0, 2.0])
        self.assertEqual(clock.waits, [1.0, 1.0])

    def test_progress_and_cancellation_interrupt_wait_before_send(self):
        clock = ReadClock()
        pacer = clock.pacer()
        cfg = config()
        transport = Mock()
        writer = Json2WriteExecutor(cfg, SCOPE, transport, request_pacer=pacer)
        pacer.defer(writer.target_hash, attempt=0, retry_after=10.0)
        observed = []

        def observe(seconds):
            observed.append(seconds)
            if seconds is not None and clock.elapsed >= 1:
                raise InterruptedError("synthetic stop")

        with observe_remote_wait(observe), self.assertRaises(InterruptedError):
            writer.admit_write()
        self.assertEqual(clock.waits, [1.0])
        self.assertEqual(observed, [10.0, 9.0, None])
        transport.assert_not_called()

    def test_failed_lookup_uses_shared_backoff_without_repeating_a_write(self):
        clock = ReadClock()
        calls = []

        def transport(*_):
            calls.append(clock.elapsed)
            return (429, None) if len(calls) < 3 else (200, [{"id": 101}])

        writer = Json2WriteExecutor(
            config(), SCOPE, transport, request_pacer=clock.pacer()
        )
        self.assertEqual(writer.find_ids("res.partner", (("ref", "=", "A"),)), (101,))
        self.assertEqual(calls, [0.0, 2.0, 6.0])

    def test_adaptive_slowdown_expires_during_a_continuing_paced_load(self):
        clock = ReadClock()
        pacer = clock.pacer()
        for _ in range(3):
            pacer.defer("target", attempt=0, retry_after=2.0)
        for _ in range(30):
            self.assertTrue(
                pacer.wait("target", deadline=clock.elapsed + 120, minimum_interval=1.0)
            )
        before = clock.elapsed
        self.assertTrue(
            pacer.wait("target", deadline=clock.elapsed + 120, minimum_interval=1.0)
        )
        self.assertEqual(clock.elapsed - before, 1.0)
