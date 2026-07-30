from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from impodo.connectors import (
    ConnectorConfigurationError,
    ConnectorTransportError,
    Json2Config,
    Json2ReadConnector,
    MetadataRequest,
    RecordRequest,
    _NoRedirectHandler,
)


class Json2ConnectorTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "base_url": "https://odoo.example.test",
            "database": "odoo_test",
            "api_key": "super-secret-token",
            "environment": "TEST",
            "timeout_seconds": 0.1,
            "page_size": 2,
            "retries": 1,
        }
        values.update(overrides)
        return Json2Config(**values)

    def test_official_endpoint_headers_and_pagination(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            payload = json.loads(body) if body else None
            calls.append((url, dict(headers), payload, timeout, method))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            offset = payload["offset"]
            pages = {
                0: [{"id": 1, "code": "A"}, {"id": 2, "code": "B"}],
                2: [{"id": 3, "code": "C"}, {"id": 4, "code": "D"}],
                4: [{"id": 5, "code": "E"}],
            }
            return 200, pages[offset]

        connector = Json2ReadConnector(
            self.config(),
            transport=transport,
            now=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
        snapshot = connector.get_records(
            [RecordRequest("x.model", ("code",), (["active", "=", True],))]
        )
        self.assertEqual(len(snapshot.records["x.model"]), 5)
        post_calls = [call for call in calls if "/json/2/" in call[0]]
        self.assertEqual(
            [call[2]["offset"] for call in post_calls],
            [0, 2, 4],
        )
        self.assertTrue(
            all(call[0].endswith("/json/2/x.model/search_read") for call in post_calls)
        )
        self.assertTrue(
            all(
                call[1]["Authorization"] == "bearer super-secret-token"
                for call in post_calls
            )
        )
        self.assertTrue(
            all(call[1]["X-Odoo-Database"] == "odoo_test" for call in post_calls)
        )
        self.assertTrue(all(call[2]["order"] == "id asc" for call in post_calls))

    def test_fields_get_uses_named_json2_arguments(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            return 200, {
                "name": {
                    "type": "char",
                    "required": True,
                    "readonly": False,
                }
            }

        connector = Json2ReadConnector(self.config(), transport=transport)
        snapshot = connector.get_model_metadata(
            [MetadataRequest("x.model", ("name",))]
        )
        self.assertEqual(snapshot.models["x.model"].fields["name"].type, "char")
        fields_call = calls[-1]
        self.assertTrue(fields_call[0].endswith("/json/2/x.model/fields_get"))
        self.assertEqual(fields_call[1]["allfields"], ["name"])
        self.assertIn("attributes", fields_call[1])

    def test_schema_discovery_requests_all_fields_once_per_model(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            return 200, {
                "name": {
                    "string": "Name",
                    "type": "char",
                    "required": True,
                    "readonly": False,
                },
                "display_name": {
                    "string": "Display Name",
                    "type": "char",
                    "readonly": True,
                },
            }

        connector = Json2ReadConnector(self.config(), transport=transport)
        snapshot = connector.get_model_metadata(
            [MetadataRequest("x.model", (), all_fields=True)]
        )

        fields_call = calls[-1]
        self.assertEqual(fields_call[1]["allfields"], [])
        self.assertEqual(
            set(snapshot.models["x.model"].fields),
            {"name", "display_name"},
        )
        self.assertEqual(
            snapshot.models["x.model"].fields["name"].label,
            "Name",
        )

    def test_timeout_error_is_redacted(self) -> None:
        def transport(url, headers, body, timeout, method):
            raise TimeoutError("contains super-secret-token")

        connector = Json2ReadConnector(self.config(), transport=transport)
        with self.assertRaises(ConnectorTransportError) as caught:
            connector.get_model_metadata(
                [MetadataRequest("x.model", ("name",))]
            )
        self.assertNotIn("super-secret-token", str(caught.exception))

    def test_connector_has_no_public_write_or_generic_rpc_surface(self) -> None:
        public = {
            name
            for name in dir(Json2ReadConnector)
            if not name.startswith("_")
        }
        forbidden = {
            "create",
            "write",
            "unlink",
            "import_data",
            "execute_kw",
            "call",
            "execute",
        }
        self.assertTrue(public.isdisjoint(forbidden))
        self.assertEqual(
            public,
            {
                "get_environment_fingerprint",
                "get_model_metadata",
                "get_records",
            },
        )

    def test_config_repr_redacts_api_key(self) -> None:
        self.assertNotIn("super-secret-token", repr(self.config()))

    def test_http_is_allowed_only_for_explicit_literal_loopback_mode(self) -> None:
        local = self.config(
            base_url="http://127.0.0.1:8069",
            environment="DEV",
            allow_insecure_loopback=True,
        )
        self.assertTrue(local.allow_insecure_loopback)

        rejected = (
            {
                "base_url": "http://127.0.0.1:8069",
                "allow_insecure_loopback": False,
            },
            {
                "base_url": "http://localhost:8069",
                "allow_insecure_loopback": True,
            },
            {
                "base_url": "http://192.168.1.20:8069",
                "allow_insecure_loopback": True,
            },
            {
                "base_url": "https://odoo.example.test",
                "allow_insecure_loopback": True,
            },
        )
        for values in rejected:
            with self.subTest(values=values), self.assertRaises(
                ConnectorConfigurationError
            ):
                self.config(**values)

    def test_authenticated_transport_refuses_redirects(self) -> None:
        handler = _NoRedirectHandler()
        redirected = handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "http://127.0.0.1:9999/capture",
        )
        self.assertIsNone(redirected)


if __name__ == "__main__":
    unittest.main()

