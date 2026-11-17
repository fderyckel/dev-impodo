"""Preserve exact shared identity keys across execution and read-back."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from impodo.domain.execution.identity import identity_domain, portable_key
from impodo.domain.shared.models import BusinessReference, LogicalReference
from impodo.domain.workspace.errors import WorkspaceError


class ExecutionIdentityTests(TestCase):
    def test_portable_key_retains_typed_decimal_and_utc_time(self):
        key = portable_key(
            (
                Decimal("1.20"),
                date(2026, 11, 17),
                datetime(2026, 11, 17, 14, 30, tzinfo=timezone(timedelta(hours=2))),
            )
        )

        self.assertEqual(
            key,
            '[{"type":"decimal","value":"1.20"},'
            '{"type":"date","value":"2026-11-17"},'
            '{"type":"datetime","value":"2026-11-17T12:30:00Z"}]',
        )
        self.assertNotEqual(portable_key((Decimal("1.20"),)), portable_key((1.2,)))

    def test_nested_key_preserves_field_order_and_explicit_empty_scope(self):
        company = BusinessReference("res.company", ("North",), target_fields=("name",))
        parent = BusinessReference(
            "x.parent",
            ("P1",),
            scope=(company,),
            target_fields=("code",),
            target_scope_fields=("company_id",),
        )

        domain = identity_domain(
            ("parent_id", "sequence"),
            (parent, Decimal("12.50")),
            ("active", "effective_on", "observed_at", "optional_id"),
            (
                False,
                date(2026, 11, 17),
                datetime(2026, 11, 17, 14, 30, tzinfo=timezone(timedelta(hours=2))),
                None,
            ),
        )

        self.assertEqual(
            domain,
            (
                ("parent_id.code", "=", "P1"),
                ("parent_id.company_id.name", "=", "North"),
                ("sequence", "=", 12.5),
                ("active", "=", False),
                ("effective_on", "=", "2026-11-17"),
                ("observed_at", "=", "2026-11-17 12:30:00"),
                ("optional_id", "=", None),
            ),
        )

    def test_incomplete_key_or_scope_rejects_before_a_lookup(self):
        for fields, key, scope_fields, scope in (
            ((), (), (), ()),
            (("code",), (), (), ()),
            (("code",), ("P1",), ("company_id",), ()),
        ):
            with self.subTest(fields=fields, key=key, scope=scope):
                with self.assertRaisesRegex(
                    WorkspaceError, "business-key shape is incomplete"
                ):
                    identity_domain(fields, key, scope_fields, scope)

    def test_unresolved_logical_key_is_not_silently_used_as_a_scalar(self):
        reference = LogicalReference(origin="incoming", dataset="parents", key=("P1",))

        with self.assertRaisesRegex(WorkspaceError, "relational business key"):
            identity_domain(("parent_id",), (reference,), (), ())

    def test_unsupported_identity_value_is_rejected(self):
        with self.assertRaisesRegex(WorkspaceError, "not supported by the Odoo API"):
            identity_domain(("code",), (object(),), (), ())
