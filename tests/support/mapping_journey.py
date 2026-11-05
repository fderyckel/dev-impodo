"""Build real frozen CSV evidence with isolated, fictional Odoo read adapters."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from impodo.adapters.odoo.local_stack import (
    LocalStackCheck,
    LocalStackProfile,
    LocalStackStatus,
    ReadinessLevel,
)
from impodo.domain.odoo.company import OdooCompanyAccess, OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    target_identity_hash,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import (
    TargetCredentialRole,
    local_read_credential_binding_hash,
    store_target_credential,
)
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class MappingJourneyFixture(HierarchyWorkflowFixture):
    """Use the authenticated ProjectSetupBrowserTestCase app and real repositories."""

    def setUp(self):
        super().setUp()
        self.probe_patches = ExitStack()

    def tearDown(self):
        self.probe_patches.close()
        super().tearDown()

    def _post(self, path, data):
        """Give the reusable source fixture a recognizable tutorial project name."""
        if path == "/projects/new":
            data = {**data, "display_name": "Workshop products"}
        return super()._post(path, data)

    def configure(self, major, mode):
        """Return a Stage 2 workspace with no company or saved matching draft."""
        workspace_id = self.freeze_files({
            "products": b"Reference,Product title,Category,Amount,Use\nP001,Workshop drill,Tools,12.50,yes\nP002,Spare drill,Tools,9.25,no\n",
            "spare_parts": b"Reference,Product title,Category,Amount,Use\nS001,Spare kit,Tools,25.00,yes\nS002,Extra kit,Tools,5.00,no\n",
        })
        state = self.context.queries.get(workspace_id)
        state = self.context.workspace_states.update_target(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode.value,
            odoo_base_url="http://127.0.0.1:8069" if mode is OdooConnectionMode.LOCAL else "https://fictional.example.test",
            odoo_database=f"fictional_odoo{major}",
            intended_applications=(),
            intended_models=("x.item", "x.category"),
        )
        target_hash = target_identity_hash(
            connection_mode=mode.value,
            base_url=state.odoo_base_url,
            database=state.odoo_database,
        )
        self.metadata = MetadataSnapshot(
            TargetFingerprint(
                target_hash, mode.value, state.odoo_database, f"{major}.0",
                datetime.now(UTC).isoformat(), {"base": f"{major}.0.1"},
            ),
            {
                "x.item": ModelMetadata("x.item", "Product", {
                    "ref": FieldMetadata("ref", "char", "Reference", required=True),
                    "name": FieldMetadata("name", "char", "Name", required=True),
                    "note": FieldMetadata("note", "char", "Internal note"),
                    "amount": FieldMetadata("amount", "float", "Amount", digits=(16, 2)),
                    "category_id": FieldMetadata("category_id", "many2one", "Category", relation="x.category"),
                }),
                "x.category": ModelMetadata("x.category", "Category", {
                    "name": FieldMetadata("name", "char", "Name", required=True),
                }),
            },
        )
        self.access = OdooCompanyAccess("manager@example.test", "Example Group", (
            OdooCompanySelection(target_hash, 1, "Example Group"),
            OdooCompanySelection(target_hash, 105, "Example Factory"),
        ))
        if mode is OdooConnectionMode.LOCAL:
            self._local_profile(state)
            binding = local_read_credential_binding_hash(state)
            identity = None
            self.context.readiness_reader = None
        else:
            owner = self.context.target_credential_workspace(
                workspace_id, workspace_state=state, actor=self.actor,
            )
            binding = store_target_credential(
                self.secrets, owner, TargetCredentialRole.READ,
                "fictional-read-key", persistent=False,
            ).binding_hash
            identity = self.read_identity(state, "fictional-read-key", tuple(self.metadata.models))
            self.context.readiness_reader = self.snapshots
            self.probe_patches.enter_context(patch(
                "impodo.web.routers.odoo_company.read_company_access",
                return_value=self.access,
            ))
        self.context.schema_read_session_factory = None
        self.context.schema_reader = lambda *_args: self.metadata
        self.context.read_identity_probe = self.read_identity
        schema = self.context.schema_workspace.capture(
            workspace_id, self.metadata, read_credential_binding_hash=binding,
            read_identity=identity, actor=self.actor,
        )
        self.context.schema_workspace.govern(
            workspace_id, actor=self.actor, business_keys=(
                BusinessKeyDefinition("item:reference", "x.item", ("ref",),
                    description="Reference", status=BusinessKeyStatus.CONFIRMED),
                BusinessKeyDefinition("category:name", "x.category", ("name",),
                    description="Name", status=BusinessKeyStatus.CONFIRMED),
            ),
        )
        self.workspace_id = workspace_id
        self.selection = self.context.queries.get_mapping_source_selection(workspace_id)
        return workspace_id, self.selection, schema

    def read_identity(self, state, _secret, models):
        """Bind each fictional read to the actual selected company and major."""
        return OdooReadIdentity(
            target_hash=self.metadata.fingerprint.target_hash,
            principal_hash=content_hash("fictional-manager"),
            permission_hash=content_hash("fictional-read-permissions"),
            context_hash=state.odoo_company.context_hash if state.odoo_company else content_hash("no-company"),
            readable_models=tuple(sorted(models)),
            observed_at=datetime.now(UTC).isoformat(),
        )

    def snapshots(self, _state, metadata_requests, record_requests):
        """Return only requested fictional metadata and zero existing target rows."""
        models = {
            request.model: replace(self.metadata.models[request.model], fields={
                name: field for name, field in self.metadata.models[request.model].fields.items()
                if request.all_fields or name in request.fields
            })
            for request in metadata_requests if request.model in self.metadata.models
        }
        return (
            replace(self.metadata, models=models),
            RecordSnapshot(self.metadata.fingerprint,
                records={request.model: () for request in record_requests},
                requested_fields={request.model: request.fields for request in record_requests}),
        )

    def _local_profile(self, state):
        """Keep local routes on mocked local adapters without accessing any Odoo process."""
        root = Path(self.temporary.name)
        profile = LocalStackProfile(
            root / "odoo.conf", root, "127.0.0.1", 5432, "fixture",
            "127.0.0.1", 8069, state.odoo_base_url, state.odoo_database,
            None, None, None, None, None, None,
        )
        status = LocalStackStatus(str(profile.config_path), profile.base_url,
            profile.database_hint, (
                LocalStackCheck("odoo", "Odoo", ReadinessLevel.READY, "Fictional Odoo"),
                LocalStackCheck("api", "Metadata", ReadinessLevel.READY, "Fictional metadata"),
            ), profile)
        self.probe_patches.enter_context(patch.object(
            self.context.local_stack, "get", return_value=status,
        ))
        self.local_odoo_reader.get_company_access.return_value = self.access
        self.local_odoo_reader.get_model_metadata.side_effect = lambda *_args: self.metadata
        self.local_odoo_reader.get_preflight_snapshots.side_effect = (
            lambda state, _profile, metadata, records, **_kwargs: self.snapshots(state, metadata, records)
        )
