"""Protect exact identity aliases and separately reusable portable choices."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from impodo.adapters.protected_destination_identity_aliases import (
    ProtectedDestinationIdentityAliasStore,
)
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.domain.mapping.contracts import ValueMapping
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    DestinationModelMatch,
    RetiredDestinationMatchPlanError,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_identity_aliases import (
    DestinationIdentityAliasEvidence,
    DestinationIdentityAliasEvidenceValue,
    DestinationIdentityAliasRule,
    ProtectedIdentityAliasRulesReference,
    resolve_transfer_identity_aliases,
)
from tests.integration.protected_evidence.test_destination_create_fields import _Secrets

PROJECT = "00000000-0000-0000-0000-000000000091"
OTHER_PROJECT = "00000000-0000-0000-0000-000000000094"
HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64


def aliased_plan():
    now = datetime.now(UTC)
    resolution = resolve_transfer_identity_aliases(
        (("Private source",), ("Unchanged",)),
        key_fields=("name",),
        destination_handling="reference_only",
        aliases=(ValueMapping("Private source", "Private destination"),),
    )
    value = DestinationIdentityAliasEvidenceValue(
        "references", "x.reference", resolution.aliases, resolution.binding
    )
    evidence = DestinationIdentityAliasEvidence(
        evidence_id=str(uuid4()),
        workspace_id="workspace",
        source_selection_hash=HASH,
        source_schema_hash=HASH,
        destination_target_hash=HASH,
        destination_credential_binding_hash=HASH,
        destination_read_principal_hash=HASH,
        destination_read_permission_hash=HASH,
        destination_read_context_hash=HASH,
        destination_schema_snapshot_hash=HASH,
        destination_record_snapshot_hash=HASH,
        values=(value,),
        recorded_at=now,
    )
    model = DestinationModelMatch(
        dataset_id="references",
        dataset_name="References",
        model="x.reference",
        model_label="Reference",
        source_column_key="reference-name",
        key_field="name",
        key_field_label="Name",
        source_row_count=2,
        source_distinct_key_count=2,
        source_blank_row_count=0,
        source_duplicate_key_count=0,
        destination_existing_key_count=2,
        destination_duplicate_key_count=0,
        destination_create_key_count=0,
        destination_key_binding_hash=HASH,
        compatible_fields=("name",),
        missing_fields=(),
        incompatible_fields=(),
        destination_limit_reached=False,
        destination_handling="reference_only",
    )
    return DestinationMatchPlan(
        workspace_id="workspace",
        source_selection_hash=HASH,
        source_schema_hash=HASH,
        destination_target_hash=HASH,
        destination_credential_binding_hash=HASH,
        destination_read_principal_hash=HASH,
        destination_read_permission_hash=HASH,
        destination_read_context_hash=HASH,
        destination_schema_snapshot_hash=HASH,
        destination_record_snapshot_hash=HASH,
        model_matches=(model,),
        recorded_at=now,
        recorded_by="Data manager",
        identity_alias_bindings=evidence.public_bindings,
        identity_alias_evidence_id=evidence.evidence_id,
        identity_alias_evidence_hash=evidence.content_hash,
        identity_alias_evidence=evidence,
    )


class ProtectedDestinationIdentityAliasTests(unittest.TestCase):
    def test_roundtrip_is_encrypted_independent_of_defaults_and_survives_reopen(self):
        plan = aliased_plan()
        with TemporaryDirectory() as root:
            secrets = _Secrets()
            store = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(root, secrets)
            )
            stored = store.put(PROJECT, plan)
            self.assertIsNone(stored.identity_alias_evidence)
            self.assertIsNone(stored.create_field_evidence_id)
            self.assertNotIn("Private source", stored.to_json())
            self.assertNotIn("Private destination", stored.to_json())
            parsed = DestinationMatchPlan.from_json(stored.to_json())
            reopened = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(root, secrets)
            )
            self.assertEqual(
                reopened.read(PROJECT, parsed), plan.identity_alias_evidence
            )
            self.assertEqual(store.put(PROJECT, plan), stored)
            self.assertEqual(store.put(PROJECT, stored), stored)
            encrypted = tuple(Path(root).rglob("*.ipe"))
            self.assertEqual(len(encrypted), 1)
            self.assertNotIn(b"Private source", encrypted[0].read_bytes())
            self.assertNotIn(b"Private destination", encrypted[0].read_bytes())

    def test_exact_read_binding_drift_is_rejected(self):
        plan = aliased_plan()
        with TemporaryDirectory() as root:
            store = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(root, _Secrets())
            )
            stored = store.put(PROJECT, plan)
            for field in (
                "source_selection_hash",
                "source_schema_hash",
                "destination_target_hash",
                "destination_credential_binding_hash",
                "destination_read_principal_hash",
                "destination_read_permission_hash",
                "destination_read_context_hash",
                "destination_schema_snapshot_hash",
                "destination_record_snapshot_hash",
            ):
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(WorkspaceError, "unavailable or changed"),
                ):
                    store.read(PROJECT, replace(stored, **{field: OTHER_HASH}))
            with self.assertRaises(WorkspaceError):
                store.read(PROJECT, replace(stored, workspace_id="another-workspace"))
            with self.assertRaises(WorkspaceError):
                store.read(OTHER_PROJECT, stored)

    def test_missing_evidence_and_tampered_ciphertext_fail_closed(self):
        plan = aliased_plan()
        with TemporaryDirectory() as root:
            store = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(root, _Secrets())
            )
            with self.assertRaises(WorkspaceError):
                store.put(PROJECT, replace(plan, identity_alias_evidence=None))
            with self.assertRaises(WorkspaceError):
                store.read(PROJECT, plan)
            stored = store.put(PROJECT, plan)
            path = next(Path(root).rglob("*.ipe"))
            path.write_bytes(path.read_bytes() + b"tampered")
            with self.assertRaises(WorkspaceError):
                store.read(PROJECT, stored)

    def test_public_binding_cannot_grant_aliases_to_writable_or_scoped_models(self):
        plan = aliased_plan()
        with self.assertRaisesRegex(ValueError, "existing-record-only"):
            replace(
                plan,
                model_matches=(
                    replace(plan.model_matches[0], destination_handling="transfer"),
                ),
            )
        with self.assertRaisesRegex(ValueError, "unscoped"):
            replace(
                plan,
                model_matches=(
                    replace(
                        plan.model_matches[0],
                        scope_fields=("parent_id",),
                        scope_dataset_ids=("parents",),
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            replace(plan, contract_version=17)
        corrupted = json.loads(plan.identity_alias_evidence.to_json())
        corrupted["values"][0]["choices"]["aliases"][0]["target_value"] = "Changed"
        with self.assertRaises(ValueError):
            DestinationIdentityAliasEvidence.from_json(json.dumps(corrupted))

    def test_retired_match_contracts_require_fresh_checks_and_preserve_source(self):
        base = aliased_plan()
        original = base.to_json()
        for version in (16, 17):
            with self.subTest(version=version):
                payload = {**base.to_dict(), "contract_version": version}
                with self.assertRaisesRegex(
                    RetiredDestinationMatchPlanError,
                    "Check the destination again.*frozen source data is unchanged",
                ):
                    DestinationMatchPlan.from_json(json.dumps(payload))
                self.assertEqual(base.to_json(), original)

    def test_reusable_rules_are_separate_encrypted_and_bound_to_stable_owner(self):
        rule = DestinationIdentityAliasRule(
            "x.reference",
            ("name",),
            (ValueMapping("Private source", "Private destination"),),
        )
        with TemporaryDirectory() as root:
            secrets = _Secrets()
            store = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(root, secrets)
            )
            reference = store.put_rules(PROJECT, owner_key=HASH, values=(rule,))
            self.assertNotIn("Private source", json.dumps(reference.to_dict()))
            self.assertEqual(
                ProtectedIdentityAliasRulesReference.from_dict(reference.to_dict()),
                reference,
            )
            reopened = ProtectedDestinationIdentityAliasStore(
                ProtectedProjectEvidenceStore(root, secrets)
            )
            self.assertEqual(
                reopened.read_rules(PROJECT, reference, owner_key=HASH), (rule,)
            )
            with self.assertRaisesRegex(
                WorkspaceError, "different source or destination"
            ):
                store.read_rules(PROJECT, reference, owner_key=OTHER_HASH)
            with self.assertRaises(WorkspaceError):
                store.read_rules(OTHER_PROJECT, reference, owner_key=HASH)
            self.assertNotIn(
                b"Private source", next(Path(root).rglob("*.ipe")).read_bytes()
            )
            with self.assertRaises(WorkspaceError):
                store.put_rules(PROJECT, owner_key=HASH, values=(rule, rule))


if __name__ == "__main__":
    unittest.main()
