"""Bounded HTTP diagnostics for the shared Odoo JSON-2 read boundary."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.error import URLError

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    Json2ReadFailureDiagnostic,
    observe_json2_read_failures,
)
from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorTransportError,
    MetadataRequest,
    RecordRequest,
)


class Json2HttpReadFailureTests(unittest.TestCase):
    def _connector(self, version, statuses):
        calls = []
        responses = iter(statuses)

        def transport(url, _headers, _body, _timeout, _method):
            if url.endswith("/web/version"):
                return 200, {"version": version}
            calls.append(url)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response, {"message": "private server body and token"}

        connector = Json2ReadConnector(
            Json2Config(
                base_url="https://private-host.example.test",
                database="private-database",
                api_key="private-api-key",
                retries=2,
            ),
            transport=transport,
        )
        return connector, calls

    def test_permanent_404_names_failed_operation_without_retrying(self):
        for version in ("19.0", "20.0"):
            for method in ("has_access", "fields_get", "search_read"):
                with self.subTest(version=version, method=method):
                    connector, calls = self._connector(version, [404])
                    failures = []
                    with (
                        observe_json2_read_failures(failures.append),
                        self.assertRaises(ConnectorTransportError) as caught,
                    ):
                        if method == "fields_get":
                            connector.get_model_metadata(
                                [MetadataRequest("x.missing.model", ("name",))]
                            )
                        elif method == "search_read":
                            connector.get_records(
                                [RecordRequest("x.missing.model", ("name",))]
                            )
                        else:
                            connector._post_read_method(
                                "x.missing.model", method, {"operation": "read"}
                            )

                    self.assertEqual(len(calls), 1)
                    self.assertEqual(
                        failures,
                        [
                            Json2ReadFailureDiagnostic(
                                model="x.missing.model",
                                method=method,
                                category="http",
                                attempt_count=1,
                                http_status=404,
                            )
                        ],
                    )
                    message = str(caught.exception)
                    self.assertIn(f"{method} read for x.missing.model", message)
                    self.assertIn("was unavailable with HTTP 404", message)
                    for private in (
                        "private-host",
                        "private-database",
                        "private-api-key",
                        "private server body",
                        "name",
                    ):
                        self.assertNotIn(private, message)

    def test_authentication_classification_is_preserved_and_observed(self):
        for version in ("19.0", "20.0"):
            for status in (401, 403):
                with self.subTest(version=version, status=status):
                    connector, calls = self._connector(version, [status])
                    failures = []
                    with (
                        observe_json2_read_failures(failures.append),
                        self.assertRaises(ConnectorAuthenticationError) as caught,
                    ):
                        connector.get_records([RecordRequest("res.partner", ("name",))])
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(failures[0].http_status, status)
                    self.assertEqual(failures[0].attempt_count, 1)
                    self.assertEqual(
                        str(caught.exception),
                        f"Odoo JSON-2 authorization failed with HTTP {status}",
                    )

    def test_actual_attempt_count_includes_transient_http_and_network_failures(self):
        for version in ("19.0", "20.0"):
            for statuses, final_status in (
                ([503, 503, 503], 503),
                ([URLError("private-host-and-token"), 503, 404], 404),
            ):
                with self.subTest(version=version, final_status=final_status):
                    connector, calls = self._connector(version, statuses)
                    failures = []
                    with (
                        patch("impodo.adapters.odoo.connectors.time.sleep"),
                        observe_json2_read_failures(failures.append),
                        self.assertRaises(ConnectorTransportError),
                    ):
                        connector.get_records([RecordRequest("res.partner", ("name",))])
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(len(failures), 1)
                    self.assertEqual(failures[0].http_status, final_status)
                    self.assertEqual(failures[0].attempt_count, 3)

    def test_success_after_transient_failure_emits_no_failure(self):
        connector, calls = self._connector("20.0", [503, 200])
        failures = []
        with (
            patch("impodo.adapters.odoo.connectors.time.sleep"),
            observe_json2_read_failures(failures.append),
        ):
            result = connector._post_read_method("res.partner", "fields_get", {})
        self.assertEqual(result, {"message": "private server body and token"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(failures, [])

    def test_observer_failure_does_not_replace_http_failure(self):
        connector, _calls = self._connector("20.0", [404])

        def failed_observer(_failure):
            raise RuntimeError("observer failed")

        with (
            self.assertLogs("impodo.adapters.odoo.connectors", level="ERROR"),
            observe_json2_read_failures(failed_observer),
            self.assertRaisesRegex(ConnectorTransportError, "HTTP 404"),
        ):
            connector.get_records([RecordRequest("res.partner", ("name",))])

    def test_failure_contract_rejects_unbounded_or_inconsistent_status(self):
        for category, status in (
            ("http", None),
            ("http", 200),
            ("http", 600),
            ("http", True),
            ("timeout", 404),
        ):
            with self.subTest(category=category, status=status):
                with self.assertRaises(ValueError):
                    Json2ReadFailureDiagnostic(
                        model="res.partner",
                        method="search_read",
                        category=category,
                        attempt_count=1,
                        http_status=status,
                    )


if __name__ == "__main__":
    unittest.main()
