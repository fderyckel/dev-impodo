"""Retain actionable private explanations without weakening write certainty."""

import json
import unittest
from io import BytesIO
from itertools import product
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.load_control import can_set_aside
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.operator_errors import (
    OperatorErrorDetail,
    capture_operator_error,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteErrorType,
    UncertainWriteReason,
    WriteFailureCode,
)
from tests.integration.odoo.test_load_request_pacing import config
from tests.support.odoo_read_clock import ReadClock

SCOPE = OdooApiScope(
    "sha256:" + "1" * 64,
    (
        OdooModelScope(
            "product.template",
            ("list_price", "name"),
            ("list_price", "name"),
            ("name",),
        ),
        OdooModelScope("res.partner", ("name", "vat"), ("name", "vat"), ("name",)),
    ),
)


class OperatorErrorAdapterTests(unittest.TestCase):
    def test_import_retains_actual_wire_value_and_validated_field_for_both_versions(
        self,
    ):
        for major, mode, model, field in product(
            (19, 20),
            ("LOCAL", "REMOTE"),
            ("res.partner", "product.template"),
            ("known", "unspecified"),
        ):
            with self.subTest(major=major, mode=mode, model=model, field=field):
                target = "vat" if model == "res.partner" else "list_price"
                message = {
                    "type": "error",
                    "message": "Fictional rule rejected 'twelve'.",
                    "rows": {"from": 0, "to": 0},
                }
                if field == "known":
                    message["field"] = [target, "Business label"]
                calls = []

                def transport(_url, _headers, body, *_):
                    calls.append(json.loads(body))
                    return 200, {"ids": False, "messages": [message]}

                writer = Json2WriteExecutor(
                    config(major, mode),
                    SCOPE,
                    transport,
                    request_pacer=ReadClock().pacer(),
                )
                with self.assertRaises(OdooWriteRejected) as caught:
                    writer.load_create_rows(
                        model,
                        ({"name": "Prepared name", target: "twelve"},),
                        ("synthetic.row",),
                    )
                error = caught.exception
                self.assertEqual(len(calls), 1)
                self.assertIn("twelve", calls[0]["data"][0])
                self.assertEqual(error.operator_detail.submitted[0][target], "twelve")
                self.assertEqual(
                    error.operator_detail.messages[0].fields,
                    (target,) if field == "known" else (),
                )
                self.assertEqual(error.diagnostic.operator_detail_status, "AVAILABLE")
                self.assertNotIn("twelve", str(error))
                self.assertNotIn("twelve", repr(error.operator_detail))
                self.assertNotIn("twelve", json.dumps(error.diagnostic.to_dict()))

    def test_http_create_and_update_extract_only_user_message_and_keep_unknown_unknown(
        self,
    ):
        for major, mode, method in product(
            (19, 20), ("LOCAL", "REMOTE"), ("create", "write")
        ):
            with self.subTest(major=major, mode=mode, method=method):
                payload = {
                    "name": "odoo.exceptions.ValidationError",
                    "message": "Fictional destination rule rejected Prepared name.",
                    "debug": "Traceback /private/customer SQL secret",
                }
                writer = Json2WriteExecutor(
                    config(major, mode), SCOPE, request_pacer=ReadClock().pacer()
                )
                with patch("impodo.adapters.odoo.connectors.build_opener") as opener:
                    opener.return_value.open.side_effect = HTTPError(
                        "synthetic",
                        400,
                        "private",
                        {},
                        BytesIO(json.dumps(payload).encode()),
                    )
                    with self.assertRaises(OdooWriteRejected) as caught:
                        if method == "create":
                            writer.create_rows(
                                "res.partner", ({"name": "Prepared name"},)
                            )
                        else:
                            writer.update_rows(
                                "res.partner", (42, 43), {"name": "Prepared name"}
                            )
                    self.assertEqual(opener.return_value.open.call_count, 1)
                detail = caught.exception.operator_detail
                self.assertEqual(len(detail.submitted), 1 if method == "create" else 2)
                self.assertIn("destination rule", detail.messages[0].text)
                self.assertNotIn("/private", detail.to_json())
                self.assertEqual(detail.messages[0].fields, ())
                self.assertTrue(can_set_aside(caught.exception))
        writer = Json2WriteExecutor(
            config(),
            SCOPE,
            lambda *_: (503, OdooWriteErrorType.VALIDATION),
            request_pacer=ReadClock().pacer(),
        )
        with self.assertRaises(OdooWriteOutcomeUnknown) as caught:
            writer.create_rows("res.partner", ({"name": "Prepared name"},))
        self.assertIsNone(caught.exception.operator_detail)
        self.assertEqual(caught.exception.diagnostic.code, WriteFailureCode.UNKNOWN)
        self.assertEqual(caught.exception.diagnostic.http_status, 503)

    def test_uncertain_write_preserves_observed_http_status_and_failure_kind(self):
        for major, mode in product((19, 20), ("LOCAL", "REMOTE")):
            for reason, body in (
                (UncertainWriteReason.TIMEOUT, None),
                (UncertainWriteReason.UNREADABLE_RESPONSE, b"private gateway text"),
                (UncertainWriteReason.RESPONSE_TOO_LARGE, b" " * (8 * 1024 * 1024 + 1)),
                (UncertainWriteReason.INVALID_RECEIPT, b'{"unexpected":"private"}'),
                (
                    UncertainWriteReason.RECEIPT_WITH_ERRORS,
                    b'{"ids":[42],"messages":[{"type":"error","message":"private"}]}',
                ),
            ):
                with (
                    self.subTest(major=major, mode=mode, reason=reason),
                    patch("impodo.adapters.odoo.connectors.build_opener") as opener,
                ):
                    request = opener.return_value.open
                    if body is None:
                        request.side_effect = URLError(TimeoutError("private"))
                    else:
                        response = BytesIO(body)
                        response.status = 200
                        request.return_value = response
                    writer = Json2WriteExecutor(
                        config(major, mode), SCOPE, request_pacer=ReadClock().pacer(),
                    )
                    with self.assertRaises(OdooWriteOutcomeUnknown) as caught:
                        writer.load_create_rows(
                            "res.partner", ({"name": "private value"},), ("synthetic.contact",),
                        )
                    request.assert_called_once()
                    diagnostic = caught.exception.diagnostic
                    self.assertEqual(diagnostic.code, WriteFailureCode.UNKNOWN)
                    self.assertEqual(diagnostic.uncertainty_reason, reason)
                    self.assertEqual(diagnostic.http_status, 200 if body is not None else None)
                    self.assertNotIn("private", str(caught.exception))
                    self.assertNotIn("private", json.dumps(diagnostic.to_dict()))
                    self.assertIsNone(caught.exception.operator_detail)

    def test_http_write_rejection_requires_typed_validation_to_continue(self):
        for status, error_type, code in (
            (403, OdooWriteErrorType.VALIDATION, WriteFailureCode.ACCESS_DENIED),
            (422, OdooWriteErrorType.USER, WriteFailureCode.REJECTED),
            (422, None, WriteFailureCode.REJECTED),
        ):
            with self.subTest(status=status, error_type=error_type):
                writer = Json2WriteExecutor(
                    config(), SCOPE, lambda *_: (status, error_type),
                    request_pacer=ReadClock().pacer(),
                )
                with self.assertRaises(OdooWriteRejected) as caught:
                    writer.update_row("res.partner", 42, {"name": "Prepared name"})
                self.assertEqual(caught.exception.diagnostic.code, code)
                self.assertFalse(can_set_aside(caught.exception))

    def test_bounds_protected_values_and_invalid_contracts(self):
        messages = [
            {
                "type": "error",
                "message": "<script>fictional</script> key-123 protected-456 "
                + "é" * 9000,
                "field": "invented",
            },
            {
                "type": "error",
                "message": "Traceback /private/customer SELECT name FROM partner",
            },
            {"type": "error", "message": "Internal script /opt/odoo/private.py"},
            {
                "type": "error",
                "message": "Database rejected record\nDETAIL: failing private row",
            },
        ]
        detail = capture_operator_error(
            messages,
            ({"name": "wire-value", "password": "protected-456"},),
            secrets=("key-123",),
        )
        self.assertTrue(detail.truncated)
        self.assertTrue(detail.withheld)
        self.assertNotIn("key-123", detail.to_json())
        self.assertNotIn("protected-456", detail.to_json())
        self.assertNotIn("/private/customer", detail.to_json())
        self.assertNotIn("/opt/odoo", detail.to_json())
        self.assertNotIn("failing private row", detail.to_json())
        self.assertEqual(detail.messages[0].fields, ())
        self.assertEqual(OperatorErrorDetail.from_json(detail.to_json()), detail)
        changed = json.loads(detail.to_json())
        changed["contract_version"] = 99
        with self.assertRaises(ValueError):
            OperatorErrorDetail.from_json(json.dumps(changed))

    def test_capture_failure_does_not_change_definitive_rejection_or_repeat_write(self):
        calls = []

        def transport(*_):
            calls.append(True)
            return 200, {
                "ids": False,
                "messages": [{"type": "error", "message": "Fictional rule"}],
            }

        writer = Json2WriteExecutor(
            config(), SCOPE, transport, request_pacer=ReadClock().pacer()
        )
        with (
            patch(
                "impodo.adapters.odoo.writer.capture_operator_error",
                side_effect=ValueError("private"),
            ),
            self.assertRaises(OdooWriteRejected) as caught,
        ):
            writer.load_create_rows(
                "res.partner", ({"name": "Prepared name"},), ("synthetic.row",)
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            caught.exception.diagnostic.operator_detail_status, "CAPTURE_FAILED"
        )
        self.assertIsNone(caught.exception.operator_detail)
