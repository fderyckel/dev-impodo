"""Retain useful write evidence without persisting arbitrary Odoo text."""

import json
from unittest import TestCase

from impodo.adapters.odoo.write_diagnostics import import_rejection_diagnostic
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)


class WriteDiagnosticTests(TestCase):
    def test_known_error_retains_field_range_and_fingerprint_without_values(self):
        diagnostic = import_rejection_diagnostic(
            [
                {
                    "type": "error",
                    "field": "x_link_id/.id",
                    "rows": {"from": 2, "to": 2},
                    "message": "No matching record found for 'private-business-value' token=secret",
                }
            ],
            model="x.custom",
            fields=("name", "x_link_id/.id"),
            batch_size=10,
        )
        self.assertEqual(diagnostic.code, WriteFailureCode.RELATED_RECORD)
        self.assertEqual(diagnostic.fields, ("x_link_id",))
        self.assertEqual((diagnostic.row_from, diagnostic.row_to), (2, 2))
        self.assertTrue(diagnostic.message_fingerprint.startswith("sha256:"))
        serialized = json.dumps(diagnostic.to_dict()) + diagnostic.summary
        self.assertNotIn("private-business-value", serialized)
        self.assertNotIn("secret", serialized)
        self.assertEqual(
            OdooWriteDiagnostic.from_dict(diagnostic.to_dict()), diagnostic
        )

    def test_missing_and_malformed_server_errors_still_retain_operation_context(self):
        for value in (
            None,
            "private response",
            {},
            [None],
            [{"type": "error", "message": "unclassified private text"}],
        ):
            with self.subTest(value=value):
                diagnostic = import_rejection_diagnostic(
                    value, model="x.custom", fields=("name",), batch_size=3
                )
                self.assertEqual(diagnostic.code, WriteFailureCode.REJECTED)
                self.assertEqual(
                    (diagnostic.model, diagnostic.method, diagnostic.batch_size),
                    ("x.custom", "load", 3),
                )
                self.assertNotIn("private", diagnostic.summary)

    def test_unreviewed_field_and_invalid_row_range_are_not_trusted(self):
        for rows in (
            {"from": -1, "to": 1},
            {"from": 1, "to": 10},
            {"from": True, "to": True},
        ):
            diagnostic = import_rejection_diagnostic(
                [{"type": "error", "field": "password", "rows": rows}],
                model="x.custom",
                fields=("name",),
                batch_size=2,
            )
            self.assertEqual(diagnostic.fields, ())
            self.assertIsNone(diagnostic.row_from)

    def test_legacy_journal_keeps_exact_json_and_new_evidence_roundtrips(self):
        attempt = ExecutionRowAttempt(
            "row",
            "records",
            2,
            "x.custom",
            "CREATE",
            ("name",),
            "test.record",
            status=ExecutionRowStatus.FAILED,
        )
        old = attempt.to_json()
        self.assertNotIn("diagnostic", old)
        self.assertEqual(ExecutionRowAttempt.from_json(old).to_json(), old)
        diagnostic = import_rejection_diagnostic(
            [], model="x.custom", fields=("name",), batch_size=1
        )
        from dataclasses import replace

        enriched = replace(attempt, diagnostic=diagnostic)
        self.assertEqual(ExecutionRowAttempt.from_json(enriched.to_json()), enriched)

    def test_partial_or_truncated_ranges_do_not_identify_one_faulty_row(self):
        located = {"type": "error", "rows": {"from": 1, "to": 1}}
        for messages in (
            [located, {"type": "error"}],
            [located] * 21,
        ):
            diagnostic = import_rejection_diagnostic(
                messages, model="x.custom", fields=("name",), batch_size=3
            )
            self.assertIsNone(diagnostic.row_from)
            self.assertIsNone(diagnostic.row_to)
