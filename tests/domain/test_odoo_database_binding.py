"""Verify one explicit database authority independent of mode and Company."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from unittest import TestCase
from uuid import uuid4

from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingError,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    database_scope_hash,
)
from impodo.domain.shared.access import LOCAL_ACTOR


class OdooDatabaseBindingDomainTests(TestCase):
    def test_scope_uses_canonical_endpoint_and_database_only(self):
        scope = database_scope_hash("https://ODOO.example.test:443/", "demo")
        self.assertEqual(
            scope, database_scope_hash("https://odoo.example.test", " demo ")
        )
        for endpoint, database in (
            ("https://other.example.test", "demo"),
            ("https://odoo.example.test", "other"),
            ("https://odoo.example.test/path", "demo"),
        ):
            self.assertNotEqual(scope, database_scope_hash(endpoint, database))

    def test_initial_generation_is_deterministic_and_organization_specific(self):
        scope = database_scope_hash("http://localhost:8069", "demo")
        first = OdooDatabaseGeneration("organization-a", scope, 0)
        self.assertEqual(first, OdooDatabaseGeneration("organization-a", scope, 0))
        self.assertNotEqual(
            first.generation_hash,
            OdooDatabaseGeneration("organization-b", scope, 0).generation_hash,
        )
        self.assertEqual(OdooDatabaseGeneration.from_json(first.to_json()), first)
        with self.assertRaises(OdooDatabaseBindingError):
            replace(first, declared_at=datetime.now(UTC))

    def test_declaration_and_review_proof_are_immutable_current_contracts(self):
        initial = OdooDatabaseGeneration(
            "organization-a", database_scope_hash("http://localhost:8069", "demo"), 0
        )
        current = OdooDatabaseGeneration(
            "organization-a",
            initial.scope_hash,
            1,
            initial.generation_hash,
            str(uuid4()),
            LOCAL_ACTOR.identity,
            datetime.now(UTC),
            "Recreated for a new test",
        )
        binding = OdooDatabaseBinding(
            str(uuid4()),
            str(uuid4()),
            "organization-a",
            OdooDatabaseBindingRole.DESTINATION,
            current.scope_hash,
            current.generation_hash,
            LOCAL_ACTOR.identity,
            datetime.now(UTC),
            str(uuid4()),
        )
        self.assertEqual(OdooDatabaseGeneration.from_json(current.to_json()), current)
        self.assertEqual(OdooDatabaseBinding.from_json(binding.to_json()), binding)
        self.assertNotEqual(binding.content_hash, binding.generation_hash)
        with self.assertRaises(FrozenInstanceError):
            binding.generation_hash = initial.generation_hash
        with self.assertRaises(OdooDatabaseBindingError):
            replace(binding, contract_version=0)

    def test_endpoint_cannot_contain_credentials_query_or_fragment(self):
        for endpoint in (
            "https://user:password@example.test",
            "https://example.test?token=secret",
            "https://example.test#other",
            "file:///tmp/database",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaises(OdooDatabaseBindingError),
            ):
                database_scope_hash(endpoint, "demo")
