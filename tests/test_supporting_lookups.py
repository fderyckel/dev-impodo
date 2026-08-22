from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.supporting_lookup_repository import (
    SupportingLookupRepository,
)
from impodo.application.supporting_lookup_service import SupportingLookupService
from impodo.models import target_identity_hash
from impodo.projects import WorkspaceState, OdooConnectionMode
from impodo.supporting_lookups import SupportingLookupChoice


ROOT = Path(__file__).resolve().parents[1]


class SupportingLookupPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbDatabase(self.temporary.name)
        self.projects = ProjectRepository(database)
        self.repository = SupportingLookupRepository(database)
        self.service = SupportingLookupService(
            self.repository,
            CapabilityAuthorizationPolicy(),
        )
        self.now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        self.project = WorkspaceState(
            project_id=str(uuid4()),
            name="Country lookup",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://production.example.test",
            odoo_database="production",
            intended_models=("res.partner",),
            updated_at=self.now,
        )
        self.projects.create(
            self.project,
            recipe_id=str(uuid4()),
            data_version_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        self.target_hash = target_identity_hash(
            connection_mode="REMOTE",
            base_url=self.project.odoo_base_url,
            database=self.project.odoo_database,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshot_reuses_portable_values_and_target_change_retires_pointer(
        self,
    ) -> None:
        captured = self.service.capture(
            self.project.project_id,
            relation_model="res.country",
            key_fields=("code",),
            scope_fields=(),
            display_field="name",
            target_hash=self.target_hash,
            read_credential_binding_hash="sha256:" + "1" * 64,
            read_principal_hash="sha256:" + "2" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            captured_at=self.now,
            choices=(
                SupportingLookupChoice("FR", "France (FR)"),
                SupportingLookupChoice("BE", "Belgium (BE)"),
            ),
            ambiguous_values=("XX",),
            actor=LOCAL_ACTOR,
        )

        restored = self.service.current(
            self.project.project_id,
            relation_model="res.country",
            key_fields=("code",),
            scope_fields=(),
            display_field="name",
            target_hash=self.target_hash,
            read_credential_binding_hash="sha256:" + "1" * 64,
            read_principal_hash="sha256:" + "2" * 64,
            read_context_hash="sha256:" + "4" * 64,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(restored, captured)
        self.assertNotIn("odoo_id", captured.to_json())
        self.assertNotIn("api_key", captured.to_json())
        changed = replace(
            self.project,
            odoo_database="replacement",
            revision=self.project.revision + 1,
            updated_at=self.now + timedelta(minutes=1),
        )
        self.projects.save(
            changed,
            expected_revision=self.project.revision,
            event_type="PROJECT_TARGET_UPDATED",
            event_detail="Target changed",
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(
            self.repository.get_current(
                self.project.project_id,
                captured.lookup_key,
            )
        )

    def test_read_context_mismatch_prevents_reuse(self) -> None:
        self.service.capture(
            self.project.project_id,
            relation_model="res.country",
            key_fields=("code",),
            scope_fields=(),
            display_field="name",
            target_hash=self.target_hash,
            read_credential_binding_hash="binding",
            read_principal_hash="principal",
            read_permission_hash="permission",
            read_context_hash="first-context",
            captured_at=self.now,
            choices=(SupportingLookupChoice("FR", "France (FR)"),),
            ambiguous_values=(),
            actor=LOCAL_ACTOR,
        )

        current = self.service.current(
            self.project.project_id,
            relation_model="res.country",
            key_fields=("code",),
            scope_fields=(),
            display_field="name",
            target_hash=self.target_hash,
            read_credential_binding_hash="binding",
            read_principal_hash="principal",
            read_context_hash="different-context",
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(current)


if __name__ == "__main__":
    unittest.main()
