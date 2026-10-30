"""Metadata-only fixtures for business intent; no source rows or target values."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping, MappingDefinition, MappingTargetMode, ReferenceKeyMapping, RelationshipMapping, RelationshipResolver, ResolverOrigin, ScalarFieldMapping, ScalarValueSource
from impodo.domain.mapping.validation.evidence import MappingValidationResult, MappingValidationStatus
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.shared.access import Actor, ActorIdentity, Capability
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaField, SchemaModel, SchemaOrigin
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState
from impodo.application.workspace.access import WorkspaceAccessContext
from tests.domain.workspace.test_supporting_sources import accepted_selection

NOW = datetime(2026, 10, 30, tzinfo=UTC)
ACTOR = Actor(ActorIdentity("local", "manager", "Manager"), frozenset({Capability.PROJECT_VIEW, Capability.MAPPING_EDIT}))


def metadata_fixture(*, mode=MappingTargetMode.UPSERT, source_mode=SourceMode.FILE, odoo_version="19.0", purpose="AUTHORING"):
    workspace_id, project_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
    selection = accepted_selection()
    workspace = WorkspaceState(workspace_id, "Fictional Contacts", "File", source_mode=source_mode, intended_models=("res.partner",))
    fields = tuple(SchemaField(name, name, kind, False, readonly, relation, None, (),
        stored=True, computed=False, related=False, translated=False, company_dependent=False,
        create_default_present=name == "default_field", create_default_value="checked" if name == "default_field" else None,
    ) for name, kind, readonly, relation in (
        ("ref", "char", False, None), ("name", "char", False, None), ("email", "char", False, None),
        ("initial_note", "char", False, None), ("default_field", "char", False, None),
        ("check_only", "char", True, None), ("country_id", "many2one", False, "res.country"),
        ("category_ids", "many2many", False, "res.partner.category"),
    ))
    schema = OdooSchemaCatalog(workspace_id, ODOO_SOURCE_POLICY_HASH, NOW, "Manager", "REMOTE", "fictional", odoo_version,
        (SchemaModel("res.partner", "Contacts", fields),), "sha256:" + "b" * 64, origin=SchemaOrigin.LIVE_API,
        connection_target_hash="sha256:" + "c" * 64,
        read_credential_binding_hash="sha256:" + "1" * 64, read_principal_hash="sha256:" + "2" * 64,
        read_permission_hash="sha256:" + "3" * 64, read_context_hash="sha256:" + "4" * 64)
    governance = SchemaGovernance(str(uuid4()), 1, workspace_id, schema.content_hash, ("res.partner",), (), NOW, "Manager")
    mapping = DatasetMapping("products", "res.partner", mode=mode,
        on_existing="unchanged" if mode is MappingTargetMode.CREATE else None,
        source_identity_column_keys=("product",), target_identity=(IdentityComponentMapping(("product",), ("ref",)),),
        fields=(ScalarFieldMapping("name", "product"), ScalarFieldMapping("email", "category"),
            ScalarFieldMapping("initial_note", "line", compare=False),
            ScalarFieldMapping("default_field", None, value_source=ScalarValueSource.ODOO_DEFAULT),
            ScalarFieldMapping("check_only", "line", compare=False, validate_only=True)),
        relationships=tuple(RelationshipMapping(name, kind, (column,), RelationshipResolver(ResolverOrigin.TARGET_CATALOG,
            model=related, key_mappings=(ReferenceKeyMapping(column, key),)), operation=operation)
            for name, kind, column, related, key, operation in (
                ("country_id", "many2one", "category", "res.country", "code", "replace"),
                ("category_ids", "many2many", "line", "res.partner.category", "name", "add"),
            )),
    )
    if source_mode is SourceMode.ODOO:
        source = OdooSourceBinding("sha256:" + "a" * 64, "res.partner", ODOO_SOURCE_POLICY_HASH,
            "sha256:" + "b" * 64, "sha256:" + "c" * 64, "sha256:" + "d" * 64, "sha256:" + "e" * 64, "sha256:" + "f" * 64)
        selection = replace(selection, datasets=(replace(selection.datasets[0], source=source),))
        mapping = replace(mapping, mode=mode, fields=mapping.fields[:2], relationships=(), target_identity=(), source_identity_column_keys=(), approved_write_fields=("name", "email"))
    definition = MappingDefinition(str(uuid4()), selection.content_hash, governance.content_hash, (mapping,))
    revision = MappingRevision(definition.mapping_id, 1, None, definition, NOW, "Manager")
    validation = MappingValidationResult(definition.content_hash, selection.content_hash, definition.schema_hash,
        MappingValidationStatus.VALID, (), (), ())
    access = WorkspaceAccessContext(project_id, workspace_id, selection.data_version_id, run_id, run_purpose=purpose)
    return SimpleNamespace(workspace=workspace, selection=selection, schema=schema, governance=governance,
        mapping=revision, validation=validation, access=access)
