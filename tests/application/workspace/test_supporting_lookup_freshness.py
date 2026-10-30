"""Lookup reuse keeps stable access bindings and request-scoped permission proof."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.workspace.mapping.supporting_values import load_current_mapping_supporting_references
from impodo.domain.mapping.contracts import DatasetMapping, ReferenceKeyMapping, RelationshipMapping, RelationshipResolver, ResolverOrigin
from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASH, StandardReferenceFieldContract
from impodo.domain.workspace.supporting_lookups import SupportingLookupSnapshot
from tests.domain.workspace.test_supporting_plan import model, relation


class SupportingLookupFreshnessTests(unittest.TestCase):
    def test_scoped_permission_hash_is_retained_and_two_edges_use_one_bulk_read(self):
        workspace_id = str(uuid4())
        snapshot = SupportingLookupSnapshot.capture(
            workspace_id=workspace_id, relation_model="res.country", key_fields=("code",), scope_fields=(), display_field="name",
            field_contracts=(StandardReferenceFieldContract("code", "char", True, False), StandardReferenceFieldContract("name", "char", True, False)),
            target_hash="target", read_credential_binding_hash="credential", read_principal_hash="principal", read_permission_hash="permission", read_context_hash="context",
            captured_at=datetime.now(UTC), captured_by="Manager", choices=(), ambiguous_values=(), reference_policy_hash=REFERENCE_POLICY_HASH,
        )
        schema = SimpleNamespace(
            odoo_version="19.0", connection_target_hash="target", read_credential_binding_hash="credential", read_principal_hash="principal", read_permission_hash="permission", read_context_hash="context",
            models=(model("res.partner", relation("country_id", "res.country"), relation("birth_country_id", "res.country")),),
        )
        dataset = DatasetMapping("contacts", "res.partner", relationships=tuple(
            RelationshipMapping(name, "many2one", ("country",), RelationshipResolver(ResolverOrigin.TARGET_CATALOG, model="res.country", key_mappings=(ReferenceKeyMapping("country", "code"),)))
            for name in ("country_id", "birth_country_id")
        ))
        repository = Mock()
        repository.list_current.return_value = (snapshot,)
        self.assertEqual(load_current_mapping_supporting_references(repository, workspace_id, (dataset,), schema), (snapshot,))
        repository.list_current.assert_called_once()
        self.assertEqual(len(repository.list_current.call_args.args[1]), 1)
        for field in ("read_context_hash", "read_principal_hash", "read_credential_binding_hash", "target_hash"):
            repository.list_current.return_value = (replace(snapshot, **{field: "changed"}),)
            self.assertEqual(load_current_mapping_supporting_references(repository, workspace_id, (dataset,), schema), (), field)
        repository.list_current.return_value = (replace(snapshot, read_permission_hash="supporting-scope"),)
        accepted = load_current_mapping_supporting_references(repository, workspace_id, (dataset,), schema)
        self.assertEqual(accepted[0].read_permission_hash, "supporting-scope")
