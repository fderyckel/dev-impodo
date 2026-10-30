"""Remote supporting access compares permission hashes for the same model scope."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.target_readers import _read_supporting_lookup_batches


class SupportingRemoteReadAccessTests(unittest.TestCase):
    def evidence(self):
        workspace = SimpleNamespace(workspace_id="workspace", odoo_connection_mode=OdooConnectionMode.REMOTE)
        schema = SimpleNamespace(
            models=(SimpleNamespace(name="res.partner"),),
            read_credential_binding_hash="credential", connection_target_hash="target",
            read_principal_hash="principal", read_permission_hash="primary-permission",
            read_context_hash="context",
        )
        primary = SimpleNamespace(target_hash="target", principal_hash="principal", permission_hash="primary-permission", context_hash="context", readable_models=("res.partner",))
        supporting = SimpleNamespace(target_hash="target", principal_hash="principal", permission_hash="supporting-permission", context_hash="context", readable_models=("res.country",))
        context = SimpleNamespace(readiness_reader=None, secret_store=Mock(), target_credential_workspace=lambda *args, **kwargs: workspace, read_identity_probe=Mock(side_effect=(primary, supporting)))
        return context, workspace, schema, primary

    def test_different_supporting_scope_keeps_its_own_permission_evidence(self):
        context, workspace, schema, _primary = self.evidence()
        credential = SimpleNamespace(binding_hash="credential", secret="fictional")
        with patch("impodo.web.composition.target_readers.get_target_credential", return_value=credential), \
             patch("impodo.web.composition.target_readers._target_json2_config"), \
             patch("impodo.web.composition.target_readers.Json2ReadConnector") as connector:
            _metadata, _records, access = _read_supporting_lookup_batches(context, workspace, schema, actor=LOCAL_ACTOR, grouped_requests=(("res.country", ("code", "name")),), verify_schema_access=True)
        self.assertEqual(access.permission_hash, "supporting-permission")
        self.assertEqual([call.args[2] for call in context.read_identity_probe.call_args_list], [("res.partner",), ("res.country",)])
        connector.return_value.get_records.assert_called_once()

    def test_changed_primary_permission_stops_before_supporting_value_reads(self):
        context, workspace, schema, primary = self.evidence()
        primary.permission_hash = "changed-primary-permission"
        with patch("impodo.web.composition.target_readers.get_target_credential", return_value=SimpleNamespace(binding_hash="credential", secret="fictional")), \
             patch("impodo.web.composition.target_readers.Json2ReadConnector") as connector:
            with self.assertRaisesRegex(WorkspaceError, "access changed"):
                _read_supporting_lookup_batches(context, workspace, schema, actor=LOCAL_ACTOR, grouped_requests=(("res.country", ("code", "name")),), verify_schema_access=True)
        connector.assert_not_called()
        context.read_identity_probe.assert_called_once()

    def test_identical_primary_and_supporting_scopes_reuse_one_probe(self):
        context, workspace, schema, primary = self.evidence()
        schema.models = (SimpleNamespace(name="res.country"),)
        primary.readable_models = ("res.country",)
        with patch("impodo.web.composition.target_readers.get_target_credential", return_value=SimpleNamespace(binding_hash="credential", secret="fictional")), \
             patch("impodo.web.composition.target_readers._target_json2_config"), \
             patch("impodo.web.composition.target_readers.Json2ReadConnector"):
            _read_supporting_lookup_batches(context, workspace, schema, actor=LOCAL_ACTOR, grouped_requests=(("res.country", ("code", "name")),), verify_schema_access=True)
        context.read_identity_probe.assert_called_once()
