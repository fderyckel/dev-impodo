import json
from types import SimpleNamespace
import unittest

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.web.composition.target_writers import _readback_reader, _write_executor


class Json2ReadbackRetryTests(unittest.TestCase):
    def test_load_composition_retries_verification_and_identity_reads(self):
        workspace = SimpleNamespace(
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_disposable",
            odoo_connection_mode=SimpleNamespace(value="LOCAL"),
        )
        scope = OdooApiScope(
            preview_hash="sha256:" + "a" * 64,
            models=(
                OdooModelScope(
                    "res.partner",
                    write_fields=("name",),
                    read_fields=("name",),
                ),
            ),
        )
        reader = _readback_reader(workspace, "secret", scope)
        writer = _write_executor(workspace, "secret", scope)
        calls = 0

        def transport(url, headers, body, timeout, method):
            nonlocal calls
            del url, headers, body, timeout, method
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary read timeout")
            return 200, [{"id": 42, "name": "Verified"}]

        reader.transport = transport
        result = reader.read_ids("res.partner", (42,), ("name",))

        self.assertEqual(result[0].values, {"name": "Verified"})
        self.assertEqual(calls, 2)
        self.assertEqual(writer.config.retries, 2)

    def test_retries_a_transient_readback_timeout(self):
        calls = 0

        def transport(url, headers, body, timeout, method):
            nonlocal calls
            del url, headers, timeout, method
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary read timeout")
            payload = json.loads(body)
            return 200, [{"id": 42, "name": "Verified"}]

        reader = Json2ReadbackReader(
            Json2Config(
                base_url="http://127.0.0.1:8069",
                database="odoo19_disposable",
                api_key="secret",
                connection_mode="LOCAL",
                retries=1,
            ),
            OdooApiScope(
                preview_hash="sha256:" + "a" * 64,
                models=(
                    OdooModelScope(
                        "res.partner",
                        write_fields=("name",),
                        read_fields=("name",),
                    ),
                ),
            ),
            transport=transport,
        )

        result = reader.read_ids("res.partner", (42,), ("name",))

        self.assertEqual(result[0].values, {"name": "Verified"})
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
