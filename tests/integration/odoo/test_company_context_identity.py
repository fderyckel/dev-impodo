from __future__ import annotations

import json
import unittest

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.domain.odoo.contracts import (
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
)


class CompanyContextIdentityTests(unittest.TestCase):
    def connector(
        self,
        context,
        *,
        version="20.0",
        companies=(3, 7),
        user_id=17,
        login="integration@example.test",
    ):
        calls = []

        def transport(url, _headers, body, _timeout, _method):
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": version}
            if url.endswith("/res.users/context_get"):
                return 200, {"uid": user_id}
            if url.endswith("/res.users/search_read"):
                return 200, [
                    {
                        "id": user_id,
                        "login": login,
                        "company_id": [companies[0], "Fictional default company"],
                        "group_ids": [4],
                        "share": False,
                    }
                ]
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": company} for company in companies]
            if url.endswith("/has_access"):
                return 200, True
            self.fail(f"Unexpected endpoint: {url}")

        return Json2ReadConnector(
            Json2Config(
                base_url="https://odoo.example.test",
                database="review",
                api_key="fictional-key",
                context=context,
            ),
            transport=transport,
        ), calls

    def test_selected_company_replaces_default_and_matches_request_context(self):
        connector, calls = self.connector({"allowed_company_ids": [7]})
        identity, context = connector._probe_capture_identity(("x.customer",))

        self.assertEqual(context.primary_company_id, 7)
        self.assertEqual(context.allowed_company_ids, (7,))
        self.assertEqual(len(calls), 5)
        for url, payload in calls:
            if not url.endswith("/web/version"):
                self.assertEqual(payload["context"], {"allowed_company_ids": [7]})
        default, _ = self.connector({})
        self.assertNotEqual(
            identity.context_hash,
            default.probe_read_identity(("x.customer",)).context_hash,
        )

    def test_custom_models_users_and_company_ids_on_both_supported_majors(self):
        for version, companies, user_id, models in (
            ("19.0", (80, 105), 212, ("x.dispatch.zone", "x.routing.rule")),
            ("20.0", (49, 7001), 903, ("x.equipment.asset", "x.asset.type")),
        ):
            with self.subTest(version=version, models=models):
                selected = companies[-1]
                connector, calls = self.connector(
                    {"allowed_company_ids": [selected]},
                    version=version,
                    companies=companies,
                    user_id=user_id,
                    login=f"operator-{user_id}@example.test",
                )

                identity, context = connector._probe_capture_identity(models)

                self.assertEqual(context.primary_company_id, selected)
                self.assertEqual(context.allowed_company_ids, (selected,))
                self.assertEqual(identity.readable_models, tuple(sorted(models)))
                self.assertEqual(len(calls), 6)
                access_calls = [
                    (url, payload)
                    for url, payload in calls
                    if url.endswith("/has_access")
                ]
                self.assertEqual(
                    {url.rsplit("/", 2)[1] for url, _ in access_calls},
                    set(models),
                )
                self.assertTrue(
                    all(
                        payload["context"] == {"allowed_company_ids": [selected]}
                        for _, payload in access_calls
                    )
                )

    def test_enabled_company_order_determines_primary_company_and_hash(self):
        first, _ = self.connector({"allowed_company_ids": [7, 3]})
        second, _ = self.connector({"allowed_company_ids": [3, 7]})

        first_identity, first_context = first._probe_capture_identity(("x.customer",))
        second_identity, second_context = second._probe_capture_identity(
            ("x.customer",)
        )

        self.assertEqual(first_context.allowed_company_ids, (7, 3))
        self.assertEqual(first_context.primary_company_id, 7)
        self.assertEqual(second_context.primary_company_id, 3)
        self.assertNotEqual(first_identity.context_hash, second_identity.context_hash)

    def test_unavailable_company_fails_before_model_access_probe(self):
        connector, calls = self.connector({"allowed_company_ids": [9]})
        with self.assertRaisesRegex(ConnectorAuthorizationError, "not available"):
            connector.probe_read_identity(("x.customer",))
        self.assertFalse(any(url.endswith("/has_access") for url, _ in calls))

    def test_invalid_company_context_fails_closed(self):
        for companies in ([], [True], ["7"], [0], [7, 7], "7", [7] * 1001):
            with self.subTest(companies=str(companies)[:40]):
                connector, _ = self.connector({"allowed_company_ids": companies})
                with self.assertRaisesRegex(
                    ConnectorConfigurationError, "context is invalid"
                ):
                    connector.probe_read_identity(("x.customer",))


if __name__ == "__main__":
    unittest.main()
