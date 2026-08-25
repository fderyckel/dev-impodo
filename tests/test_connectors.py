from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from impodo.connectors import (
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
    Json2Config,
    Json2ReadConnector,
    Json2WriteIdentityConnector,
    MetadataRequest,
    RecordRequest,
    _NoRedirectHandler,
    target_record_read_config,
)


class Json2ConnectorTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "base_url": "https://odoo.example.test",
            "database": "odoo_review",
            "api_key": "super-secret-token",
            "connection_mode": "REMOTE",
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
            target_record_read_config(
                self.config(context={"active_test": True, "lang": "fr_FR"})
            ),
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
            all(call[1]["X-Odoo-Database"] == "odoo_review" for call in post_calls)
        )
        self.assertTrue(all(call[2]["order"] == "id asc" for call in post_calls))
        self.assertTrue(
            all(
                call[2]["context"]
                == {"active_test": False, "lang": "fr_FR"}
                for call in post_calls
            )
        )

    def test_record_limit_bounds_pagination(self) -> None:
        calls = []

        def transport(url, _headers, body, _timeout, _method):
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            payload = json.loads(body)
            calls.append(payload)
            return 200, [
                {"id": payload["offset"] + index + 1, "code": "X"}
                for index in range(payload["limit"])
            ]

        connector = Json2ReadConnector(
            self.config(page_size=2),
            transport=transport,
        )
        snapshot = connector.get_records(
            (RecordRequest("x.model", ("code",), limit=3),)
        )

        self.assertEqual(len(snapshot.records["x.model"]), 3)
        self.assertEqual(
            [(item["offset"], item["limit"]) for item in calls],
            [(0, 2), (2, 1)],
        )

    def test_read_identity_probe_is_closed_stable_and_secret_independent(
        self,
    ) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            del timeout, method
            payload = json.loads(body) if body else None
            calls.append((url, dict(headers), payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            if url.endswith("/res.users/context_get"):
                return 200, {"uid": 17, "lang": "en_US", "tz": "UTC"}
            if url.endswith("/res.users/search_read"):
                return 200, [
                    {
                        "id": 17,
                        "login": "impodo-read@example.test",
                        "company_id": [3, "Example"],
                        "group_ids": [8, 4],
                        "lang": "en_US",
                        "tz": "UTC",
                        "share": False,
                    }
                ]
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": 7}, {"id": 3}]
            if url.endswith("/has_access"):
                return 200, True
            self.fail(f"unexpected URL: {url}")

        connector = Json2ReadConnector(
            self.config(),
            transport=transport,
            now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        )

        identity = connector.probe_read_identity(
            ("res.partner", "product.template", "res.partner")
        )
        rotated_identity = Json2ReadConnector(
            self.config(api_key="rotated-secret-token"),
            transport=transport,
            now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        ).probe_read_identity(("product.template", "res.partner"))

        self.assertRegex(identity.principal_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(identity.permission_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(identity.context_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            identity.readable_models,
            ("product.template", "res.partner"),
        )
        self.assertEqual(identity, rotated_identity)
        self.assertNotIn("super-secret-token", repr(identity))
        context_call = next(
            call for call in calls if call[0].endswith("/res.users/context_get")
        )
        self.assertEqual(context_call[2], {"context": {}})
        user_call = next(
            call for call in calls if call[0].endswith("/res.users/search_read")
        )
        self.assertEqual(user_call[2]["domain"], [["id", "=", 17]])
        self.assertEqual(user_call[2]["limit"], 2)
        company_call = next(
            call for call in calls if call[0].endswith("/res.company/search_read")
        )
        self.assertEqual(
            company_call[2]["domain"],
            [["user_ids", "in", [17]], ["active", "=", True]],
        )
        self.assertEqual(company_call[2]["fields"], ["id"])
        access_calls = [call for call in calls if call[0].endswith("/has_access")]
        self.assertEqual(len(access_calls), 4)
        self.assertTrue(all(call[2]["ids"] == [] for call in access_calls))
        self.assertTrue(
            all(call[2]["operation"] == "read" for call in access_calls)
        )

    def test_read_identity_probe_fails_when_model_read_is_not_allowed(self) -> None:
        def transport(url, _headers, body, _timeout, _method):
            json.loads(body) if body else None
            if url.endswith("/res.users/context_get"):
                return 200, {"uid": 17, "lang": "en_US", "tz": "UTC"}
            if url.endswith("/res.users/search_read"):
                return 200, [
                    {
                        "id": 17,
                        "login": "impodo-read@example.test",
                        "company_id": [3, "Example"],
                        "group_ids": [4],
                        "lang": "en_US",
                        "tz": "UTC",
                        "share": False,
                    }
                ]
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": 3}]
            return 200, False

        connector = Json2ReadConnector(self.config(), transport=transport)

        with self.assertRaisesRegex(
            ConnectorAuthorizationError,
            "cannot access model x.private",
        ):
            connector.probe_read_identity(("x.private",))

    def test_write_identity_probe_requires_exact_readback_and_write_scope(
        self,
    ) -> None:
        calls = []

        def transport(url, _headers, body, _timeout, _method):
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            if url.endswith("/res.users/context_get"):
                return 200, {"uid": 17, "lang": "en_US", "tz": "UTC"}
            if url.endswith("/res.users/search_read"):
                return 200, [
                    {
                        "id": 17,
                        "login": "impodo-write@example.test",
                        "company_id": [3, "Example"],
                        "group_ids": [9, 4],
                        "lang": "en_US",
                        "tz": "UTC",
                        "share": False,
                    }
                ]
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": 3}]
            if url.endswith("/has_access"):
                return 200, True
            self.fail(f"unexpected URL: {url}")

        identity = Json2WriteIdentityConnector(
            self.config(api_key="write-only-secret"),
            transport=transport,
            now=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        ).probe_write_identity(
            ("res.partner", "product.category"),
            ("res.partner",),
        )

        self.assertEqual(
            identity.readable_models,
            ("product.category", "res.partner"),
        )
        self.assertEqual(identity.writable_models, ("res.partner",))
        self.assertRegex(identity.principal_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(identity.permission_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("write-only-secret", repr(identity))
        access_calls = [item for item in calls if item[0].endswith("/has_access")]
        self.assertEqual(
            [item[1]["operation"] for item in access_calls],
            ["read", "read", "write"],
        )

    def test_write_identity_probe_fails_closed_on_denied_write(self) -> None:
        def transport(url, _headers, body, _timeout, _method):
            payload = json.loads(body) if body else None
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            if url.endswith("/res.users/context_get"):
                return 200, {"uid": 17}
            if url.endswith("/res.users/search_read"):
                return 200, [
                    {
                        "id": 17,
                        "login": "writer@example.test",
                        "company_id": [3, "Example"],
                        "group_ids": [],
                        "lang": "en_US",
                        "tz": "UTC",
                        "share": False,
                    }
                ]
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": 3}]
            if url.endswith("/has_access"):
                return 200, payload["operation"] == "read"
            self.fail(f"unexpected URL: {url}")

        connector = Json2WriteIdentityConnector(
            self.config(),
            transport=transport,
        )
        with self.assertRaisesRegex(
            ConnectorAuthorizationError,
            "cannot access model res.partner",
        ):
            connector.probe_write_identity(("res.partner",), ("res.partner",))

    def test_read_identity_probe_rejects_a_mismatched_self_record(self) -> None:
        def transport(url, _headers, body, _timeout, _method):
            json.loads(body) if body else None
            if url.endswith("/res.users/context_get"):
                return 200, {"uid": 17}
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": 3}]
            return 200, [
                {
                    "id": 18,
                    "login": "other@example.test",
                    "company_id": [3, "Example"],
                    "group_ids": [],
                    "lang": "en_US",
                    "tz": "UTC",
                    "share": False,
                }
            ]

        connector = Json2ReadConnector(self.config(), transport=transport)

        with self.assertRaisesRegex(
            ConnectorIncompleteResultError,
            "does not match",
        ):
            connector.probe_read_identity(("res.partner",))

    def test_same_model_chunks_merge_identical_records(self) -> None:
        def transport(url, _headers, body, _timeout, _method):
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            json.loads(body)
            return 200, [{"id": 7, "code": "SAME"}]

        connector = Json2ReadConnector(self.config(page_size=500), transport=transport)
        snapshot = connector.get_records(
            (
                RecordRequest("x.model", ("code",), (["code", "=", "A"],)),
                RecordRequest("x.model", ("code",), (["code", "=", "B"],)),
            )
        )

        self.assertEqual(
            snapshot.records["x.model"],
            (snapshot.records["x.model"][0],),
        )
        self.assertEqual(snapshot.records["x.model"][0].odoo_id, 7)

    def test_same_model_chunk_conflict_fails_closed(self) -> None:
        responses = iter(("FIRST", "SECOND"))

        def transport(url, _headers, body, _timeout, _method):
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            json.loads(body)
            return 200, [{"id": 7, "code": next(responses)}]

        connector = Json2ReadConnector(self.config(page_size=500), transport=transport)
        with self.assertRaisesRegex(
            ConnectorIncompleteResultError,
            "record chunks conflict",
        ):
            connector.get_records(
                (
                    RecordRequest("x.model", ("code",), (["code", "=", "A"],)),
                    RecordRequest("x.model", ("code",), (["code", "=", "B"],)),
                )
            )

    def test_repeated_record_across_pages_fails_closed(self) -> None:
        def transport(url, _headers, body, _timeout, _method):
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            offset = json.loads(body)["offset"]
            if offset == 0:
                return 200, [
                    {"id": 1, "code": "A"},
                    {"id": 2, "code": "B"},
                ]
            return 200, [{"id": 2, "code": "B"}]

        connector = Json2ReadConnector(self.config(page_size=2), transport=transport)
        with self.assertRaisesRegex(
            ConnectorIncompleteResultError,
            "pagination repeated records",
        ):
            connector.get_records(
                (RecordRequest("x.model", ("code",), (["active", "=", True],)),)
            )

    def test_fields_get_uses_named_json2_arguments(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            if url.endswith("/default_get"):
                return 200, {"name": "New record"}
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
        fields_call = next(
            item for item in calls if item[0].endswith("/fields_get")
        )
        self.assertTrue(fields_call[0].endswith("/json/2/x.model/fields_get"))
        self.assertEqual(fields_call[1]["allfields"], ["name"])
        self.assertIn("attributes", fields_call[1])
        self.assertEqual(snapshot.create_defaults["x.model"]["name"], "New record")

    def test_schema_discovery_requests_all_fields_once_per_model(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            if url.endswith("/default_get"):
                return 200, {"name": "New record"}
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

        fields_call = next(
            item for item in calls if item[0].endswith("/fields_get")
        )
        self.assertEqual(fields_call[1]["allfields"], [])
        self.assertEqual(
            set(snapshot.models["x.model"].fields),
            {"name", "display_name"},
        )
        self.assertEqual(
            snapshot.models["x.model"].fields["name"].label,
            "Name",
        )
        default_calls = [
            item for item in calls if item[0].endswith("/default_get")
        ]
        self.assertEqual(len(default_calls), 1)

    def test_schema_constraint_evidence_is_batched_for_all_models(self) -> None:
        calls = []

        def transport(url, headers, body, timeout, method):
            del headers, timeout, method
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "19.0"}
            if url.endswith("/json/2/ir.model/search_read"):
                return 200, [
                    {"id": 42, "model": "x.model"},
                    {"id": 43, "model": "y.model"},
                ]
            if url.endswith("/json/2/ir.model.constraint/search_read"):
                return 200, [
                    {
                        "id": 7,
                        "name": "x_model_code_uniq",
                        "definition": "UNIQUE(code)",
                        "model": [42, "Custom Model"],
                    }
                ]
            return 200, {
                "code": {
                    "string": "Code",
                    "type": "char",
                    "required": True,
                    "readonly": False,
                }
            }

        connector = Json2ReadConnector(self.config(), transport=transport)
        snapshot = connector.get_model_metadata(
            [
                MetadataRequest(
                    "x.model",
                    (),
                    all_fields=True,
                    include_unique_constraints=True,
                ),
                MetadataRequest(
                    "y.model",
                    (),
                    all_fields=True,
                    include_unique_constraints=True,
                ),
            ]
        )

        constraints = snapshot.models["x.model"].unique_constraints
        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0].definition, "UNIQUE(code)")
        self.assertEqual(
            sum(url.endswith("/json/2/ir.model/search_read") for url, _ in calls),
            1,
        )
        model_call = next(
            payload
            for url, payload in calls
            if url.endswith("/json/2/ir.model/search_read")
        )
        self.assertEqual(
            model_call["domain"],
            [["model", "in", ["x.model", "y.model"]]],
        )
        self.assertEqual(
            sum(
                url.endswith("/json/2/ir.model.constraint/search_read")
                for url, _ in calls
            ),
            1,
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
                "get_target_fingerprint",
                "get_model_metadata",
                "get_records",
                "probe_read_identity",
            },
        )

    def test_config_repr_redacts_api_key(self) -> None:
        self.assertNotIn("super-secret-token", repr(self.config()))

    def test_http_is_allowed_only_for_explicit_literal_loopback_mode(self) -> None:
        local = self.config(
            base_url="http://127.0.0.1:8069",
            connection_mode="LOCAL",
        )
        self.assertEqual(local.connection_mode, "LOCAL")

        rejected = (
            {
                "base_url": "http://127.0.0.1:8069",
                "connection_mode": "REMOTE",
            },
            {
                "base_url": "http://localhost:8069",
                "connection_mode": "LOCAL",
            },
            {
                "base_url": "http://192.168.1.20:8069",
                "connection_mode": "LOCAL",
            },
            {
                "base_url": "https://odoo.example.test",
                "connection_mode": "LOCAL",
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
