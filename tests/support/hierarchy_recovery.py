"""Reopen durable hierarchy evidence and reauthorize through the real recovery route."""

from unittest.mock import patch

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    target_write_credential_id,
)


def reopen_and_check_recovery(
    fixture, workspace_id, target, snapshot, interrupted, identities
):
    """Keep remembered read authority and explicitly replace a lost remote loading key."""

    remote = target.schema.connection_mode == "REMOTE"
    old_context = fixture.context
    owner = old_context.target_credential_workspace(
        workspace_id,
        workspace_state=old_context.queries.get(workspace_id),
        actor=fixture.actor,
    )
    if remote:
        fixture.secrets.delete(target_write_credential_id(owner))

    def reader(state, key, scope):
        config = Json2Config(
            state.odoo_base_url,
            state.odoo_database,
            key,
            connection_mode=state.odoo_connection_mode.value,
            retries=0,
        )
        return Json2ReadbackReader(config, scope, transport=target.transport)

    fixture.reopen(
        read_identity_probe=lambda *_: identities["read_identity"],
        write_identity_probe=lambda *_: identities["write_identity"],
        readback_reader_factory=reader,
    )
    fixture.assertIsNot(fixture.context, old_context)
    restored = fixture.context.execution.journal.get_current_run(workspace_id)
    fixture.assertEqual(restored, interrupted)
    writes_before = tuple(target.writes)
    assessments = []
    assess = fixture.context.reconciliation.assess_recovery

    def capture(_service, *args, **kwargs):
        result = assess(*args, **kwargs)
        assessments.append(result)
        return result

    with patch.object(
        type(fixture.context.reconciliation), "assess_recovery", new=capture
    ):
        response = fixture._post(
            f"/workspaces/{workspace_id}/load/recovery/check",
            {
                "csrf_token": fixture.csrf,
                "snapshot_hash": snapshot.semantic_hash,
                "execution_run_id": interrupted.run_id,
                **({"write_api_key": "fictional-reauthorized-key"} if remote else {}),
            },
        )
    fixture.assertEqual(response.status_code, 303, response.text)
    fixture.assertEqual(tuple(target.writes), writes_before)
    credential = get_target_credential(
        fixture.context.secret_store, owner, TargetCredentialRole.WRITE
    )
    if remote:
        fixture.assertNotEqual(
            credential.binding_hash, interrupted.write_credential_binding_hash
        )
        fixture.assertTrue(
            fixture.context.workspace_states.repository.has_audit_event(
                workspace_id, "ODOO_WRITE_CREDENTIAL_REAUTHORIZED"
            )
        )
    fixture.assertEqual(len(assessments), 1)
    return {
        "write_identity": identities["write_identity"],
        "write_credential_binding_hash": credential.binding_hash if remote else "",
    }, assessments[0]
