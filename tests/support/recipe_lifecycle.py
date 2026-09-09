"""Run real Recipe evidence stages against one isolated in-memory Odoo adapter."""

from datetime import UTC, datetime
from decimal import Decimal

from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, OdooReadIdentity, OdooWriteIdentity, TargetFingerprint
from impodo.domain.execution.odoo_readback import ReadbackRecord, ExternalIdBinding
from impodo.domain.serialization import content_hash
from impodo.web.run_commands import publish_compared_application, publish_reconciled_application
from impodo.web.target_credentials import TargetCredentialRole, get_target_credential, store_target_credential


def metadata_for_schema(schema, requested=None):
    models = {}
    for model in schema.models:
        fields = requested.get(model.name, ()) if requested is not None else tuple(field.name for field in model.fields)
        if requested is not None and model.name not in requested:
            continue
        models[model.name] = ModelMetadata(model.name, model.label, {
            field.name: FieldMetadata(name=field.name, type=field.type, label=field.label,
                required=field.required, readonly=field.readonly, relation=field.relation,
                relation_field=field.relation_field, selection=field.selection)
            for field in model.fields if field.name in fields
        })
    return MetadataSnapshot(TargetFingerprint(target_hash=schema.connection_target_hash,
        connection_mode=schema.connection_mode, database=schema.database, odoo_version="19.0",
        snapshot_timestamp=datetime.now(UTC).isoformat(), module_versions={"base": "19.0.1.0"}), models)


class FictionalOdoo:
    """Store actual requested writes and return them independently on read-back."""

    imports_external_ids = True

    def __init__(self, snapshot, scope):
        self.target_hash = snapshot.target_hash
        self.scope_hash = scope.semantic_hash
        self.records = {}
        self.external_ids = {}
        self.write_batches = []

    def find_ids_many(self, model, domains):
        return tuple(self.find_ids(model, domain) for domain in domains)

    def find_ids(self, model, domain):
        assert model == "res.partner" and domain
        return tuple(identifier for (record_model, identifier), values in self.records.items()
            if record_model == model and all(values[field] == value for field, operator, value in domain if operator == "="))

    def create_rows(self, model, values):
        assert model == "res.partner"
        identifiers = tuple(range(len(self.records) + 1, len(self.records) + len(values) + 1))
        self.write_batches.append((model, tuple(dict(row) for row in values)))
        for identifier, row in zip(identifiers, values, strict=True):
            self.records[(model, identifier)] = dict(row)
        return identifiers

    def load_create_rows(self, model, values, external_ids):
        identifiers = self.create_rows(model, values)
        for identifier, external_id in zip(identifiers, external_ids, strict=True):
            self.external_ids[external_id] = ExternalIdBinding(external_id, model, identifier)
        return identifiers

    def read_ids(self, model, identifiers, fields):
        return tuple(ReadbackRecord(identifier, {field: self.records[(model, identifier)][field] for field in fields})
            for identifier in identifiers if (model, identifier) in self.records)

    def read_external_ids(self, external_ids):
        return tuple(self.external_ids[item] for item in external_ids if item in self.external_ids)


def complete_application(case, context, application, *, expected_total, write_identity=None):
    """Approve, compare, execute and verify persisted evidence without replacing services."""

    workspace_id, actor = application.workspace_id, context.actor
    state = context.workspace_states.repository.get(workspace_id)
    owner = context.target_credential_workspace(workspace_id, workspace_state=state)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    read = get_target_credential(context.secret_store, owner, TargetCredentialRole.READ)
    if read is None:
        read = store_target_credential(context.secret_store, owner, TargetCredentialRole.READ,
            "fictional-current-read-key", persistent=False)
        identity = OdooReadIdentity(target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash, permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash, readable_models=tuple(model.name for model in schema.models),
            observed_at=datetime.now(UTC).isoformat())
        context.schema_workspace.rebind_current_access(workspace_id, metadata_for_schema(schema),
            read_credential_binding_hash=read.binding_hash, read_identity=identity, actor=actor)
    write = store_target_credential(context.secret_store, owner, TargetCredentialRole.WRITE,
        "fictional-current-write-key", persistent=False)
    write_generation = write.binding_hash
    summary, evaluation, _ = context.normalization.current_review(workspace_id)
    for group in evaluation.groups:
        if group.requires_decision:
            summary = context.normalization.decide_group(workspace_id, summary.run_id, group.group_id,
                approve=True, expected_version=summary.lifecycle_version, actor=actor)
    summary = context.normalization.approve(workspace_id, summary.run_id,
        expected_version=summary.lifecycle_version, actor=actor)
    case.assertTrue(summary.frozen)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)

    def empty_target(requirements):
        case.assertTrue(all(request.domain for request in requirements.record_requests))
        metadata = metadata_for_schema(schema, {request.model: request.fields for request in requirements.metadata_requests})
        requested = {request.model: request.fields for request in requirements.record_requests}
        return metadata, RecordSnapshot(metadata.fingerprint, {model: () for model in requested}, requested)

    report = context.preflight.compare(workspace_id, reader=empty_target, actor=actor)
    case.assertEqual(report.status, "READY")
    case.assertEqual(report.create_count, 2)
    publish_compared_application(context, application.application_id, application.migration_run_id)
    preview = context.execution.current_preview(workspace_id)
    snapshot = preview.snapshot
    read_identity = OdooReadIdentity(target_hash=snapshot.target_hash, principal_hash=snapshot.read_principal_hash,
        permission_hash=snapshot.read_permission_hash, context_hash=snapshot.read_context_hash,
        readable_models=snapshot.readable_models, observed_at=datetime.now(UTC).isoformat())
    write_identity = write_identity or OdooWriteIdentity(target_hash=snapshot.target_hash,
        principal_hash=content_hash("fictional Test writer"), permission_hash=content_hash("fictional Test write permission"),
        context_hash=snapshot.read_context_hash, readable_models=("res.partner",), writable_models=("res.partner",),
        observed_at=datetime.now(UTC).isoformat())
    context.production_runs.assert_execution_authority(workspace_id, read_identity=read_identity,
        read_credential_generation=snapshot.read_credential_binding_hash,
        expected_read_credential_generation=snapshot.read_credential_binding_hash,
        write_identity=write_identity, write_credential_generation=write_generation, actor=actor)
    adapter = FictionalOdoo(snapshot, preview.api_scope)
    execution = context.execution.execute(workspace_id, expected_snapshot_hash=snapshot.semantic_hash,
        executor=adapter, actor=actor, read_identity=read_identity,
        read_credential_binding_hash=snapshot.read_credential_binding_hash,
        write_identity=write_identity, write_credential_binding_hash=write_generation)
    case.assertEqual(execution.status.value, "COMPLETED")
    case.assertEqual(execution.committed_count, 2)
    case.assertEqual(sum(Decimal(str(values["credit_limit"])) for values in adapter.records.values()), Decimal(expected_total))
    reconciliation = context.reconciliation.reconcile(workspace_id, expected_execution_run_id=execution.run_id,
        reader=adapter, actor=actor, write_identity=write_identity, write_credential_binding_hash=write_generation)
    case.assertEqual(reconciliation.status.value, "VERIFIED", reconciliation.rows)
    case.assertEqual(reconciliation.verified_count, 2)
    publish_reconciled_application(context, application.application_id, application.migration_run_id)
    return execution, reconciliation
