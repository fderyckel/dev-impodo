"""Bind actual hierarchy comparison and load identities to a stateful target."""

from datetime import UTC, datetime

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.hierarchy_odoo import HierarchyOdoo


def compared_hierarchy(fixture, *, major=19, mode="LOCAL", source_shape="generated"):
    workspace_id, revision = fixture.hierarchy(
        major=major, mode=mode, source_shape=source_shape
    )
    fixture.prepare(workspace_id)
    fixture.approve(workspace_id)
    fixture.expect_business_writes(workspace_id)
    return workspace_id, revision, compare_hierarchy(fixture, workspace_id)


def compare_hierarchy(fixture, workspace_id):
    context, actor = fixture.context, fixture.actor
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    target = HierarchyOdoo(schema)
    report = context.preflight.compare(workspace_id, reader=target.compare, actor=actor)
    fixture.assertEqual(report.status, "READY", report)
    fixture.assertEqual(report.create_count, 13)
    fixture.assertEqual(report.unchanged_count, 5)
    preview = context.execution.current_preview(workspace_id, actor=actor)
    fixture.assertTrue(preview.can_load, preview.scope_error)
    snapshot = preview.snapshot
    target.scope_hash = preview.api_scope.semantic_hash
    observed = datetime.now(UTC).isoformat()
    read = OdooReadIdentity(
        target_hash=snapshot.target_hash,
        principal_hash=snapshot.read_principal_hash,
        permission_hash=snapshot.read_permission_hash,
        context_hash=snapshot.read_context_hash,
        readable_models=snapshot.readable_models,
        observed_at=observed,
    )
    write = OdooWriteIdentity(
        target_hash=snapshot.target_hash,
        principal_hash=content_hash("writer"),
        permission_hash=content_hash("write permission"),
        context_hash=snapshot.read_context_hash,
        readable_models=snapshot.readable_models,
        writable_models=snapshot.readable_models,
        observed_at=observed,
    )
    owner = context.target_credential_workspace(
        workspace_id, workspace_state=context.queries.get(workspace_id), actor=actor
    )
    credential = store_target_credential(
        context.secret_store,
        owner,
        TargetCredentialRole.WRITE,
        "fictional-write-key",
        persistent=False,
    )
    return (
        target,
        snapshot,
        {
            "read_identity": read,
            "read_credential_binding_hash": snapshot.read_credential_binding_hash,
            "write_identity": write if schema.connection_mode == "REMOTE" else None,
            "write_credential_binding_hash": (
                credential.binding_hash if schema.connection_mode == "REMOTE" else ""
            ),
        },
    )


def json2_adapters(fixture, workspace_id, target):
    """Use production JSON-2 adapters over the independent fictional transport."""

    state = fixture.context.queries.get(workspace_id)
    preview = fixture.context.execution.current_preview(workspace_id, actor=fixture.actor)
    config = Json2Config(
        base_url=state.odoo_base_url,
        database=state.odoo_database,
        api_key="fictional-key",
        connection_mode=state.odoo_connection_mode.value,
        retries=0,
    )
    return (
        Json2WriteExecutor(config, preview.api_scope, transport=target.transport),
        Json2ReadbackReader(config, preview.api_scope, transport=target.transport),
    )
