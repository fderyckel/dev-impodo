"""Fence native Odoo 19/20 transports without retaining local owners."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import (
    DuckDbConnectionFactory,
    _read_databases,
    retain_databases_for_operation,
)
from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.odoo_generation_fence import (
    GenerationFencedOdooReadbackReader,
    GenerationFencedOdooWriteExecutor,
)
from impodo.domain.execution.odoo_readback import OdooReadbackError
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from tests.integration.odoo.test_editable_scalar_transfer import _TransferScenario


class NativeOdooGenerationFenceTests(unittest.TestCase):
    def test_same_destination_and_ids_reset_fences_native_writes_and_verification(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._assert_native_reset_fenced(major, mode)

    def _assert_native_reset_fenced(self, major, mode):
        scenario = _TransferScenario(major, known_inverse=mode == "LOCAL")
        self.assertEqual(scenario.schema.odoo_version, f"{major}.0")
        scope = OdooApiScope(
            "sha256:" + "a" * 64,
            (
                OdooModelScope(
                    scenario.model,
                    write_fields=("code", "kind", "name"),
                    read_fields=("code", "kind", "name"),
                ),
            ),
        )
        values = {"code": "001002", "kind": "asset", "name": "New"}
        current_generation = ["generation-one"]
        expected_generation = current_generation[0]
        requests = []

        def require_current():
            if current_generation[0] != expected_generation:
                raise ValueError("The generation changed")

        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            payload = json.loads(body)
            requests.append(url.rsplit("/", 1)[-1])
            # Simulate an operator reset while the accepted call is
            # returning. URL, database, schema and IDs stay equal.
            current_generation[0] = "generation-two"
            if url.endswith("/load"):
                self.assertEqual(payload["data"][0][1:], ["001002", "asset", "New"])
                return 200, {"ids": [901], "messages": []}
            if url.endswith("/create"):
                return 200, [901]
            return 200, [{"id": 901, **values}]

        config = Json2Config(
            base_url="http://127.0.0.1:8069"
            if mode == "LOCAL"
            else "https://destination.example.test",
            database="destination",
            api_key="secret",
            connection_mode=mode,
            retries=0,
        )
        native_writer = Json2WriteExecutor(config, scope, transport=transport)
        writer = GenerationFencedOdooWriteExecutor(native_writer, require_current)
        native_reader = Json2ReadbackReader(config, scope, transport=transport)
        reader = GenerationFencedOdooReadbackReader(native_reader, require_current)
        original_target_hash = writer.target_hash

        def create():
            if mode == "REMOTE":
                return writer.load_create_rows(
                    scenario.model, (values,), ("impodo.row",)
                )
            return writer.create_rows(scenario.model, (values,))

        with self.assertRaises(OdooWriteOutcomeUnknown):
            create()
        self.assertEqual(requests, ["load" if mode == "REMOTE" else "create"])
        self.assertEqual(writer.target_hash, original_target_hash)
        self.assertEqual(reader.target_hash, original_target_hash)
        with self.assertRaises(OdooWriteRejected):
            create()
        with self.assertRaises(OdooReadbackError):
            reader.read_ids(scenario.model, (901,), ("code", "kind", "name"))
        self.assertEqual(len(requests), 1)
        # A read started before the reset must discard matching
        # values returned by the replacement database as well.
        current_generation[0] = expected_generation
        with self.assertRaises(OdooReadbackError):
            reader.read_ids(scenario.model, (901,), ("code", "kind", "name"))
        self.assertEqual(len(requests), 2)

    def test_local_guard_owners_close_before_transport_and_each_call_rechecks(self):
        with TemporaryDirectory() as directory:
            factory = DuckDbConnectionFactory()
            path = Path(directory) / "binding.duckdb"
            checks = []
            transports = []

            def require_current():
                with (
                    retain_databases_for_operation(),
                    factory.connect(path) as connection,
                ):
                    checks.append(connection.execute("SELECT 1").fetchone())

            def transport(url, headers, body, timeout, method):
                del headers, body, timeout, method
                self.assertIsNone(getattr(_read_databases, "connections", None))
                transports.append(url)
                if url.endswith("/create"):
                    return 200, [42]
                return 200, [{"id": 42, "name": "Reviewed"}]

            scope = OdooApiScope(
                "sha256:" + "a" * 64,
                (
                    OdooModelScope(
                        "res.partner", write_fields=("name",), read_fields=("name",)
                    ),
                ),
            )
            reader = GenerationFencedOdooReadbackReader(
                Json2ReadbackReader(
                    Json2Config(
                        "https://destination.example.test",
                        "destination",
                        "secret",
                        retries=0,
                    ),
                    scope,
                    transport=transport,
                ),
                require_current,
            )
            with collect_duckdb_request_timings() as timings:
                first = reader.read_ids("res.partner", (42,), ("name",))
                second = reader.read_ids("res.partner", (42,), ("name",))
                writer = GenerationFencedOdooWriteExecutor(
                    Json2WriteExecutor(
                        reader.delegate.config, scope, transport=transport
                    ),
                    require_current,
                )
                self.assertEqual(
                    writer.create_rows("res.partner", ({"name": "Reviewed"},)), (42,)
                )
            self.assertEqual(first, second)
            self.assertEqual(len(transports), 3)
            self.assertEqual(len(checks), 6)
            self.assertEqual(timings.connection_count, 6)
            self.assertIsNone(getattr(_read_databases, "connections", None))


if __name__ == "__main__":
    unittest.main()
