"""Run real Recipe evidence stages against one isolated in-memory Odoo adapter."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from impodo.application.workspace.preparation.job_models import PreparationJobStatus
from impodo.domain.execution.odoo_readback import ExternalIdBinding, ReadbackRecord
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    OdooWriteIdentity,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.workspace.business_intent import BusinessIntent, BusinessModelIntent
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.preparation_job_manager import PreparationJobManager
from impodo.web.run_commands import (
    publish_compared_application,
    publish_preparation_progress,
    publish_reconciled_application,
    start_next_preparation,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)
from tests.support.identity import resolve_local_actor


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
        connection_mode=schema.connection_mode, database=schema.database, odoo_version=schema.odoo_version,
        snapshot_timestamp=datetime.now(UTC).isoformat(), module_versions={"base": f"{schema.odoo_version}.1.0"}), models)


class FictionalOdoo:
    """Store actual requested writes and return them independently on read-back."""

    def __init__(self, snapshot, scope, *, imports_external_ids):
        self.target_hash = snapshot.target_hash
        self.scope_hash = scope.semantic_hash
        self.imports_external_ids = imports_external_ids
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

    def update_row(self, model, record_id, values):
        self.records[(model, record_id)].update(values)

    def read_ids(self, model, identifiers, fields):
        return tuple(ReadbackRecord(identifier, {field: self.records[(model, identifier)][field] for field in fields})
            for identifier in identifiers if (model, identifier) in self.records)

    def read_external_ids(self, external_ids):
        return tuple(self.external_ids[item] for item in external_ids if item in self.external_ids)


def prepare_test_application(case, context, application, root):
    """Use the actual worker and publish the run milestone before browser review."""

    manager = PreparationJobManager(root)
    case.addCleanup(manager.shutdown)
    context.preparation_jobs = manager
    manager.set_result_reader(lambda job, actor: context.preparation_recovery.current(
        job.workspace_id, job.workspace.mapping_content_hash, actor=actor,
    ))
    manager.set_status_listener(
        lambda job, actor: publish_preparation_progress(
            context,
            job,
            actor=actor,
        )
    )
    job = start_next_preparation(
        context,
        application.migration_run_id,
        actor=resolve_local_actor(context),
    )
    worker = manager._workers[job.job_id]
    worker.supervisor.join(timeout=60)
    case.assertFalse(worker.supervisor.is_alive(), "The preparation worker did not finish")
    completed = manager.get(application.workspace_id, job.job_id)
    case.assertEqual(completed.status, PreparationJobStatus.SUCCEEDED, completed.failure_message)
    return completed


def complete_application(case, context, application, *, expected_total, write_identity=None):
    """Complete the two-record customer balance fixture."""

    execution, reconciliation, records = execute_test_application(
        case, context, application, target_records={}, expected_creates=2,
        expected_updates=0, write_identity=write_identity,
    )
    case.assertEqual(sum(Decimal(str(values["credit_limit"])) for values in records.values()), Decimal(expected_total))
    return execution, reconciliation


def execute_test_application(
    case, context, application, *, target_records, expected_creates,
    expected_updates, write_mode="create", write_identity=None,
):
    """Compare and verify real Test evidence against a scoped fictional target."""

    workspace_id, actor = application.workspace_id, resolve_local_actor(context)
    state = context.workspace_states.repository.get(workspace_id)
    owner = context.target_credential_workspace(
        workspace_id,
        workspace_state=state,
        actor=actor,
    )
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
    if context.business_intents.get(workspace_id, actor=actor) is None:
        inputs = context.business_intents.inputs(workspace_id, actor=actor)
        context.business_intents.save(
            workspace_id,
            intent=BusinessIntent(tuple(BusinessModelIntent(model.model, write_mode)
                for model in inputs.scope.models)),
            expected_version=0,
            expected_binding_hash=inputs.binding_hash,
            operation_id=str(uuid4()),
            actor=actor,
        )
    summary, evaluation, _ = context.normalization.current_review(workspace_id)
    for group in evaluation.groups:
        if group.requires_decision:
            summary = context.normalization.decide_group(workspace_id, summary.run_id, group.group_id,
                approve=True, expected_version=summary.lifecycle_version, actor=actor)
    summary = context.normalization.approve(workspace_id, summary.run_id,
        expected_version=summary.lifecycle_version, actor=actor)
    case.assertTrue(summary.frozen)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)

    def read_target(requirements):
        case.assertTrue(all(request.domain for request in requirements.record_requests))
        metadata = metadata_for_schema(schema, {request.model: request.fields for request in requirements.metadata_requests})
        requested = {request.model: request.fields for request in requirements.record_requests}
        records = {
            model: tuple(
                TargetRecord(model, record_id, {field: values[field] for field in fields})
                for (record_model, record_id), values in target_records.items()
                if record_model == model
            )
            for model, fields in requested.items()
        }
        return metadata, RecordSnapshot(metadata.fingerprint, records, requested)

    report = context.preflight.compare(workspace_id, reader=read_target, actor=actor)
    case.assertEqual(report.status, "READY")
    case.assertEqual(report.create_count, expected_creates)
    case.assertEqual(report.update_count, expected_updates)
    publish_compared_application(
        context,
        application.application_id,
        application.migration_run_id,
        actor=actor,
    )
    preview = context.execution.current_preview(workspace_id, actor=actor)
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
    adapter = FictionalOdoo(
        snapshot, preview.api_scope,
        imports_external_ids=state.odoo_connection_mode is OdooConnectionMode.REMOTE,
    )
    adapter.records.update({key: dict(values) for key, values in target_records.items()})
    execution = context.execution.execute(workspace_id, expected_snapshot_hash=snapshot.semantic_hash,
        executor=adapter, actor=actor, read_identity=read_identity,
        read_credential_binding_hash=snapshot.read_credential_binding_hash,
        write_identity=write_identity, write_credential_binding_hash=write_generation)
    case.assertEqual(execution.status.value, "COMPLETED")
    expected_count = expected_creates + expected_updates
    case.assertEqual(execution.committed_count, expected_count)
    reconciliation = context.reconciliation.reconcile(workspace_id, expected_execution_run_id=execution.run_id,
        reader=adapter, actor=actor, write_identity=write_identity, write_credential_binding_hash=write_generation)
    case.assertEqual(reconciliation.status.value, "VERIFIED", reconciliation.rows)
    case.assertEqual(reconciliation.verified_count, expected_count)
    publish_reconciled_application(
        context,
        application.application_id,
        application.migration_run_id,
        actor=actor,
    )
    return execution, reconciliation, adapter.records
