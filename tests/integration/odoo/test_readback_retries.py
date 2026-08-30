import json
import unittest

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope


class Json2ReadbackRetryTests(unittest.TestCase):
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
