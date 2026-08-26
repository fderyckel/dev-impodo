from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode
from impodo.domain.execution.planner import PreflightRequirementPlan, ReferenceReadRequirement
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from impodo.web.composition.target_readers import (
    _capture_recipe_supporting_values,
    _read_readiness_snapshots,
    _read_supporting_lookup_snapshots,
)
from impodo.application.run.odoo_requirements import (
    OdooCheckRelationshipRequirement,
    OdooCheckSupportingRequirement,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError


HASH = "sha256:" + "1" * 64


class RemoteReadinessCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemorySecretStore()
        self.workspace_state = WorkspaceState(
            workspace_id="project-1",
            name="Production customers",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://production.example.test",
            odoo_database="production",
            intended_models=("res.partner",),
        )
        self.first = store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "first-secret",
            persistent=False,
        )
        self.target_hash = target_identity_hash(
            connection_mode=self.workspace_state.odoo_connection_mode.value,
            base_url=self.workspace_state.odoo_base_url,
            database=self.workspace_state.odoo_database,
        )
        self.schema = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="res.partner",
                    fields=(
                        SimpleNamespace(
                            name="country_id",
                            type="many2one",
                            relation="res.country",
                        ),
                    ),
                ),
            ),
            odoo_version="19.0",
            read_credential_binding_hash=self.first.binding_hash,
            read_principal_hash="sha256:" + "2" * 64,
            read_permission_hash="sha256:" + "3" * 64,
            read_context_hash="sha256:" + "4" * 64,
            connection_target_hash=self.target_hash,
        )
        self.probe_calls: list[tuple[str, tuple[str, ...]]] = []
        self.reader_calls = 0

    @staticmethod
    def _requirements(
        metadata_requests=(),
        record_requests=(),
        reference_requirements=(),
    ):
        return PreflightRequirementPlan(
            metadata_requests=tuple(metadata_requests),
            record_requests=tuple(record_requests),
            reference_requirements=tuple(reference_requirements),
            source_record_count=0,
        )

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
            target_credential_workspace=lambda _workspace_id: self.workspace_state,
            queries=SimpleNamespace(
                get_odoo_schema_catalog=lambda _project_id: self.schema
            ),
            read_identity_probe=probe,
            readiness_reader=reader,
        )

    def test_rotated_generation_is_probed_but_cannot_reuse_schema(self) -> None:
        store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "rotated-secret",
            persistent=False,
        )
        context = self._context()

        with self.assertRaisesRegex(WorkspaceError, "read key.*changed"):
            _read_readiness_snapshots(
                context,
                self.workspace_state,
                self._requirements(),
            )

        self.assertEqual(
            self.probe_calls,
            [("rotated-secret", ("res.partner",))],
        )
        self.assertEqual(self.reader_calls, 0)

    def test_acl_change_blocks_comparison_before_target_records(self) -> None:
        context = self._context(permission_hash="sha256:" + "9" * 64)

        with self.assertRaisesRegex(WorkspaceError, "permissions.*changed"):
            _read_readiness_snapshots(
                context,
                self.workspace_state,
                self._requirements(),
            )

        self.assertEqual(self.reader_calls, 0)

    def test_comparison_reads_reviewed_linked_model_outside_primary_schema(
        self,
    ) -> None:
        context = self._context()
        metadata_requests = (
            MetadataRequest(model="res.country", fields=("code",)),
        )
        record_requests = (
            RecordRequest(
                model="res.country",
                fields=("code",),
                domain=(["code", "in", ["FR"]],),
            ),
        )

        result = _read_readiness_snapshots(
            context,
            self.workspace_state,
            self._requirements(
                metadata_requests,
                record_requests,
                (
                    ReferenceReadRequirement(
                        parent_model="res.partner",
                        relationship_field="country_id",
                        relationship_type="many2one",
                        relation_model="res.country",
                        key_fields=("code",),
                        scope_fields=(),
                        requested_fields=("code",),
                    ),
                ),
            ),
        )

        self.assertEqual(result, ("metadata", "records"))
        self.assertEqual(
            self.probe_calls,
            [
                ("first-secret", ("res.partner",)),
                ("first-secret", ("res.country",)),
            ],
        )
        self.assertEqual(self.reader_calls, 1)

    def test_comparison_rejects_unreviewed_model_outside_primary_schema(
        self,
    ) -> None:
        context = self._context()

        with self.assertRaisesRegex(WorkspaceError, "governed read policy"):
            _read_readiness_snapshots(
                context,
                self.workspace_state,
                self._requirements(
                    (MetadataRequest(model="res.users", fields=("login",)),),
                ),
            )

        self.assertEqual(self.probe_calls, [])
        self.assertEqual(self.reader_calls, 0)

    def test_comparison_rejects_unreviewed_field_on_linked_model(self) -> None:
        context = self._context()

        with self.assertRaisesRegex(WorkspaceError, "governed read policy"):
            _read_readiness_snapshots(
                context,
                self.workspace_state,
                self._requirements(
                    (
                        MetadataRequest(
                            model="res.country",
                            fields=("vat_label",),
                        ),
                    ),
                    reference_requirements=(
                        ReferenceReadRequirement(
                            parent_model="res.partner",
                            relationship_field="country_id",
                            relationship_type="many2one",
                            relation_model="res.country",
                            key_fields=("code",),
                            scope_fields=(),
                            requested_fields=("code",),
                        ),
                    ),
                ),
            )

        self.assertEqual(self.probe_calls, [])
        self.assertEqual(self.reader_calls, 0)

    def test_comparison_rejects_incompatible_captured_reference_metadata(
        self,
    ) -> None:
        self.schema.models = (
            *self.schema.models,
            SimpleNamespace(
                name="res.country",
                fields=(
                    SimpleNamespace(
                        name="code",
                        type="char",
                        required=True,
                        readonly=True,
                        relation=None,
                    ),
                ),
            ),
        )
        context = self._context()

        with self.assertRaisesRegex(WorkspaceError, "governed read policy"):
            _read_readiness_snapshots(
                context,
                self.workspace_state,
                self._requirements(
                    (MetadataRequest(model="res.country", fields=("code",)),),
                    (
                        RecordRequest(
                            model="res.country",
                            fields=("code",),
                            domain=(["code", "in", ["FR"]],),
                        ),
                    ),
                    (
                        ReferenceReadRequirement(
                            parent_model="res.partner",
                            relationship_field="country_id",
                            relationship_type="many2one",
                            relation_model="res.country",
                            key_fields=("code",),
                            scope_fields=(),
                            requested_fields=("code",),
                        ),
                    ),
                ),
            )

        self.assertEqual(self.probe_calls, [])
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
            "impodo.web.composition.target_readers.Json2ReadConnector",
            return_value=connector,
        ):
            _metadata, _records, access = _read_supporting_lookup_snapshots(
                context,
                self.workspace_state,
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
        self.assertEqual(access.permission_hash, "sha256:" + "8" * 64)


class RecipeSupportingValueBatchTests(unittest.TestCase):
    def test_several_recipe_relationships_use_one_bounded_reader_call(self) -> None:
        workspace = WorkspaceState(
            workspace_id="recipe-setup",
            name="Recipe setup",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://test.example.test",
            odoo_database="test",
            intended_models=(
                "res.country",
                "res.currency",
                "res.partner",
            ),
        )
        target_hash = target_identity_hash(
            connection_mode="REMOTE",
            base_url=workspace.odoo_base_url,
            database=workspace.odoo_database,
        )

        def field(name, field_type="char", *, relation=None, required=False):
            return SchemaField(
                name=name,
                label=name.replace("_", " ").title(),
                type=field_type,
                required=required,
                readonly=False,
                relation=relation,
                relation_field=None,
                selection=(),
            )

        schema = OdooSchemaCatalog(
            workspace_id=workspace.workspace_id,
            policy_hash=HASH,
            captured_at=datetime.now(timezone.utc),
            captured_by="Data manager",
            connection_mode="REMOTE",
            database="test",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    "res.partner",
                    "Contacts",
                    (
                        field(
                            "country_id",
                            "many2one",
                            relation="res.country",
                        ),
                        field(
                            "currency_id",
                            "many2one",
                            relation="res.currency",
                        ),
                    ),
                ),
                SchemaModel(
                    "res.country",
                    "Countries",
                    (field("code", required=True), field("name", required=True)),
                ),
                SchemaModel(
                    "res.currency",
                    "Currencies",
                    (field("name", required=True),),
                ),
            ),
            content_hash=HASH,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash="credential",
            read_principal_hash="principal",
            read_permission_hash="permission",
            read_context_hash="context",
            connection_target_hash=target_hash,
        )
        fingerprint = TargetFingerprint(
            target_hash=target_hash,
            connection_mode="REMOTE",
            database="test",
            odoo_version="19.0",
            snapshot_timestamp="2026-08-25T00:00:00Z",
        )
        calls = []

        def reader(_workspace, metadata_requests, record_requests):
            calls.append((tuple(metadata_requests), tuple(record_requests)))
            return (
                MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={
                        "res.country": ModelMetadata(
                            "res.country",
                            "Countries",
                            {
                                "code": FieldMetadata(
                                    "code", "char", required=True
                                ),
                                "name": FieldMetadata(
                                    "name", "char", required=True
                                ),
                            },
                        ),
                        "res.currency": ModelMetadata(
                            "res.currency",
                            "Currencies",
                            {
                                "name": FieldMetadata(
                                    "name", "char", required=True
                                )
                            },
                        ),
                    },
                ),
                RecordSnapshot(
                    fingerprint=fingerprint,
                    records={
                        "res.country": (
                            TargetRecord(
                                "res.country",
                                21,
                                {"code": "FR", "name": "France"},
                            ),
                        ),
                        "res.currency": (
                            TargetRecord(
                                "res.currency",
                                1,
                                {"name": "EUR"},
                            ),
                        ),
                    },
                    requested_fields={
                        request.model: request.fields
                        for request in record_requests
                    },
                ),
            )

        captures = []

        def capture(_workspace_id, **values):
            captures.append(values)
            return SimpleNamespace(**values)

        context = SimpleNamespace(
            actor=SimpleNamespace(),
            readiness_reader=reader,
            supporting_lookups=SimpleNamespace(capture=capture),
        )
        requirements = tuple(
            OdooCheckSupportingRequirement(
                model_name=model_name,
                key_fields=(key_field,),
                scope_fields=(),
                relationships=(
                    OdooCheckRelationshipRequirement(
                        parent_model="res.partner",
                        relationship_field=relationship_field,
                        relationship_type="many2one",
                    ),
                ),
                recipe_names=("Customers",),
            )
            for model_name, key_field, relationship_field in (
                ("res.country", "code", "country_id"),
                ("res.currency", "name", "currency_id"),
            )
        )

        stored = _capture_recipe_supporting_values(
            context,
            workspace,
            schema,
            requirements,
        )

        self.assertEqual(len(calls), 1)
        metadata_requests, record_requests = calls[0]
        self.assertEqual(
            tuple(request.model for request in metadata_requests),
            ("res.country", "res.currency"),
        )
        self.assertTrue(all(request.limit == 2_001 for request in record_requests))
        self.assertEqual(len(stored), 2)
        self.assertEqual(len(captures), 2)
        self.assertEqual(
            {item["relation_model"] for item in captures},
            {"res.country", "res.currency"},
        )


if __name__ == "__main__":
    unittest.main()
