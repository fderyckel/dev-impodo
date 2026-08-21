from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from impodo.connectors import MetadataSnapshot, RecordSnapshot
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    target_identity_hash,
)
from impodo.projects import MigrationProject, OdooConnectionMode
from impodo.secrets import MemorySecretStore
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from impodo.web.target_readers import (
    _read_readiness_snapshots,
    _read_supporting_lookup_snapshots,
)
from impodo.workspace_errors import WorkspaceError


HASH = "sha256:" + "1" * 64


class RemoteReadinessCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemorySecretStore()
        self.project = MigrationProject(
            project_id="project-1",
            name="Production customers",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://production.example.test",
            odoo_database="production",
            intended_models=("res.partner",),
        )
        self.first = store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.READ,
            "first-secret",
            persistent=False,
        )
        self.target_hash = target_identity_hash(
            connection_mode=self.project.odoo_connection_mode.value,
            base_url=self.project.odoo_base_url,
            database=self.project.odoo_database,
        )
        self.schema = SimpleNamespace(
            models=(SimpleNamespace(name="res.partner"),),
            read_credential_binding_hash=self.first.binding_hash,
            read_principal_hash="sha256:" + "2" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            connection_target_hash=self.target_hash,
        )
        self.probe_calls: list[tuple[str, tuple[str, ...]]] = []
        self.reader_calls = 0

    def _context(self, *, permission_hash: str | None = None):
        def probe(_project, secret, models):
            normalized = tuple(sorted(models))
            self.probe_calls.append((secret, normalized))
            return OdooReadIdentity(
                target_hash=self.target_hash,
                principal_hash=self.schema.read_principal_hash,
                permission_hash=(
                    permission_hash or self.schema.read_permission_hash
                ),
                context_hash=self.schema.read_context_hash,
                readable_models=normalized,
                observed_at="2026-08-19T00:00:00Z",
            )

        def reader(_project, _metadata, _records):
            self.reader_calls += 1
            return "metadata", "records"

        return SimpleNamespace(
            secret_store=self.store,
            queries=SimpleNamespace(
                get_odoo_schema_catalog=lambda _project_id: self.schema
            ),
            read_identity_probe=probe,
            readiness_reader=reader,
        )

    def test_rotated_generation_is_probed_but_cannot_reuse_schema(self) -> None:
        store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.READ,
            "rotated-secret",
            persistent=False,
        )
        context = self._context()

        with self.assertRaisesRegex(WorkspaceError, "read key.*changed"):
            _read_readiness_snapshots(context, self.project, (), ())

        self.assertEqual(
            self.probe_calls,
            [("rotated-secret", ("res.partner",))],
        )
        self.assertEqual(self.reader_calls, 0)

    def test_acl_change_blocks_comparison_before_target_records(self) -> None:
        context = self._context(permission_hash="sha256:" + "9" * 64)

        with self.assertRaisesRegex(WorkspaceError, "permissions.*changed"):
            _read_readiness_snapshots(context, self.project, (), ())

        self.assertEqual(self.reader_calls, 0)

    def test_remote_supporting_lookup_reads_inferred_model_outside_schema(
        self,
    ) -> None:
        context = self._context(permission_hash="sha256:" + "8" * 64)
        context.readiness_reader = None
        fingerprint = TargetFingerprint(
            target_hash=self.target_hash,
            connection_mode="REMOTE",
            database="production",
            odoo_version="19.0",
            snapshot_timestamp="2026-08-21T00:00:00Z",
        )

        class FakeConnector:
            def __init__(self):
                self.metadata_requests = ()
                self.record_requests = ()

            def get_model_metadata(self, requests):
                self.metadata_requests = tuple(requests)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={
                        "res.country": ModelMetadata(
                            model="res.country",
                            description="Country",
                            fields={
                                "code": FieldMetadata("code", "char"),
                                "name": FieldMetadata("name", "char"),
                            },
                        )
                    },
                )

            def get_records(self, requests):
                self.record_requests = tuple(requests)
                return RecordSnapshot(
                    fingerprint=fingerprint,
                    records={"res.country": ()},
                    requested_fields={"res.country": ("code", "name")},
                )

        connector = FakeConnector()
        with patch(
            "impodo.web.target_readers.Json2ReadConnector",
            return_value=connector,
        ):
            _metadata, _records, access = _read_supporting_lookup_snapshots(
                context,
                self.project,
                self.schema,
                relation_model="res.country",
                requested_fields=("code", "name"),
            )

        self.assertEqual(
            self.probe_calls,
            [("first-secret", ("res.country",))],
        )
        self.assertEqual(connector.metadata_requests[0].model, "res.country")
        self.assertEqual(connector.metadata_requests[0].fields, ("code", "name"))
        self.assertEqual(connector.record_requests[0].model, "res.country")
        self.assertEqual(connector.record_requests[0].limit, 2001)
        self.assertEqual(access[2], "sha256:" + "8" * 64)


if __name__ == "__main__":
    unittest.main()
