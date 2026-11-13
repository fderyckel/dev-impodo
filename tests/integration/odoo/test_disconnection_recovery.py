"""Dropped HTTP connections retry reads and never replay reviewed writes."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from http.client import IncompleteRead, RemoteDisconnected
from unittest.mock import patch
from urllib.error import URLError

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    observe_json2_read_failures,
)
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.odoo.contracts import ConnectorTransportError


class Json2DisconnectionRecoveryTests(unittest.TestCase):
    def _config(self, major, mode):
        return Json2Config(
            base_url="http://127.0.0.1:8069"
            if mode == "LOCAL"
            else "https://odoo.example.test",
            database=f"odoo{major}_test",
            api_key="private-api-key",
            connection_mode=mode,
            retries=2,
        )

    def _scope(self):
        return OdooApiScope(
            preview_hash="sha256:" + "a" * 64,
            models=(
                OdooModelScope(
                    "res.partner",
                    write_fields=("name",),
                    read_fields=("name",),
                    lookup_fields=("ref",),
                ),
            ),
        )

    def test_dropped_lookup_and_verification_reads_retry_for_both_majors(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for error_type in (
                    RemoteDisconnected,
                    ConnectionResetError,
                    IncompleteRead,
                ):
                    for kind in ("lookup", "verification", "authoring"):
                        with self.subTest(
                            major=major, mode=mode, error=error_type, kind=kind
                        ):
                            calls = []

                            def transport(url, _headers, body, _timeout, _method):
                                calls.append(json.loads(body))
                                if len(calls) == 1:
                                    raise error_type("private-server-text")
                                return 200, [
                                    {"id": 42, "name": "Verified", "ref": "C1"}
                                ]

                            config = self._config(major, mode)
                            with (
                                patch("impodo.adapters.odoo.writer.time.sleep"),
                                patch("impodo.adapters.odoo.readback.time.sleep"),
                                patch("impodo.adapters.odoo.connectors.time.sleep"),
                            ):
                                if kind == "lookup":
                                    writer = Json2WriteExecutor(
                                        config, self._scope(), transport
                                    )
                                    result = writer.find_ids_many(
                                        "res.partner", ((("ref", "=", "C1"),),)
                                    )
                                    self.assertEqual(result, ((42,),))
                                elif kind == "verification":
                                    reader = Json2ReadbackReader(
                                        config, self._scope(), transport
                                    )
                                    result = reader.read_ids(
                                        "res.partner", (42,), ("name",)
                                    )
                                    self.assertEqual(
                                        result[0].values, {"name": "Verified"}
                                    )
                                else:
                                    reader = Json2ReadConnector(
                                        config, transport=transport
                                    )
                                    result = reader._post_read_method(
                                        "res.partner", "search_read", {}
                                    )
                                    self.assertEqual(result[0]["id"], 42)
                            self.assertEqual(len(calls), 2)
                            self.assertEqual(calls[0], calls[1])

    def test_exhausted_identity_lookup_explains_disconnection_and_attempts(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    calls = []

                    def transport(*_args):
                        calls.append(1)
                        raise RemoteDisconnected("private-server-text private-api-key")

                    writer = Json2WriteExecutor(
                        self._config(major, mode), self._scope(), transport
                    )
                    with (
                        patch("impodo.adapters.odoo.writer.time.sleep"),
                        self.assertRaises(OdooWriteRejected) as caught,
                    ):
                        writer.find_ids_many("res.partner", ((("ref", "=", "C1"),),))
                    self.assertEqual(len(calls), 3)
                    self.assertIn(
                        "connection to Odoo was interrupted", str(caught.exception)
                    )
                    self.assertIn("checking existing records", str(caught.exception))
                    self.assertIn("3 attempts", str(caught.exception))
                    self.assertEqual(
                        caught.exception.load_failure_code,
                        "LOAD_ODOO_IDENTITY_LOOKUP_UNAVAILABLE",
                    )
                    self.assertNotIn("private", str(caught.exception))

    def test_exhausted_authoring_read_keeps_safe_transport_diagnostics(self):
        for major in (19, 20):
            with self.subTest(major=major):
                config = self._config(major, "REMOTE")
                failures = []
                reader = Json2ReadConnector(
                    config,
                    transport=lambda *_args: (_ for _ in ()).throw(
                        RemoteDisconnected("private")
                    ),
                )
                with (
                    patch("impodo.adapters.odoo.connectors.time.sleep"),
                    observe_json2_read_failures(failures.append),
                    self.assertRaises(ConnectorTransportError) as caught,
                ):
                    reader._post_read_method("res.partner", "search_read", {})
                self.assertEqual(failures[0].attempt_count, 3)
                self.assertEqual(failures[0].category, "unreachable")
                self.assertNotIn("private", str(caught.exception))

    def test_dropped_write_response_is_unknown_and_never_retried(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                for operation in ("create", "load", "write"):
                    for error_type in (
                        RemoteDisconnected,
                        ConnectionResetError,
                        IncompleteRead,
                    ):
                        with self.subTest(
                            major=major,
                            mode=mode,
                            operation=operation,
                            error=error_type,
                        ):
                            calls = []

                            def transport(*_args):
                                calls.append(1)
                                raise error_type("private-server-text")

                            writer = Json2WriteExecutor(
                                self._config(major, mode), self._scope(), transport
                            )
                            with (
                                patch(
                                    "impodo.adapters.odoo.writer.time.sleep"
                                ) as sleep,
                                self.assertRaises(OdooWriteOutcomeUnknown) as caught,
                            ):
                                if operation == "create":
                                    writer.create_rows(
                                        "res.partner", ({"name": "Contact"},)
                                    )
                                elif operation == "load":
                                    writer.load_create_rows(
                                        "res.partner",
                                        ({"name": "Contact"},),
                                        ("impodo_test.contact",),
                                    )
                                else:
                                    writer.update_row(
                                        "res.partner", 42, {"name": "Contact"}
                                    )
                            self.assertEqual(len(calls), 1)
                            sleep.assert_not_called()
                            self.assertNotIn("private", str(caught.exception))

    def test_lookup_timeout_is_distinct_from_a_lost_connection(self):
        def transport(*_args):
            raise URLError(TimeoutError("private"))

        writer = Json2WriteExecutor(
            replace(self._config(20, "REMOTE"), retries=0), self._scope(), transport
        )
        with self.assertRaises(OdooWriteRejected) as caught:
            writer.find_ids("res.partner", (("ref", "=", "C1"),))
        self.assertIn("timed out", str(caught.exception))
        self.assertIn("1 attempt", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
